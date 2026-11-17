"""Global admission and cooperative renewable lease using DynamoDB conditions."""

import hashlib
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from .core import Refused, validate_config


def month_reservations(start_epoch, seconds):
    """Allocate reservation seconds to UTC billing months without float rounding."""
    remaining, cursor = int(seconds), int(start_epoch)
    if remaining <= 0:
        raise Refused("Reservation must be positive")
    result = {}
    while remaining:
        current = datetime.fromtimestamp(cursor, timezone.utc)
        following = datetime(
            current.year + (current.month == 12),
            1 if current.month == 12 else current.month + 1,
            1,
            tzinfo=timezone.utc,
        )
        amount = min(remaining, int(following.timestamp()) - cursor)
        result[current.strftime("%Y-%m")] = amount
        cursor += amount
        remaining -= amount
    return result


class ControlStore:
    def __init__(self, client, table="deepresearch-control", clock=time.time):
        if table != "deepresearch-control":
            raise Refused("Unexpected global control table")
        self.client, self.table, self.clock = client, table, clock

    def assert_table_scope(self, account_id, region):
        table = self.client.describe_table(TableName=self.table)["Table"]
        expected = f"arn:aws:dynamodb:{region}:{account_id}:table/{self.table}"
        if table["TableArn"] != expected or table["TableStatus"] != "ACTIVE":
            raise Refused("Control table account/region/status mismatch")
        tags = {
            tag["Key"]: tag["Value"]
            for tag in self.client.list_tags_of_resource(ResourceArn=expected)["Tags"]
        }
        if tags.get("Project") != "deepresearch" or tags.get("Component") != "control":
            raise Refused("Control table tags mismatch")

    def reserve(self, config, start_epoch, reservation_seconds=21600):
        validate_config(config)
        self.assert_table_scope(config["account_id"], config["region"])
        if reservation_seconds != 21600 or abs(start_epoch - int(self.clock())) > 120:
            raise Refused("Admission requires the current six-hour reservation")
        transactions = []
        for month, seconds in month_reservations(start_epoch, reservation_seconds).items():
            transactions.append(
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": {"key": {"S": "month#" + month}},
                        "UpdateExpression": "SET reserved_seconds = if_not_exists(reserved_seconds, :zero) + :amount",
                        "ConditionExpression": "attribute_not_exists(reserved_seconds) OR reserved_seconds <= :remaining",
                        "ExpressionAttributeValues": {
                            ":zero": {"N": "0"},
                            ":amount": {"N": str(seconds)},
                            ":remaining": {"N": str(72000 - seconds)},
                        },
                    }
                }
            )
        transactions.append(
            {
                "ConditionCheck": {
                    "TableName": self.table,
                    "Key": {
                        "key": {
                            "S": "completion#" + config["environment"] + "#" + config["session_id"]
                        }
                    },
                    "ConditionExpression": "attribute_not_exists(#key)",
                    "ExpressionAttributeNames": {"#key": "key"},
                }
            }
        )
        transactions.append(
            {
                "Put": {
                    "TableName": self.table,
                    "Item": {
                        "key": {"S": "active-session"},
                        "session_id": {"S": config["session_id"]},
                        "environment": {"S": config["environment"]},
                        "account_id": {"S": config["account_id"]},
                        "started_at": {"N": str(start_epoch)},
                        "reserved_seconds": {"N": str(reservation_seconds)},
                    },
                    "ConditionExpression": "attribute_not_exists(#key)",
                    "ExpressionAttributeNames": {"#key": "key"},
                }
            }
        )
        token = hashlib.sha256(
            f"{config['account_id']}:{config['environment']}:{config['session_id']}:{start_epoch}".encode()
        ).hexdigest()[:36]
        try:
            self.client.transact_write_items(TransactItems=transactions, ClientRequestToken=token)
        except Exception as exc:
            raise Refused(
                "Session admission failed; another session, a completed session ID or the monthly reservation cap may block it"
            ) from exc
        return {
            "admitted": True,
            "session_id": config["session_id"],
            "environment": config["environment"],
            "reserved_seconds": reservation_seconds,
            "months": month_reservations(start_epoch, reservation_seconds),
        }

    def assert_admitted(self, config):
        validate_config(config)
        self.assert_table_scope(config["account_id"], config["region"])
        item = self.client.get_item(
            TableName=self.table, Key={"key": {"S": "active-session"}}, ConsistentRead=True
        ).get("Item", {})
        if any(
            item.get(name, {}).get("S") != config[name]
            for name in ("session_id", "environment", "account_id")
        ):
            raise Refused("The requested environment/session has no matching global admission")
        return item

    def acquire(self, config, ttl=120):
        if not isinstance(ttl, int) or not 60 <= ttl <= 300:
            raise Refused(
                "Lifecycle lease duration must be between sixty and three hundred seconds"
            )
        self.assert_admitted(config)
        owner, instant = str(uuid4()), int(self.clock())
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"key": {"S": "lifecycle-lock"}},
                UpdateExpression="SET owner_id = :owner, expires_at = :expires, session_id = :session",
                ConditionExpression="attribute_not_exists(owner_id) OR expires_at < :now",
                ExpressionAttributeValues={
                    ":owner": {"S": owner},
                    ":expires": {"N": str(instant + ttl)},
                    ":session": {"S": config["session_id"]},
                    ":now": {"N": str(instant)},
                },
            )
        except Exception as exc:
            raise Refused("Another lifecycle operation holds the lease") from exc
        return Lease(self, config, owner, ttl)


class Lease:
    def __init__(self, store, config, owner, ttl):
        self.store, self.config, self.owner, self.ttl = store, config, owner, ttl
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def assert_owned(self):
        if self.lost.is_set():
            raise Refused("Lifecycle lease was lost")
        self.store.assert_admitted(self.config)
        item = self.store.client.get_item(
            TableName=self.store.table, Key={"key": {"S": "lifecycle-lock"}}, ConsistentRead=True
        ).get("Item", {})
        if (
            item.get("owner_id", {}).get("S") != self.owner
            or item.get("session_id", {}).get("S") != self.config["session_id"]
            or int(item.get("expires_at", {}).get("N", "0")) <= int(self.store.clock())
        ):
            self.lost.set()
            raise Refused("Lifecycle lease expired or ownership changed")

    def renew(self):
        self.assert_owned()
        instant = int(self.store.clock())
        try:
            self.store.client.update_item(
                TableName=self.store.table,
                Key={"key": {"S": "lifecycle-lock"}},
                UpdateExpression="SET expires_at = :expires",
                ConditionExpression="owner_id = :owner AND expires_at > :now",
                ExpressionAttributeValues={
                    ":owner": {"S": self.owner},
                    ":expires": {"N": str(instant + self.ttl)},
                    ":now": {"N": str(instant)},
                },
            )
        except Exception as exc:
            self.lost.set()
            raise Refused("Lifecycle lease renewal failed") from exc

    def __enter__(self):
        self.assert_owned()

        def heartbeat():
            while not self.stop.wait(self.ttl / 3):
                try:
                    self.renew()
                except Exception:
                    self.lost.set()
                    return

        self.thread = threading.Thread(target=heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        try:
            self.store.client.update_item(
                TableName=self.store.table,
                Key={"key": {"S": "lifecycle-lock"}},
                UpdateExpression="REMOVE owner_id, expires_at, session_id",
                ConditionExpression="owner_id = :owner AND session_id = :session",
                ExpressionAttributeValues={
                    ":owner": {"S": self.owner},
                    ":session": {"S": self.config["session_id"]},
                },
            )
        except Exception:
            # Never remove another holder's lock or erase persistent session markers.
            self.lost.set()
