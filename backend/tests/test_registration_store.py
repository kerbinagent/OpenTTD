"""Durability and capability gates for company registration setup."""

import hashlib
import os
import tempfile
import threading
import unittest

from backend.errors import ServiceError
from backend.json_tools import dumps
from backend.store import Store


class RegistrationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "world.sqlite")
        self.store = Store(self.path, world_id="world")
        self.store.ensure_world("world", "epoch")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    @staticmethod
    def claim(secret):
        return hashlib.sha256(secret.encode()).hexdigest()

    def prepare(self, secret="claim-one", mode="text", slots=("worker-a",)):
        return self.store.prepare_registration("world", "epoch", self.claim(secret), mode, slots)

    def allocation(self, registration):
        return self.store.get_internal("world", registration["allocation_request_id"], "company.register")

    def complete_allocation(self, registration, company=3, result=None):
        record = self.allocation(registration)
        actor = self.store.get_principal(record["run_id"])
        self.store.update_request(actor, record["request_id"], "queued", result={"engine_instance_id": "engine"})
        result = result or {
            "company_id": company,
            "owner": {"identity_id": registration["identity_id"], "run_id": registration["run_id"],
                      "mode": registration["mode"], "company_id": company},
            "owner_bound": True,
            "checkpoint_state": "uncheckpointed",
        }
        return self.store.update_request(actor, record["request_id"], "completed", result=result)

    def checkpoint(self, *records):
        self.store.apply_checkpoint("world", {
            "world_epoch": "epoch", "save_id": "save-" + str(self.store.max_request_sequence("world")),
            "retired_through": 0, "tick": 10,
        }, [{"request_sequence": record["request_sequence"], "status": record["status"]}
            for record in records])

    @staticmethod
    def handle(number=5, generation=1):
        return {"id": number, "generation": generation, "engine_instance_id": "engine"}

    def grant(self, principal, handle=None):
        registration = self.store.registration_for_run(principal.run_id)
        if registration["worker_public_key"] is None:
            self.store.bind_registration_worker_key("world", registration["claim_hash"], "a" * 64)
        handle = handle or self.handle()
        request_id = self.store.registration_worker_request_id(principal, handle)
        payload = {"action": {"name": "company.participant.authorize", "parameters": {
            "participant": handle, "allowed": True}}, "preconditions": {}}
        return request_id, payload

    def test_world_initialization_has_no_fake_company_or_credential(self):
        self.store.ensure_world("world", "epoch")
        self.assertEqual(self.store.principals_for_world("world"), [])
        self.assertEqual(self.store.max_request_sequence("world"), 0)
        self.assertEqual(self.store.registrations("world"), [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM credentials").fetchone()[0], 0)
        self.assert_code("epoch_conflict", lambda: self.store.ensure_world("world", "other"))
        self.assert_code("world_scope", lambda: self.store.ensure_world("other", "epoch"))

    def test_prepare_uses_global_journal_and_retry_survives_restart(self):
        first = self.prepare()
        record = self.allocation(first)
        self.assertEqual(record["request_sequence"], 1)
        self.assertEqual(record["company_id"], 255)
        self.assertEqual(record["mode"], "internal")
        self.assertEqual(record["payload"], {"rpc_type": "company.register", "owner": {
            "identity_id": first["identity_id"], "run_id": first["run_id"], "mode": "text"}})
        self.assertTrue(first["run_id"].startswith("registration-"))
        self.assertEqual(first["registration_id"], first["run_id"])
        self.assertIsNone(first["company_id"])
        self.assertIsNone(first["worker_slot"])
        self.assertIsNone(first["worker_request_id"])
        self.assertFalse(first["ready"])
        chat = self.store.reserve_internal("world", "hello", {
            "rpc_type": "chat.publish", "message_id": "hello", "delivery": {"body": "Hello"}})
        self.assertEqual(chat["request_sequence"], 2)
        self.store.close()
        self.store = Store(self.path, world_id="world")
        self.assertEqual(self.prepare(), first)
        self.assertEqual(self.store.registration_for_run(first["run_id"]), first)
        self.assertEqual(self.store.max_request_sequence("world"), 2)
        self.assert_code("join_conflict", lambda: self.prepare(mode="visual"))
        self.assert_code("epoch_conflict", lambda: self.store.prepare_registration(
            "world", "wrong", self.claim("claim-one"), "text", ()))

    def test_visual_capacity_is_reserved_before_native_allocation(self):
        first = self.prepare(mode="visual")
        self.assertEqual(first["worker_slot"], "worker-a")
        self.assert_code("visual_capacity", lambda: self.prepare("claim-two", "visual"))
        self.assertIsNone(self.store.registration("world", self.claim("claim-two")))
        self.assertEqual(self.store.max_request_sequence("world"), 1)
        self.assertEqual(self.prepare("claim-two")["allocation_request_sequence"], 2)
        self.assert_code("registration_worker_unavailable", lambda: self.prepare(mode="visual", slots=("other",)))

    def test_worker_key_is_pinned_across_restart_and_never_rotates(self):
        registration = self.prepare(mode="visual")
        self.assertIsNone(registration["worker_public_key"])
        key = "a" * 64
        bound = self.store.bind_registration_worker_key("world", registration["claim_hash"], key)
        self.assertEqual(bound["worker_public_key"], key)
        self.assertEqual(self.store.bind_registration_worker_key("world", registration["claim_hash"], key), bound)
        self.store.close()
        self.store = Store(self.path, world_id="world")
        self.assertEqual(self.prepare(mode="visual")["worker_public_key"], key)
        self.assert_code("join_worker_changed", lambda: self.store.bind_registration_worker_key(
            "world", registration["claim_hash"], "b" * 64))
        for invalid in ("A" * 64, "a" * 63, "z" * 64, None):
            self.assert_code("invalid_worker_key", lambda invalid=invalid: self.store.bind_registration_worker_key(
                "world", registration["claim_hash"], invalid))
        text = self.prepare("text-claim")
        self.assert_code("forbidden", lambda: self.store.bind_registration_worker_key(
            "world", text["claim_hash"], key))

    def test_worker_grant_requires_key_pin_first(self):
        registration = self.prepare(mode="visual")
        self.complete_allocation(registration)
        principal = self.store.provisional_registration("world", registration["claim_hash"], 3)
        participant = self.handle()
        request_id = self.store.registration_worker_request_id(principal, participant)
        payload = {"action": {"name": "company.participant.authorize", "parameters": {
            "participant": participant, "allowed": True}}, "preconditions": {}}
        self.assert_code("forbidden", lambda: self.store.accept_registration_setup(principal, request_id, payload))
        self.assertEqual(self.store.max_request_sequence("world"), 1)
        self.store.bind_registration_worker_key("world", registration["claim_hash"], "a" * 64)
        self.assertTrue(self.store.accept_registration_setup(principal, request_id, payload)[1])

    def test_outbox_capacity_rolls_back_slot_and_setup_reservation(self):
        self.store.max_pending_world = 1
        first = self.prepare()
        self.assert_code("world_capacity", lambda: self.prepare("claim-two", "visual"))
        self.assertIsNone(self.store.registration("world", self.claim("claim-two")))
        record = self.allocation(first)
        self.store.update_request(self.store.get_principal(record["run_id"]), record["request_id"],
                                  "rejected", error={"code": "company_limit"})
        second = self.prepare("claim-two", "visual")
        self.assertEqual(second["worker_slot"], "worker-a")
        self.assertEqual(second["allocation_request_sequence"], 2)

    def test_concurrent_visual_claims_cannot_share_slot(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def register(secret):
            barrier.wait()
            try:
                outcomes.append(self.prepare(secret, "visual"))
            except ServiceError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=register, args=(secret,)) for secret in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(isinstance(outcome, dict) for outcome in outcomes), 1)
        self.assertIn("visual_capacity", outcomes)
        self.assertEqual(self.store.max_request_sequence("world"), 1)

    def test_creation_receipt_required_before_provisional_owner(self):
        registration = self.prepare()
        self.assert_code("join_not_ready", lambda: self.store.provisional_registration(
            "world", registration["claim_hash"], 3))
        self.complete_allocation(registration)
        self.assert_code("join_not_ready", lambda: self.store.provisional_registration(
            "world", registration["claim_hash"], 4))
        principal = self.store.provisional_registration("world", registration["claim_hash"], 3)
        self.assertEqual(principal.run_id, registration["run_id"])
        self.assertEqual(principal, self.store.provisional_registration("world", registration["claim_hash"], 3))
        self.assert_code("join_conflict", lambda: self.store.provisional_registration(
            "world", registration["claim_hash"], 4))
        self.assert_code("forbidden", lambda: self.store.accept_request(principal, "gameplay", {"action": {}}))
        self.assert_code("unauthorized", lambda: self.store.authenticate("claim-one"))

    def test_wrong_owner_receipt_never_publishes_owner(self):
        registration = self.prepare()
        self.complete_allocation(registration, result={"company_id": 3, "owner_bound": True, "owner": {
            "company_id": 3, "identity_id": "somebody-else", "run_id": registration["run_id"], "mode": "text"}})
        self.assert_code("join_not_ready", lambda: self.store.provisional_registration(
            "world", registration["claim_hash"], 3))
        self.assertIsNone(self.store.get_principal(registration["run_id"]))

    def test_text_ready_requires_checkpoint_then_activates_same_hashed_secret_once(self):
        registration = self.prepare()
        allocation = self.complete_allocation(registration)
        principal = self.store.provisional_registration("world", registration["claim_hash"], 3)
        self.assert_code("join_not_ready", lambda: self.store.finalize_registration("world", registration["claim_hash"]))
        self.checkpoint(allocation)
        ready = self.store.finalize_registration("world", registration["claim_hash"])
        self.assertTrue(ready["ready"])
        self.assertEqual(self.store.authenticate("claim-one"), principal)
        self.assertEqual(self.store.agent_join_claim("world", registration["claim_hash"]),
                         {"mode": "text", "run_id": principal.run_id})
        self.assertEqual(self.store.finalize_registration("world", registration["claim_hash"]), ready)
        self.assertEqual(self.store.connection.execute("SELECT token_hash FROM credentials").fetchone()[0],
                         registration["claim_hash"])
        self.store.close()
        self.store = Store(self.path, world_id="world")
        self.assertEqual(self.store.authenticate("claim-one"), principal)
        self.assertEqual(self.prepare(), ready)
        for path in (self.path, self.path + "-wal"):
            if os.path.exists(path):
                with open(path, "rb") as stream:
                    self.assertNotIn(b"claim-one", stream.read())

    def test_visual_ready_requires_checkpointed_current_worker_grant(self):
        registration = self.prepare(mode="visual")
        allocation = self.complete_allocation(registration)
        principal = self.store.provisional_registration("world", registration["claim_hash"], 3)
        self.checkpoint(allocation)
        self.assert_code("join_not_ready", lambda: self.store.finalize_registration("world", registration["claim_hash"]))
        request_id, payload = self.grant(principal)
        grant, accepted = self.store.accept_registration_setup(principal, request_id, payload)
        self.assertTrue(accepted)
        self.assertEqual(grant["request_sequence"], 2)
        self.assertEqual(self.store.registration_for_run(principal.run_id)["worker_request_id"], request_id)
        self.assertEqual(self.store.accept_registration_setup(principal, request_id, payload), (grant, False))
        self.store.update_request(principal, request_id, "queued")
        grant = self.store.update_request(principal, request_id, "completed", result={"success": True})
        self.assert_code("join_not_ready", lambda: self.store.finalize_registration("world", registration["claim_hash"]))
        self.checkpoint(allocation, grant)
        self.assertTrue(self.store.finalize_registration("world", registration["claim_hash"])["ready"])
        self.assertEqual(self.store.authenticate("claim-one"), principal)
        self.assert_code("forbidden", lambda: self.store.accept_registration_setup(principal, request_id, payload))

    def test_setup_is_narrow_and_cannot_change_pending_or_uncertain_handle(self):
        registration = self.prepare(mode="visual")
        self.complete_allocation(registration)
        principal = self.store.provisional_registration("world", registration["claim_hash"], 3)
        request_id, payload = self.grant(principal)
        self.assertEqual(request_id, "registration-worker:" + principal.run_id + ":" +
                         hashlib.sha256(dumps(self.handle()).encode()).hexdigest()[:16])
        bad_payloads = [
            {**payload, "extra": 1},
            {**payload, "preconditions": {"tick": 1}},
            {"action": {"name": "company.rename", "parameters": {"name": "Other"}}, "preconditions": {}},
            {"action": {"name": "company.participant.authorize", "parameters": {
                "participant": self.handle(), "allowed": False}}, "preconditions": {}},
        ]
        for invalid in bad_payloads:
            self.assert_code("invalid_payload", lambda invalid=invalid: self.store.accept_registration_setup(
                principal, request_id, invalid))
        self.assert_code("invalid_request_id", lambda: self.store.accept_registration_setup(principal, "arbitrary", payload))
        self.store.accept_registration_setup(principal, request_id, payload)
        replacement_id, replacement = self.grant(principal, self.handle(6))
        self.assert_code("join_not_ready", lambda: self.store.accept_registration_setup(principal, replacement_id, replacement))
        self.store.update_request(principal, request_id, "queued")
        self.store.update_request(principal, request_id, "uncertain")
        self.assert_code("join_not_ready", lambda: self.store.accept_registration_setup(principal, replacement_id, replacement))

    def test_definitive_stale_target_retries_new_handle_without_company_or_slot(self):
        registration = self.prepare(mode="visual")
        self.complete_allocation(registration)
        principal = self.store.provisional_registration("world", registration["claim_hash"], 3)
        request_id, payload = self.grant(principal)
        first, _ = self.store.accept_registration_setup(principal, request_id, payload)
        self.store.update_request(principal, request_id, "rejected", error={"code": "stale_target"})
        replacement_id, replacement = self.grant(principal, self.handle(6))
        second, accepted = self.store.accept_registration_setup(principal, replacement_id, replacement)
        self.assertTrue(accepted)
        self.assertEqual(second["request_sequence"], first["request_sequence"] + 1)
        current = self.store.registration_for_run(principal.run_id)
        self.assertEqual(current["worker_request_id"], replacement_id)
        self.assertEqual(current["worker_slot"], "worker-a")
        self.assertEqual(current["company_id"], 3)
        self.assertEqual(len(self.store.registrations("world")), 1)

    def test_owner_failure_cannot_rotate_grant_and_revocation_cannot_reactivate(self):
        registration = self.prepare(mode="visual")
        self.complete_allocation(registration)
        principal = self.store.provisional_registration("world", registration["claim_hash"], 3)
        request_id, payload = self.grant(principal)
        self.store.accept_registration_setup(principal, request_id, payload)
        self.store.update_request(principal, request_id, "rejected", error={"code": "stale_context"})
        replacement_id, replacement = self.grant(principal, self.handle(6))
        self.assert_code("join_not_ready", lambda: self.store.accept_registration_setup(principal, replacement_id, replacement))
        self.store.revoke_membership("world", principal.identity_id, principal.company_id)
        self.assert_code("unauthorized", lambda: self.store.provisional_registration("world", registration["claim_hash"], 3))
        self.assert_code("unauthorized", lambda: self.store.finalize_registration("world", registration["claim_hash"]))

    def test_input_and_internal_rpc_types_are_finite(self):
        for invalid in ("", "a" * 63, "A" * 64, "z" * 64, None):
            self.assert_code("invalid_claim", lambda invalid=invalid: self.store.prepare_registration(
                "world", "epoch", invalid, "text", ()))
        for invalid in (("duplicate", "duplicate"), ("",), ("a\n",), "worker"):
            self.assert_code("invalid_worker_slots", lambda invalid=invalid: self.prepare(slots=invalid))
        self.assert_code("invalid_mode", lambda: self.prepare(mode="human"))
        self.assert_code("invalid_payload", lambda: self.store.reserve_internal("world", "bad", {"rpc_type": "company.delete"}))
        self.assert_code("invalid_payload", lambda: self.store.get_internal("world", "bad", "company.delete"))
        self.assert_code("invalid_payload", lambda: self.store.reserve_internal("world", "bad", {
            "rpc_type": "company.register", "owner": {"identity_id": "x", "run_id": "unfenced", "mode": "text"}}))
        self.assert_code("invalid_identity", lambda: self.store.provision("world", "__backend_registration__", 1))


if __name__ == "__main__":
    unittest.main()
