"""Deterministic checkpoint scheduling without an engine or wall-clock waits."""

import unittest

from backend.checkpoint import EARLY_WINDOW, CheckpointPolicy


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class CheckpointPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.policy = CheckpointPolicy(clock=self.clock)

    def test_new_backend_waits_one_interval_then_quiesces_until_queue_drains(self):
        self.assertFalse(self.policy.due())
        self.assertFalse(self.policy.status["overdue"])
        self.assertFalse(self.policy.admission_blocked)
        self.clock.advance(29.9)
        self.assertFalse(self.policy.due())
        self.clock.advance(0.1)
        self.assertTrue(self.policy.due())
        self.assertTrue(self.policy.quiescing)
        self.assertFalse(self.policy.should_create(2))
        self.assertTrue(self.policy.should_create(0))

    def test_create_receipt_is_not_durability_and_new_completed_id_releases(self):
        self.policy.observe({"status": "completed", "checkpoint_id": "save-0"})
        self.clock.advance(30)
        self.policy.due()
        self.policy.created("queued")
        self.assertTrue(self.policy.saving)
        self.assertFalse(self.policy.should_create(0))
        # A stale status response cannot claim the newly requested save finished.
        self.policy.observe({"status": "completed", "checkpoint_id": "save-0"})
        self.assertTrue(self.policy.quiescing)
        self.policy.observe({"status": "saving", "checkpoint_id": "save-0"})
        self.clock.advance(5)
        self.policy.observe({"status": "completed", "checkpoint_id": "save-1"})
        self.assertFalse(self.policy.quiescing)
        self.assertFalse(self.policy.saving)
        self.assertEqual(self.policy.status["last_success_age"], 0)
        self.clock.advance(29)
        self.assertFalse(self.policy.due())

    def test_busy_and_failed_status_retry_with_capped_backoff(self):
        self.clock.advance(30)
        self.policy.due()
        self.policy.created("rejected")
        self.policy.observe({"status": "failed", "checkpoint_id": ""})
        self.assertFalse(self.policy.should_create(0))
        self.clock.advance(1)
        self.assertTrue(self.policy.should_create(0))
        self.policy.created("rejected")
        self.clock.advance(1)
        self.assertFalse(self.policy.should_create(0))
        self.clock.advance(1)
        self.assertTrue(self.policy.should_create(0))
        self.policy.created("queued")
        self.policy.observe({"status": "failed", "checkpoint_id": "save-0"})
        self.clock.advance(4)
        self.assertTrue(self.policy.should_create(0))
        # Polling the same failed status must not multiply the delay.
        self.policy.observe({"status": "failed", "checkpoint_id": "save-0"})
        self.assertTrue(self.policy.should_create(0))

    def test_rpc_failure_keeps_quiescence_and_never_claims_success(self):
        self.clock.advance(30)
        self.policy.due()
        self.policy.failed()
        self.assertTrue(self.policy.quiescing)
        self.assertFalse(self.policy.should_create(0))
        self.clock.advance(1)
        self.assertTrue(self.policy.should_create(0))
        self.assertIsNone(self.policy.status["last_success_age"])

    def test_old_success_event_does_not_reset_overdue_timer(self):
        self.policy.observe({"status": "completed", "checkpoint_id": "save-1"})
        self.clock.advance(30)
        self.assertTrue(self.policy.due())
        self.policy.observe({"status": "completed", "checkpoint_id": "save-1"})
        self.assertTrue(self.policy.status["overdue"])
        self.assertTrue(self.policy.quiescing)

    def test_admission_grace_is_based_on_last_real_success(self):
        self.clock.advance(119)
        self.assertFalse(self.policy.admission_blocked)
        self.clock.advance(1)
        self.assertTrue(self.policy.admission_blocked)
        self.policy.observe({"status": "completed", "checkpoint_id": "save-1"})
        self.assertFalse(self.policy.admission_blocked)
        self.clock.advance(120)
        self.assertTrue(self.policy.admission_blocked)

    def test_early_window_triggers_before_interval(self):
        self.assertFalse(self.policy.due(EARLY_WINDOW - 1))
        self.assertTrue(self.policy.due(EARLY_WINDOW))
        self.assertTrue(self.policy.should_create(0))

    def test_malformed_counters_and_statuses_do_not_trigger_save(self):
        with self.assertRaises(ValueError):
            self.policy.due(-1)
        with self.assertRaises(ValueError):
            self.policy.should_create(-1)
        with self.assertRaises(ValueError):
            self.policy.observe({"status": "unknown"})
        self.assertFalse(self.policy.quiescing)


if __name__ == "__main__":
    unittest.main()
