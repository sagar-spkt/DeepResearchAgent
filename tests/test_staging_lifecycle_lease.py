import json
import subprocess
import unittest

from scripts.staging_lifecycle_lease import (
    LeaseError,
    acquire,
    assert_owner,
    acquire_finalizer,
    assert_finalizer_owner,
    deployment_marker_key,
    mark_deployment_started,
    release,
    renew_finalizer,
    renew,
)


class StagingLifecycleLeaseTests(unittest.TestCase):
    def test_acquire_requires_exact_active_staging_admission_and_conditional_lock(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "Item": {
                                "session_id": {"S": "stg-20261116"},
                                "environment": {"S": "staging"},
                                "account_id": {"S": "088759500358"},
                                "started_at": {"N": "1000"},
                            }
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        acquire("workflow-owner", "stg-20261116", now=1000, runner=runner)
        update = next(call for call in calls if "update-item" in call)
        self.assertEqual(
            "attribute_not_exists(owner_id) OR expires_at < :now",
            update[update.index("--condition-expression") + 1],
        )
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual("workflow-owner", values[":owner"]["S"])
        self.assertEqual("1120", values[":expires"]["N"])

    def test_acquire_accepts_empty_success_response_from_dynamodb_update(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "Item": {
                                "session_id": {"S": "stg-20261116"},
                                "environment": {"S": "staging"},
                                "account_id": {"S": "088759500358"},
                                "started_at": {"N": "1000"},
                            }
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        acquire("workflow-owner", "stg-20261116", now=1000, runner=runner)
        self.assertEqual(2, len(calls))
        self.assertIn("update-item", calls[1])

    def test_wrong_admission_fails_before_lock_mutation(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"Item": {"session_id": {"S": "other-session"}}}),
                "",
            )

        with self.assertRaisesRegex(LeaseError, "not the active admission"):
            acquire("workflow-owner", "stg-20261116", now=1000, runner=runner)
        self.assertEqual(1, len(calls))
        self.assertIn("get-item", calls[0])

    def test_lease_cannot_start_past_four_hour_runtime_deadline(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "Item": {
                                "session_id": {"S": "stg-20261116"},
                                "environment": {"S": "staging"},
                                "account_id": {"S": "088759500358"},
                                "started_at": {"N": "1000"},
                            }
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        acquire("near-deadline", "stg-20261116", now=15350, runner=runner)
        update = next(call for call in calls if "update-item" in call)
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual("15400", values[":expires"]["N"])
        calls.clear()
        with self.assertRaisesRegex(LeaseError, "deadline has passed"):
            acquire("too-late", "stg-20261116", now=15400, runner=runner)
        self.assertFalse(any("update-item" in call for call in calls))
        calls.clear()
        with self.assertRaisesRegex(LeaseError, "deadline has passed"):
            renew("near-deadline", "stg-20261116", now=15400, runner=runner)
        self.assertFalse(any("update-item" in call for call in calls))

    def test_finalizer_lease_only_runs_after_four_hours_before_reservation_expiry(self):
        session_id = "stg-20261117"
        items = {
            "active-session": {
                "session_id": {"S": session_id},
                "environment": {"S": "staging"},
                "account_id": {"S": "088759500358"},
                "started_at": {"N": "1000"},
            },
            "lifecycle-lock": {},
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                key = json.loads(argv[argv.index("--key") + 1])["key"]["S"]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Item": items.get(key, {})}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        acquire_finalizer("finalizer-owner", session_id, now=15400, runner=runner)
        update = next(call for call in calls if "update-item" in call)
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual("15520", values[":expires"]["N"])

        items["lifecycle-lock"] = {
            "owner_id": {"S": "finalizer-owner"},
            "session_id": {"S": session_id},
            "expires_at": {"N": "15520"},
        }
        assert_finalizer_owner("finalizer-owner", session_id, now=15500, runner=runner)
        renew_finalizer("finalizer-owner", session_id, now=15500, runner=runner)
        self.assertEqual("15620", json.loads(calls[-1][calls[-1].index("--expression-attribute-values") + 1])[":expires"]["N"])

    def test_finalizer_lease_rejects_early_expired_or_deployed_sessions(self):
        session_id = "stg-20261117"
        items = {
            "active-session": {
                "session_id": {"S": session_id},
                "environment": {"S": "staging"},
                "account_id": {"S": "088759500358"},
                "started_at": {"N": "1000"},
            }
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                key = json.loads(argv[argv.index("--key") + 1])["key"]["S"]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Item": items.get(key, {})}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(LeaseError, "only after the runtime deadline"):
            acquire_finalizer("owner", session_id, now=15399, runner=runner)
        with self.assertRaisesRegex(LeaseError, "reservation has expired"):
            acquire_finalizer("owner", session_id, now=22600, runner=runner)
        items[deployment_marker_key(session_id)] = {
            "key": {"S": deployment_marker_key(session_id)},
            "session_id": {"S": session_id},
            "environment": {"S": "staging"},
        }
        with self.assertRaisesRegex(LeaseError, "deployment marker exists"):
            acquire_finalizer("owner", session_id, now=15400, runner=runner)
        self.assertFalse(any("update-item" in call for call in calls))

    def test_assert_and_renew_are_bound_to_owner_and_session(self):
        items = {
            "active-session": {
                "session_id": {"S": "stg-20261116"},
                "environment": {"S": "staging"},
                "account_id": {"S": "088759500358"},
                "started_at": {"N": "1000"},
            },
            "lifecycle-lock": {
                "owner_id": {"S": "workflow-owner"},
                "session_id": {"S": "stg-20261116"},
                "expires_at": {"N": "1200"},
            },
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                key = json.loads(argv[argv.index("--key") + 1])["key"]["S"]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Item": items.get(key, {})}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        assert_owner("workflow-owner", "stg-20261116", now=1100, runner=runner)
        renew("workflow-owner", "stg-20261116", now=1100, runner=runner)
        update = calls[-1]
        condition = update[update.index("--condition-expression") + 1]
        self.assertIn("owner_id = :owner", condition)
        self.assertIn("session_id = :session", condition)
        self.assertIn("expires_at > :now", condition)

    def test_release_is_conditional_on_exact_owner_and_session(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        release("workflow-owner", "stg-20261116", runner=runner)
        update = calls[0]
        self.assertIn("update-item", update)
        expression = update[update.index("--update-expression") + 1]
        self.assertEqual("REMOVE owner_id, expires_at, session_id", expression)
        self.assertNotIn("app_writer_sessions", expression)
        self.assertEqual("owner_id = :owner AND session_id = :session", update[update.index("--condition-expression") + 1])
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual("workflow-owner", values[":owner"]["S"])
        self.assertEqual("stg-20261116", values[":session"]["S"])

    def test_deployment_marker_is_added_only_under_the_active_session_lease(self):
        session_id = "stg-20261117"
        items = {
            "active-session": {
                "session_id": {"S": session_id},
                "environment": {"S": "staging"},
                "account_id": {"S": "088759500358"},
                "started_at": {"N": "1000"},
            },
            "lifecycle-lock": {
                "owner_id": {"S": "workflow-owner"},
                "session_id": {"S": session_id},
                "expires_at": {"N": "1200"},
            },
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                key = json.loads(argv[argv.index("--key") + 1])["key"]["S"]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Item": items.get(key, {})}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        mark_deployment_started(
            "workflow-owner", session_id, "a" * 40, "12345", now=1100, runner=runner
        )

        transaction = next(call for call in calls if "transact-write-items" in call)
        actions = json.loads(transaction[transaction.index("--transact-items") + 1])
        self.assertEqual(["ConditionCheck", "Put"], [next(iter(action)) for action in actions])
        lease_check = actions[0]["ConditionCheck"]
        self.assertEqual("lifecycle-lock", lease_check["Key"]["key"]["S"])
        self.assertEqual(
            "owner_id = :owner AND session_id = :session AND expires_at > :now AND expires_at <= :deadline",
            lease_check["ConditionExpression"],
        )
        lease_values = lease_check["ExpressionAttributeValues"]
        self.assertEqual("workflow-owner", lease_values[":owner"]["S"])
        self.assertEqual(session_id, lease_values[":session"]["S"])
        self.assertEqual("1100", lease_values[":now"]["N"])
        self.assertEqual("15400", lease_values[":deadline"]["N"])
        marker_put = actions[1]["Put"]
        marker = marker_put["Item"]
        self.assertEqual(deployment_marker_key(session_id), marker["key"]["S"])
        self.assertEqual(session_id, marker["session_id"]["S"])
        self.assertEqual("staging", marker["environment"]["S"])
        self.assertEqual("1100", marker["started_at"]["N"])
        self.assertEqual("a" * 40, marker["commit_sha"]["S"])
        self.assertEqual("12345", marker["workflow_run_id"]["N"])
        self.assertEqual("attribute_not_exists(#key)", marker_put["ConditionExpression"])

    def test_existing_valid_deployment_marker_is_idempotent(self):
        session_id = "stg-20261117"
        items = {
            "active-session": {
                "session_id": {"S": session_id},
                "environment": {"S": "staging"},
                "account_id": {"S": "088759500358"},
                "started_at": {"N": "1000"},
            },
            "lifecycle-lock": {
                "owner_id": {"S": "workflow-owner"},
                "session_id": {"S": session_id},
                "expires_at": {"N": "1200"},
            },
            deployment_marker_key(session_id): {
                "key": {"S": deployment_marker_key(session_id)},
                "session_id": {"S": session_id},
                "environment": {"S": "staging"},
                "started_at": {"N": "1050"},
                "commit_sha": {"S": "b" * 40},
                "workflow_run_id": {"N": "10000"},
            },
        }
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                key = json.loads(argv[argv.index("--key") + 1])["key"]["S"]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Item": items.get(key, {})}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        mark_deployment_started(
            "workflow-owner", session_id, "a" * 40, "12345", now=1100, runner=runner
        )

        self.assertFalse(any("transact-write-items" in call for call in calls))
        self.assertEqual("b" * 40, items[deployment_marker_key(session_id)]["commit_sha"]["S"])

    def test_deployment_marker_refuses_when_lease_is_not_owned(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                key = json.loads(argv[argv.index("--key") + 1])["key"]["S"]
                value = (
                    {"session_id": {"S": "stg-20261117"}, "environment": {"S": "staging"}, "account_id": {"S": "088759500358"}, "started_at": {"N": "1000"}}
                    if key == "active-session"
                    else {"owner_id": {"S": "other-owner"}, "session_id": {"S": "stg-20261117"}, "expires_at": {"N": "1200"}}
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Item": value}), "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with self.assertRaisesRegex(LeaseError, "expired or changed owner"):
            mark_deployment_started(
                "workflow-owner", "stg-20261117", "a" * 40, "12345", now=1100, runner=runner
            )
        self.assertFalse(any("put-item" in call for call in calls))

    def test_session_id_is_validated_before_any_cloud_call(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            raise AssertionError("invalid session IDs must be rejected before AWS")

        with self.assertRaisesRegex(LeaseError, "session ID is invalid"):
            acquire("workflow-owner", "stg;aws", runner=runner)
        self.assertEqual([], calls)

    def test_lease_uses_the_explicit_current_session_id(self):
        calls = []
        session_id = "stg-20261117"

        def runner(argv, **kwargs):
            calls.append(argv)
            if "get-item" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "Item": {
                                "session_id": {"S": session_id},
                                "environment": {"S": "staging"},
                                "account_id": {"S": "088759500358"},
                                "started_at": {"N": "1000"},
                            }
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        acquire("workflow-owner", session_id, now=1000, runner=runner)
        update = next(call for call in calls if "update-item" in call)
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual(session_id, values[":session"]["S"])


if __name__ == "__main__":
    unittest.main()
