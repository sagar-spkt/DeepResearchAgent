import json
import subprocess
import unittest

from scripts.staging_lifecycle_lease import LeaseError, acquire, assert_owner, renew


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

        acquire("workflow-owner", now=1000, runner=runner)
        update = next(call for call in calls if "update-item" in call)
        self.assertEqual(
            "attribute_not_exists(owner_id) OR expires_at < :now",
            update[update.index("--condition-expression") + 1],
        )
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual("workflow-owner", values[":owner"]["S"])
        self.assertEqual("1120", values[":expires"]["N"])

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
            acquire("workflow-owner", now=1000, runner=runner)
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

        acquire("near-deadline", now=15350, runner=runner)
        update = next(call for call in calls if "update-item" in call)
        values = json.loads(update[update.index("--expression-attribute-values") + 1])
        self.assertEqual("15400", values[":expires"]["N"])
        calls.clear()
        with self.assertRaisesRegex(LeaseError, "deadline has passed"):
            acquire("too-late", now=15400, runner=runner)
        self.assertFalse(any("update-item" in call for call in calls))
        calls.clear()
        with self.assertRaisesRegex(LeaseError, "deadline has passed"):
            renew("near-deadline", now=15400, runner=runner)
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

        assert_owner("workflow-owner", now=1100, runner=runner)
        renew("workflow-owner", now=1100, runner=runner)
        update = calls[-1]
        condition = update[update.index("--condition-expression") + 1]
        self.assertIn("owner_id = :owner", condition)
        self.assertIn("session_id = :session", condition)
        self.assertIn("expires_at > :now", condition)


if __name__ == "__main__":
    unittest.main()
