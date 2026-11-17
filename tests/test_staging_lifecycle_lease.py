import json
import subprocess
import unittest

from scripts.staging_lifecycle_lease import LeaseError, acquire, assert_owner, release, renew


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
                return subprocess.CompletedProcess(argv, 0, json.dumps({"Item": items[key]}), "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

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
        self.assertEqual("REMOVE owner_id, expires_at, session_id", update[update.index("--update-expression") + 1])
        self.assertEqual("owner_id = :owner AND session_id = :session", update[update.index("--condition-expression") + 1])
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual("workflow-owner", values[":owner"]["S"])
        self.assertEqual("stg-20261116", values[":session"]["S"])

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
