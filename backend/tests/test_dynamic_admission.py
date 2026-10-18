"""Dynamic creation uses the ordinary journal and save-bound owner receipts.

The fake models separately executed and checkpointed effects, so returning from
an RPC, publishing a save, and activating a bearer cannot collapse into one step.
Real native allocation/incarnation behavior is covered by the native smoke run.
"""

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http
from backend.admission import DynamicAgentAdmission
from backend.errors import ServiceError
from backend.service import Service
from backend.store import Store
from backend.tests.test_service import FakeEngine


class RegistrationEngine(FakeEngine):
    """Small authoritative ledger with explicit checkpoint publication."""

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.company_ids = iter((7, 2, 11, 0, 5, 9, 1, 4, 6, 8, 10, 12, 13, 14, 3))
        self.owners = {}
        self.ledger = {}
        self.checkpoint = {"status": "idle", "checkpoint_id": "", "retired_through": 0}
        self.retired = 0
        self.save_number = 0
        self.hold_grants = False
        self.reject_attach = False
        self.lose_registration_reply = False
        self.lose_grant_reply = False
        self.allowed_keys = set()
        self.attached = {}
        self.bare_roster = False
        self.owner_error = None
        self.registration_capability = True

    def valid_hello_reply(self, world_id):
        reply = super().valid_hello_reply(world_id)
        if self.registration_capability:
            reply["result"]["company_registration"] = {"version": 1}
        return reply

    @staticmethod
    def owner(envelope):
        return envelope.get("owner", envelope.get("actor"))

    def finish_checkpoint(self):
        """Publish the same effects and terminal receipts in one verified save."""
        self.save_number += 1
        for receipt in self.ledger.values():
            if receipt["status"] in ("completed", "rejected"):
                receipt["result"]["checkpoint_state"] = "checkpointed"
        self.checkpoint = {"status": "completed", "checkpoint_id": f"save-{self.save_number}",
                           "saved_tick": 100 + self.save_number, "save_checksum": f"digest-{self.save_number}",
                           "retired_through": self.retired, "checkpoint_retired_through": self.retired}

    def finish_grant(self, sequence, *, error=None):
        receipt = self.ledger[sequence]
        receipt["status"] = "rejected" if error else "completed"
        receipt["result"]["success"] = error is None
        if error:
            receipt["error"] = {"code": error}
        else:
            self.allowed_keys.add(receipt["worker_key"])

    def _receipt(self, envelope, status="completed", result=None):
        return {"status": status, "tick": 12,
                "result": {"request_id": envelope["request_id"],
                           "request_sequence": envelope["request_sequence"],
                           "checkpoint_state": "live", **(result or {})}}

    def call(self, envelope):
        if self.store.connection.in_transaction:
            raise AssertionError("Native RPC inside SQLite transaction")
        kind = envelope["type"]
        if kind not in {"company.register", "session.bind", "action.submit", "action.status",
                        "recovery.reconcile", "renderer.attach", "checkpoint.create", "checkpoint.status",
                        "action.ack", "chat.pending", "events.poll", "recovery.finish", "chat.roster"}:
            return super().call(envelope)
        self.calls.append(copy.deepcopy(envelope))
        if envelope["world_id"] in self.unavailable:
            raise ServiceError("engine_unavailable", status=503)
        if kind == "company.register":
            if "actor" in envelope:
                raise AssertionError("Registration must use the private actorless RPC")
            sequence = envelope["request_sequence"]
            if sequence not in self.ledger:
                company = next(self.company_ids)
                owner = dict(envelope["owner"], company_id=company)
                self.owners[company] = owner
                self.ledger[sequence] = self._receipt(envelope, result={
                    "company_id": company, "owner": owner, "owner_bound": True})
                self.ledger[sequence]["registration_owner"] = copy.deepcopy(envelope["owner"])
            if self.lose_registration_reply:
                self.lose_registration_reply = False
                raise ServiceError("engine_unavailable", status=503)
            return copy.deepcopy(self.ledger[sequence])
        if kind == "session.bind":
            if self.owner_error is not None:
                return {"status": "rejected", "error": {"code": self.owner_error}}
            owner = envelope["actor"]
            if self.owners.get(owner["company_id"]) != owner:
                return {"status": "rejected", "error": {"code": "company_owner_mismatch"}}
            return {"status": "completed", "result": {"bound": True}}
        if kind == "action.submit":
            sequence = envelope["request_sequence"]
            if sequence not in self.ledger:
                action = envelope["action"]
                if action["name"] != "company.participant.authorize":
                    raise AssertionError("Only the fixed worker grant belongs in this fixture")
                if self.owners.get(envelope["actor"]["company_id"]) != envelope["actor"]:
                    receipt = self._receipt(envelope, status="rejected", result={"success": False})
                    receipt["error"] = {"code": "company_owner_mismatch"}
                else:
                    handle = action["parameters"]["participant"]
                    matches = [row for row in self.roster_rows if row["native_handle"] == handle]
                    receipt = self._receipt(envelope, status="queued" if self.hold_grants else "completed",
                                            result={"success": True})
                    if len(matches) != 1:
                        receipt["status"] = "rejected"
                        receipt["error"] = {"code": "stale_target"}
                    else:
                        receipt["worker_key"] = matches[0]["public_key"].lower()
                        if not self.hold_grants:
                            self.allowed_keys.add(receipt["worker_key"])
                self.ledger[sequence] = receipt
            if self.lose_grant_reply:
                self.lose_grant_reply = False
                raise ServiceError("engine_unavailable", status=503)
            return copy.deepcopy(self.ledger[sequence])
        if kind in ("action.status", "recovery.reconcile"):
            receipt = self.ledger.get(envelope["request_sequence"])
            if receipt is None:
                return {"status": "uncertain", "result": {
                    "request_id": envelope["request_id"], "request_sequence": envelope["request_sequence"]}}
            if "registration_owner" in receipt:
                if (envelope.get("request_type") != "company.register" or "actor" in envelope
                        or envelope.get("owner") != receipt["registration_owner"]):
                    raise AssertionError("Registration recovery must preserve the exact owner and type")
            return copy.deepcopy(receipt)
        if kind == "renderer.attach":
            owner, key = envelope["owner"], envelope["native_public_key"]
            if (self.reject_attach or self.owners.get(owner["company_id"]) != owner
                    or key not in self.allowed_keys):
                return {"status": "rejected"}
            matches = [row for row in self.roster_rows if row["public_key"].lower() == key]
            if len(matches) != 1:
                return {"status": "rejected"}
            self.attached[key] = copy.deepcopy(owner)
            return {"status": "completed", "result": {"attached": True}}
        if kind == "checkpoint.create":
            return {"status": "queued"}
        if kind == "checkpoint.status":
            return {"status": "completed", "result": {
                "checkpoint": copy.deepcopy(self.checkpoint), "retired_through": self.retired,
                "load_verified": True}}
        if kind == "action.ack":
            self.retired = envelope["through_sequence"]
            return {"status": "completed", "result": {"retired_through": self.retired}}
        if kind == "chat.pending":
            return {"status": "completed", "result": {"messages": []}}
        if kind == "chat.roster":
            clients = copy.deepcopy(self.roster_rows)
            return {"status": "completed", "engine_instance_id": self.instances.get("world", "boot"),
                    "result": clients if self.bare_roster else {"clients": clients}}
        if kind == "recovery.finish":
            self.recovery_required = False
            return {"status": "completed", "result": {}}
        if kind == "events.poll":
            return {"status": "completed", "engine_instance_id": self.instances.get("world", "boot"), "result": {
                "events": [], "cursor": envelope["after"], "gap": False, "oldest_event_id": 1}}
        raise AssertionError(kind)


class RegistrationRenderer:
    """Immutable trusted slots and a private-hello key, separate from roster."""

    key = "a" * 64

    def __init__(self, engine, slots=("capture-1",)):
        self.engine, self.slots = engine, slots
        self.assignments = {}
        self.checked = []
        self.observed = []

    def worker_slots(self):
        return self.slots

    def assign_slot(self, slot, principal):
        if slot not in self.slots or self.assignments.get(slot, principal) != principal:
            raise ServiceError("renderer_unavailable", status=503)
        self.assignments[slot] = principal

    def worker_key(self, slot):
        if slot not in self.assignments:
            raise AssertionError("Private hello must follow immutable slot assignment")
        return self.key

    def managed_native_worker_keys(self):
        return {self.key}

    verified_native_worker_keys = managed_native_worker_keys

    def configure_authority(self, instance, lag):
        self.authority = instance, lag

    def check_ready(self, principal):
        owner = DynamicAgentAdmission._owner(principal)
        if self.engine.attached.get(self.key) != owner:
            raise ServiceError("renderer_unavailable", status=503)
        self.checked.append(principal.run_id)

    def available(self, principal):
        return principal in self.assignments.values()

    def observe(self, principal, kind, body, barrier):
        self.check_ready(principal)
        self.observed.append((principal.run_id, kind))
        return {"status": "pending"}


class DynamicAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "world.db"
        self.store = Store(self.database, world_id="world")
        self.store.ensure_world("world", "initial")
        self.engine = RegistrationEngine(self.store)
        self.renderer = RegistrationRenderer(self.engine)
        self.admission = DynamicAgentAdmission(self.store, "world", self.renderer)
        self.service = Service(self.store, self.engine, renderer=self.renderer, admission=self.admission,
                               world_id="world", autostart=False)
        self.set_worker_handle(4)

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.tmp.cleanup()

    def set_worker_handle(self, identifier, generation=1, key=None):
        self.engine.roster_rows = [{"client_id": identifier, "company_id": None,
            "label": "Trusted worker", "public_key": key or self.renderer.key,
            "native_handle": {"id": identifier, "generation": generation, "engine_instance_id": "boot"}}]

    def join(self, claim, mode="text", **extra):
        return self.service.handle("POST", "/v1/agent/join", claim, {"mode": mode, "intent": "new", **extra})

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def row(self, claim):
        return self.store.registration("world", hashlib.sha256(claim.encode()).hexdigest())

    def allocation(self, claim):
        return self.store.get_internal("world", self.row(claim)["allocation_request_id"], "company.register")

    def grant(self, claim):
        row = self.row(claim)
        return self.store.get_request(self.store.get_principal(row["run_id"]), row["worker_request_id"])

    def drain(self, times=1):
        for _ in range(times):
            self.service._process_world("world", self.store.pending_requests())

    def begin(self, claim, mode="text"):
        self.assert_code("join_not_ready", lambda: self.join(claim, mode))

    def ready(self, claim, mode="text"):
        self.begin(claim, mode)
        self.drain(2 if mode == "visual" else 1)
        self.engine.finish_checkpoint()
        self.drain()
        return self.join(claim, mode)[1]

    def test_text_ready_requires_verified_checkpoint_and_retries_do_not_allocate(self):
        claim = secrets.token_urlsafe(32)
        self.begin(claim)
        before = self.row(claim)
        self.assertIsNone(before["company_id"])
        self.assertEqual(self.engine.count("company.register"), 0)
        self.assert_code("unauthorized", lambda: self.store.authenticate(claim))
        self.begin(claim)
        self.assertEqual(self.row(claim), before)
        self.drain()
        self.assertEqual(self.row(claim)["company_id"], 7)
        self.assertFalse(self.row(claim)["ready"])
        self.assertEqual(self.engine.count("checkpoint.create"), 1)
        self.assert_code("unauthorized", lambda: self.service.handle("GET", "/v1/me", claim))
        self.begin(claim)
        self.drain()
        self.assertFalse(self.row(claim)["ready"], "A create RPC reply is not a published save")
        self.engine.finish_checkpoint()
        self.drain()
        status, reply = self.join(claim)
        self.assertEqual(status, 200)
        self.assertEqual(reply["company_id"], 7)
        self.assertEqual(reply["token"], claim)
        self.assertEqual(reply["registration_id"], reply["run_id"])
        self.assertEqual(reply["world_epoch"], "initial")
        self.assertEqual(self.join(claim), (status, reply))
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.service.handle("GET", "/v1/me", claim)[1]["registration_id"], reply["run_id"])
        self.assertNotIn(claim, "\n".join(self.store.connection.iterdump()))

    def test_concurrent_new_claims_and_same_claim_retries_have_one_allocation_each(self):
        claims = [secrets.token_urlsafe(32) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self.begin, claims * 3))
        self.assertEqual(len(self.store.registrations("world")), 4)
        self.drain()
        self.engine.finish_checkpoint()
        self.drain()
        replies = [self.join(claim)[1] for claim in claims]
        self.assertEqual(len({reply["company_id"] for reply in replies}), 4)
        self.assertEqual(len({reply["run_id"] for reply in replies}), 4)
        self.assertEqual(self.engine.count("company.register"), 4)

    def test_exact_mode_and_no_caller_selected_actor_or_company(self):
        claim = secrets.token_urlsafe(32)
        self.begin(claim)
        self.assert_code("join_conflict", lambda: self.join(claim, "visual"))
        for extra in ({"company_id": 3}, {"run_id": "mine"}, {"name": "User name"},
                      {"owner": {"company_id": 3}}, {"intent": "resume"}):
            self.assert_code("invalid_request", lambda extra=extra: self.join(secrets.token_urlsafe(32), **extra))
        self.assertEqual(len(self.store.registrations("world")), 1)
        self.assertEqual(self.engine.count("company.register"), 0)

    def test_resume_rechecks_live_owner_and_never_rebinds_reused_company(self):
        claim = secrets.token_urlsafe(32)
        reply = self.ready(claim)
        self.engine.owners[reply["company_id"]] = dict(self.engine.owners[reply["company_id"]], run_id="replacement")
        for operation in (lambda: self.service.handle("GET", "/v1/me", claim), lambda: self.join(claim)):
            self.assert_code("company_unavailable", operation)
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(len(self.store.registrations("world")), 1)

    def test_missing_ledger_receipt_is_uncertain_not_new_company(self):
        claim = secrets.token_urlsafe(32)
        self.engine.lose_registration_reply = True
        self.begin(claim)
        self.drain()
        self.assertEqual(self.allocation(claim)["status"], "queued")
        self.engine.ledger.clear()  # The authority cannot prove the sent creation outcome.
        self.drain()
        self.assertEqual(self.allocation(claim)["status"], "uncertain")
        for _ in range(3):
            self.assert_code("join_uncertain", lambda: self.join(claim))
            self.drain()
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assert_code("unauthorized", lambda: self.store.authenticate(claim))

    def test_backend_restart_recovers_lost_allocation_reply_with_same_owner_and_sequence(self):
        claim = secrets.token_urlsafe(32)
        self.engine.lose_registration_reply = True
        self.begin(claim)
        self.drain()
        old = self.row(claim)
        self.assertEqual(self.allocation(claim)["status"], "queued")
        self.service.close()
        self.store.close()
        self.store = Store(self.database, world_id="world")
        self.engine.store = self.store
        self.admission = DynamicAgentAdmission(self.store, "world", self.renderer)
        self.service = Service(self.store, self.engine, renderer=self.renderer, admission=self.admission,
                               world_id="world", autostart=False)
        self.drain()
        self.engine.finish_checkpoint()
        self.drain()
        reply = self.join(claim)[1]
        self.assertEqual(reply["run_id"], old["run_id"])
        self.assertEqual(self.row(claim)["allocation_request_sequence"], old["allocation_request_sequence"])
        self.assertEqual(self.engine.count("company.register"), 1)
        checks = [call for call in self.engine.calls if call["type"] == "action.status"
                  and call.get("request_type") == "company.register"]
        self.assertTrue(checks)
        self.assertTrue(all(call["owner"]["run_id"] == old["run_id"] for call in checks))

    def test_capture_capacity_reserved_before_any_native_creation_and_text_still_joins(self):
        first = secrets.token_urlsafe(32)
        self.begin(first, "visual")
        self.assertEqual(self.row(first)["worker_slot"], "capture-1")
        self.assertEqual(self.assert_code("visual_capacity", lambda: self.join(secrets.token_urlsafe(32), "visual")).status, 429)
        self.begin(secrets.token_urlsafe(32))
        self.assertEqual(len(self.store.registrations("world")), 2)
        self.assertEqual(self.engine.count("company.register"), 0)

    def test_native_restart_before_checkpoint_marks_creation_uncertain_without_reallocation(self):
        self.engine.finish_checkpoint()  # Known loaded save contains no company.
        self.drain()
        claim = secrets.token_urlsafe(32)
        self.begin(claim)
        self.drain()
        self.assertEqual(self.allocation(claim)["status"], "completed")
        self.engine.owners.clear()
        self.engine.ledger.clear()
        self.engine.instances["world"] = "restored-boot"
        self.engine.recovery_required = True
        self.drain(2)
        self.assertEqual(self.allocation(claim)["status"], "uncertain")
        self.assert_code("join_uncertain", lambda: self.join(claim))
        self.assert_code("unauthorized", lambda: self.store.authenticate(claim))
        self.assertEqual(self.engine.count("company.register"), 1)
        reconcile = [call for call in self.engine.calls if call["type"] == "recovery.reconcile"]
        self.assertEqual(len(reconcile), 1)
        self.assertEqual(reconcile[0]["request_type"], "company.register")
        self.assertEqual(reconcile[0]["owner"]["run_id"], self.row(claim)["run_id"])

    def test_native_restart_after_checkpoint_recovers_same_ready_company(self):
        claim = secrets.token_urlsafe(32)
        original = self.ready(claim)
        self.engine.instances["world"] = "restored-boot"
        self.engine.recovery_required = True
        self.engine.retired = self.engine.checkpoint["checkpoint_retired_through"]
        self.drain()
        self.assertEqual(self.join(claim)[1], original)
        self.assertEqual(self.service.handle("GET", "/v1/me", claim)[1]["company_id"], original["company_id"])
        self.assertEqual(self.engine.count("company.register"), 1)

    def test_capture_pending_registration_hydrates_exact_worker_slot_on_backend_restart(self):
        claim = secrets.token_urlsafe(32)
        self.begin(claim, "visual")
        self.drain()
        before = self.row(claim)
        old_grant = self.grant(claim)
        self.service.close()
        self.store.close()
        self.store = Store(self.database, world_id="world")
        self.engine.store = self.store
        self.renderer = RegistrationRenderer(self.engine)
        self.admission = DynamicAgentAdmission(self.store, "world", self.renderer)
        self.assertEqual(self.renderer.assignments[before["worker_slot"]].run_id, before["run_id"])
        self.service = Service(self.store, self.engine, renderer=self.renderer, admission=self.admission,
                               world_id="world", autostart=False)
        self.drain()
        self.engine.finish_checkpoint()
        self.drain()
        self.assertEqual(self.join(claim, "visual")[1]["company_id"], before["company_id"])
        self.assertEqual(self.grant(claim)["request_id"], old_grant["request_id"])
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.engine.count("action.submit"), 1)

    def test_capture_fixed_worker_key_grant_is_private_and_must_be_checkpointed(self):
        claim = secrets.token_urlsafe(32)
        self.begin(claim, "visual")
        self.drain()
        principal = self.store.get_principal(self.row(claim)["run_id"])
        grant = self.grant(claim)
        self.assertEqual(grant["payload"]["action"], {"name": "company.participant.authorize", "parameters": {
            "participant": self.engine.roster_rows[0]["native_handle"], "allowed": True}})
        self.assertEqual(self.renderer.assignments["capture-1"], principal)
        self.assertEqual(self.service.handle("GET", "/v1/model-reports", None), (200, {"participants": []}))
        self.assert_code("unauthorized", lambda: self.store.authenticate(claim))
        self.assert_code("forbidden", lambda: self.store.accept_request(principal, "public-bypass", {
            "action": {"name": "company.rename", "parameters": {"name": "Too early"}}, "preconditions": {}}))
        self.drain()
        self.assertEqual(self.grant(claim)["status"], "completed")
        self.assertEqual(self.engine.count("renderer.attach"), 0)
        self.assert_code("unauthorized", lambda: self.store.authenticate(claim))
        self.engine.finish_checkpoint()
        self.drain()
        reply = self.join(claim, "visual")[1]
        self.assertEqual(reply["company_id"], 7)
        self.assertEqual(self.grant(claim)["checkpoint_state"], "checkpointed")
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.assertEqual(len(self.renderer.checked), 1)
        self.assertEqual(self.engine.attached[self.renderer.key]["run_id"], reply["run_id"])

    def test_capture_pending_or_uncertain_grant_never_rotates_for_changed_handle(self):
        claim = secrets.token_urlsafe(32)
        self.engine.hold_grants = True
        self.begin(claim, "visual")
        self.drain(2)
        first = self.grant(claim)
        self.assertEqual(first["status"], "queued")
        self.set_worker_handle(5, generation=2)
        self.drain(2)
        self.assertEqual(self.grant(claim)["request_id"], first["request_id"])
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.engine.ledger[first["request_sequence"]]["status"] = "uncertain"
        self.drain(2)
        self.assertEqual(self.grant(claim)["status"], "uncertain")
        self.assertEqual(self.grant(claim)["request_id"], first["request_id"])
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.assert_code("join_uncertain", lambda: self.join(claim, "visual")).status, 409)

    def test_lost_successful_grant_reply_recovers_same_request_after_handle_churn(self):
        claim = secrets.token_urlsafe(32)
        self.engine.lose_grant_reply = True
        self.begin(claim, "visual")
        self.drain(2)
        first = self.grant(claim)
        self.assertEqual(first["status"], "queued")
        self.assertIn(self.renderer.key, self.engine.allowed_keys)
        self.set_worker_handle(5, generation=2)
        self.drain()
        self.assertEqual(self.grant(claim)["status"], "completed")
        self.assertEqual(self.grant(claim)["request_id"], first["request_id"])
        self.engine.finish_checkpoint()
        self.drain()
        self.assertEqual(self.join(claim, "visual")[1]["company_id"], 7)
        self.assertEqual(self.engine.count("action.submit"), 1)

    def test_owner_rejection_cannot_be_retried_as_a_new_worker_target(self):
        claim = secrets.token_urlsafe(32)
        self.engine.hold_grants = True
        self.begin(claim, "visual")
        self.drain(2)
        first = self.grant(claim)
        self.engine.finish_grant(first["request_sequence"], error="company_owner_mismatch")
        self.set_worker_handle(5, generation=2)
        self.drain(3)
        self.assertEqual(self.grant(claim)["request_id"], first["request_id"])
        self.assertEqual(self.grant(claim)["status"], "rejected")
        self.assertFalse(self.row(claim)["ready"])
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.assertEqual(self.assert_code("join_rejected", lambda: self.join(claim, "visual")).status, 409)

    def test_capture_accepts_native_bare_roster_without_using_public_filtered_roster(self):
        self.engine.bare_roster = True
        claim = secrets.token_urlsafe(32)
        reply = self.ready(claim, "visual")
        self.assertEqual(reply["company_id"], 7)
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.assertEqual(self.engine.attached[self.renderer.key]["run_id"], reply["run_id"])

    def test_malformed_roster_public_key_does_not_crash_or_create_a_grant(self):
        claim = secrets.token_urlsafe(32)
        self.engine.roster_rows[0]["public_key"] = 123
        self.begin(claim, "visual")
        self.drain(2)
        self.assertIsNone(self.row(claim)["worker_request_id"])
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assert_code("unauthorized", lambda: self.store.authenticate(claim))

    def test_definitive_stale_grant_retries_new_handle_but_keeps_company_and_slot(self):
        claim = secrets.token_urlsafe(32)
        self.engine.hold_grants = True
        self.begin(claim, "visual")
        self.drain(2)
        first = self.grant(claim)
        self.set_worker_handle(5, generation=2)
        self.engine.finish_grant(first["request_sequence"], error="stale_target")
        self.drain()
        second = self.grant(claim)
        self.assertNotEqual(second["request_id"], first["request_id"])
        self.assertEqual(second["payload"]["action"]["parameters"]["participant"]["id"], 5)
        self.engine.hold_grants = False
        self.drain()
        self.engine.finish_checkpoint()
        self.drain()
        self.assertEqual(self.join(claim, "visual")[1]["company_id"], 7)
        self.assertEqual(self.row(claim)["worker_slot"], "capture-1")
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.engine.count("action.submit"), 2)

    def test_checkpointed_grant_handle_churn_uses_attach_only(self):
        claim = secrets.token_urlsafe(32)
        self.engine.reject_attach = True
        self.begin(claim, "visual")
        self.drain(2)
        self.engine.finish_checkpoint()
        self.drain()
        grant = self.grant(claim)
        self.assertEqual(grant["checkpoint_state"], "checkpointed")
        self.assertFalse(self.row(claim)["ready"])
        self.set_worker_handle(9, generation=3)
        self.engine.reject_attach = False
        self.drain()
        self.assertEqual(self.join(claim, "visual")[1]["company_id"], 7)
        self.assertEqual(self.grant(claim)["request_id"], grant["request_id"])
        self.assertEqual(self.engine.count("action.submit"), 1)

    def test_capture_wrong_roster_key_or_replacement_owner_cannot_advance_setup(self):
        claim = secrets.token_urlsafe(32)
        self.set_worker_handle(4, key="b" * 64)
        self.begin(claim, "visual")
        self.drain(2)
        self.assertIsNone(self.row(claim)["worker_request_id"])
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.set_worker_handle(4)
        self.engine.owners[7]["run_id"] = "replacement"
        self.drain()
        self.assertIsNone(self.row(claim)["worker_request_id"])
        self.assert_code("unauthorized", lambda: self.store.authenticate(claim))

    def test_anonymous_reports_are_compact_ready_agent_only_and_create_nothing(self):
        self.assertEqual(self.service.handle("GET", "/v1/model-reports", None), (200, {"participants": []}))
        self.assertFalse(self.store.principals_for_world("world"))
        self.assertFalse(self.engine.calls)
        claim = secrets.token_urlsafe(32)
        self.begin(claim)
        self.drain()
        pending = self.store.get_principal(self.row(claim)["run_id"])
        self.store.set_model_report(pending, {"model_name": "Test model", "effort_level": "high"})
        self.assertEqual(self.service.handle("GET", "/v1/model-reports", None)[1], {"participants": []})
        self.engine.finish_checkpoint()
        self.drain()
        projected = self.service.handle("GET", "/v1/model-reports", None)[1]
        self.assertEqual(projected, {"participants": [{"company_id": 7, "model_report": {
            "model_name": "Test model", "effort_level": "high", "self_reported": True}}]})
        self.store.revoke_membership("world", pending.identity_id, pending.company_id)
        self.assertEqual(self.service.handle("GET", "/v1/model-reports", None)[1], {"participants": []})
        for method, path in (("POST", "/v1/model-reports"), ("GET", "/v1/model-reports/"),
                             ("GET", "/v1/%6dodel-reports")):
            self.assert_code("unauthorized", lambda method=method, path=path: self.service.handle(method, path, None))
        self.assert_code("invalid_request", lambda: self.service.handle("GET", "/v1/model-reports", None, query={"x": "y"}))

    def test_public_participants_hide_provisional_owner_and_idle_worker_until_ready(self):
        operator_token = self.store.provision("world", "operator", 14, kind="agent")
        claim = secrets.token_urlsafe(32)
        self.begin(claim, "visual")
        self.drain()
        rows = self.service.handle("GET", "/v1/participants", operator_token)[1]["participants"]
        self.assertEqual({row["identity_id"] for row in rows}, {"operator"})
        self.assertEqual(len(self.store.principals_for_world("world")), 2)
        self.drain()
        self.engine.finish_checkpoint()
        self.drain()
        reply = self.join(claim, "visual")[1]
        rows = self.service.handle("GET", "/v1/participants", operator_token)[1]["participants"]
        self.assertEqual({row["identity_id"] for row in rows}, {"operator", reply["identity_id"]})

    def test_http_only_exact_get_model_reports_is_credential_free(self):
        server = http.create_server(self.service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_address[1]}"

        def fetch(path, method="GET"):
            request = Request(origin + path, method=method,
                              data=b"{}" if method == "POST" else None,
                              headers={"Content-Type": "application/json"} if method == "POST" else {})
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

        try:
            self.assertEqual(fetch("/v1/model-reports"), (200, {"participants": []}))
            self.assertEqual(fetch("/v1/model-reports?x=1")[0], 400)
            for method, path in (("POST", "/v1/model-reports"), ("GET", "/v1/model-reports/"),
                                 ("GET", "/v1/%6dodel-reports"), ("GET", "/v1/me"),
                                 ("GET", "/v1/participants")):
                with self.subTest(method=method, path=path):
                    status, body = fetch(path, method)
                    self.assertEqual(status, 401)
                    self.assertEqual(body["error"]["code"], "unauthorized")
            self.assertFalse(self.store.principals_for_world("world"))
            self.assertEqual(self.engine.count("company.register"), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_deleted_owner_cannot_read_or_write_replacement_company_chat_events_or_images(self):
        self.engine.company_ids = iter((7, 7))  # Native deletion and ID reuse.
        old_claim, current_claim = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        old = self.ready(old_claim)
        current = self.ready(current_claim)
        self.assertEqual(old["company_id"], current["company_id"])
        self.assertNotEqual(old["run_id"], current["run_id"])
        _, thread = self.service.handle("POST", "/v1/threads", current_claim, {"scope": "company"})
        path = f"/v1/threads/{thread['id']}/messages"
        status, message = self.service.handle("POST", path, current_claim,
            {"body": "Replacement owner's private plans", "request_id": "new-private-message"})
        self.assertEqual(status, 201)
        self.assertEqual(self.service.handle("GET", path, current_claim)[1]["messages"][0]["id"], message["id"])
        # DB membership is intentionally still present; native incarnation is
        # the required fence even for routes with no native gameplay operation.
        self.assertEqual(self.store.authenticate(old_claim).run_id, old["run_id"])
        operations = [
            lambda: self.service.handle("GET", "/v1/me", old_claim),
            lambda: self.service.handle("GET", "/v1/threads", old_claim),
            lambda: self.service.handle("POST", "/v1/threads", old_claim, {"scope": "company"}),
            lambda: self.service.handle("GET", path, old_claim),
            lambda: self.service.handle("POST", path, old_claim, {"body": "Spy", "request_id": "spy"}),
            lambda: self.service.events(old_claim),
            lambda: self.service.wait_for_events(old_claim, timeout=0),
            lambda: self.service.image(old_claim, "private-image"),
        ]
        configurations = [("dynamic", self.admission), ("disabled", None),
                          ("legacy-only", SimpleNamespace(unclaimed_runs=lambda: set()))]
        for label, admission in configurations:
            self.service.admission = admission
            with self.subTest(admission=label):
                for operation in operations:
                    self.assert_code("company_unavailable", operation)
                current_me = self.service.handle("GET", "/v1/me", current_claim)[1]
                self.assertEqual(current_me["run_id"], current["run_id"])
                self.assertEqual(current_me["registration_id"], current["run_id"])
                self.assertEqual(current_me["world_epoch"], "initial")
        self.assertEqual(self.engine.count("company.register"), 2)

    def test_native_owner_check_temporary_errors_are_retryable_and_binding_errors_are_conflicts(self):
        claim = secrets.token_urlsafe(32)
        original = self.ready(claim)
        self.service.admission = None
        for native_error in ("checkpoint_busy", "world_unavailable", "ledger_unavailable", "authority_unavailable",
                             "world_recovering", "recovery_required", "world_replaced", "engine_fence"):
            with self.subTest(native_error=native_error):
                self.engine.owner_error = native_error
                with self.assertRaises(ServiceError) as caught:
                    self.service.handle("GET", "/v1/me", claim)
                self.assertEqual(caught.exception.status, 503)
                expected = "join_not_ready" if native_error in {
                    "checkpoint_busy", "world_unavailable", "ledger_unavailable", "authority_unavailable"
                } else "world_recovering"
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(self.store.authenticate(claim).run_id, original["run_id"])
        for native_error in ("stale_company_binding", "company_owner_mismatch", "invalid_actor"):
            with self.subTest(native_error=native_error):
                self.engine.owner_error = native_error
                self.assertEqual(self.assert_code("company_unavailable", lambda:
                    self.service.handle("GET", "/v1/me", claim)).status, 409)
        self.engine.owner_error = None
        self.engine.unavailable.add("world")
        self.assertEqual(self.assert_code("engine_unavailable", lambda:
            self.service.handle("GET", "/v1/me", claim)).status, 503)
        self.engine.unavailable.clear()
        self.assertEqual(self.service.handle("GET", "/v1/me", claim)[1]["run_id"], original["run_id"])
        self.assertEqual(self.engine.count("company.register"), 1)

    def test_ready_visual_backend_restart_and_spectator_reconnect_repair_only_on_demand(self):
        claim = secrets.token_urlsafe(32)
        original = self.ready(claim, "visual")
        before = self.row(claim)
        sequence = self.store.max_request_sequence("world")
        attaches = self.engine.count("renderer.attach")
        self.engine.attached.clear()
        self.set_worker_handle(15, generation=3)
        self.service.close()
        self.store.close()
        self.store = Store(self.database, world_id="world")
        self.engine.store = self.store
        self.renderer = RegistrationRenderer(self.engine)
        self.service = Service(self.store, self.engine, renderer=self.renderer, admission=None,
                               world_id="world", autostart=False)
        principal = self.store.authenticate(claim)
        self.assertFalse(self.renderer.available(principal))
        self.drain(2)
        self.assertEqual(self.engine.count("renderer.attach"), attaches, "Idle READY runs do not probe workers")
        self.service._configure_renderer(principal)
        self.assertTrue(self.renderer.available(principal))
        self.assertEqual(self.engine.attached[self.renderer.key], DynamicAgentAdmission._owner(principal))
        self.assertEqual(self.renderer.assignments[before["worker_slot"]], principal)
        self.assertEqual(self.engine.count("renderer.attach"), attaches + 1)
        # A reconnect can retain the same gateway object and alias. It still
        # needs attachment even when the object has cached its earlier binding.
        self.engine.attached.clear()
        self.set_worker_handle(19, generation=4)
        self.assertTrue(self.renderer.available(principal))
        self.service._configure_renderer(principal)
        self.assertEqual(self.engine.count("renderer.attach"), attaches + 2)
        self.assertEqual(self.engine.attached[self.renderer.key]["run_id"], original["run_id"])
        self.assertEqual(self.row(claim), before)
        self.assertEqual(self.store.max_request_sequence("world"), sequence)
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.engine.count("action.submit"), 1)

    def test_selected_observation_can_repair_ready_worker_without_admission_or_existing_alias(self):
        claim = secrets.token_urlsafe(32)
        original = self.ready(claim, "visual")
        self.engine.attached.clear()
        self.renderer = RegistrationRenderer(self.engine)
        self.service.renderer = self.renderer
        self.service.admission = None
        principal = self.store.authenticate(claim)
        self.assertFalse(self.renderer.available(principal))
        status, response = self.service.handle("POST", "/v1/view/observe/world", claim, {})
        self.assertEqual((status, response), (202, {"status": "pending"}))
        self.assertEqual(self.renderer.observed, [(original["run_id"], "world")])
        self.assertEqual(self.engine.attached[self.renderer.key]["run_id"], original["run_id"])
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.assertEqual(self.store.max_request_sequence("world"), 2)

    def test_ready_visual_repair_refuses_changed_key_owner_and_missing_durable_grant(self):
        claim = secrets.token_urlsafe(32)
        self.ready(claim, "visual")
        principal = self.store.authenticate(claim)
        grant = self.grant(claim)
        before = self.row(claim)
        self.service.admission = None
        self.engine.attached.clear()
        attaches = self.engine.count("renderer.attach")
        self.renderer.key = "b" * 64
        self.assert_code("join_worker_changed", lambda: self.service._configure_renderer(principal))
        self.assertEqual(self.engine.count("renderer.attach"), attaches)
        self.renderer.key = before["worker_public_key"]
        owner = copy.deepcopy(self.engine.owners[principal.company_id])
        self.engine.owners[principal.company_id]["run_id"] = "replacement"
        self.assert_code("company_unavailable", lambda: self.service._configure_renderer(principal))
        self.assertEqual(self.engine.count("renderer.attach"), attaches)
        self.engine.owners[principal.company_id] = owner
        for status in ("completed", "uncertain"):
            with self.subTest(grant_status=status):
                self.store.begin_recovery("world", "boot")
                self.store.resolve_recovery_request(principal, grant["request_id"], status,
                                                    result=grant["result"], checkpointed=False)
                self.store.finish_recovery("world", scan_completed=True)
                self.assert_code("join_not_ready", lambda: self.service._configure_renderer(principal))
                self.assertEqual(self.engine.count("renderer.attach"), attaches)
        self.store.begin_recovery("world", "boot")
        self.store.resolve_recovery_request(principal, grant["request_id"], "completed",
                                            result=grant["result"], checkpointed=True)
        self.store.finish_recovery("world", scan_completed=True)
        self.engine.allowed_keys.clear()
        self.assert_code("join_not_ready", lambda: self.service._configure_renderer(principal))
        self.assertEqual(self.engine.count("renderer.attach"), attaches + 1)
        self.assertFalse(self.engine.attached)
        self.assertFalse(self.engine.allowed_keys, "Revoked keys must never be silently regranted")
        self.assertEqual(self.row(claim), before)
        self.assertEqual(self.store.max_request_sequence("world"), 2)
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.engine.count("action.submit"), 1)

    def test_ui_apply_repairs_worker_before_intent_preparation(self):
        claim = secrets.token_urlsafe(32)
        self.ready(claim, "visual")
        principal = self.store.authenticate(claim)
        self.engine.attached.clear()
        self.renderer.assignments.clear()
        self.service.admission = None
        prepared = []

        def prepare(owner, instance, lag):
            self.renderer.check_ready(owner)
            prepared.append((owner, instance, lag))
            # This test stops exactly at the preparation boundary. Other
            # suites cover capture bundles and durable intent dispatch.
            raise ServiceError("prepared_after_repair", status=503)

        self.renderer.prepare_intent = prepare
        record, _ = self.store.accept_request(principal, "repair-before-click", {
            "action": {"name": "ui.apply", "parameters": {
                "image_id": "old-image", "gesture": "click", "x": 2, "y": 3}}, "preconditions": {}})
        self.assert_code("prepared_after_repair", lambda: self.service._process_record(principal, record))
        self.assertEqual(prepared, [(principal, "boot", 100)])
        self.assertEqual(self.engine.attached[self.renderer.key], DynamicAgentAdmission._owner(principal))
        self.assertEqual(self.store.max_request_sequence("world"), 3)
        self.assertEqual(self.engine.count("company.register"), 1)
        self.assertEqual(self.engine.count("action.submit"), 1)

    def legacy_claim(self, *, bound):
        token = self.store.provision("world", "legacy-public-agent", 2, mode="text", kind="agent")
        principal = self.store.authenticate(token)
        if bound:
            self.engine.owners[2] = DynamicAgentAdmission._owner(principal)
        self.store.claim_agent_join("world", "initial", hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            "text", principal, hashlib.sha256(token.encode()).hexdigest(), "boot")
        return token, principal

    def test_claimed_legacy_owner_is_verified_without_admission_and_requires_native_support(self):
        token, principal = self.legacy_claim(bound=True)
        self.service.admission = None
        self.engine.registration_capability = False
        self.assertEqual(self.assert_code("join_not_ready", lambda:
            self.service.handle("GET", "/v1/me", token)).status, 503)
        self.assertEqual(self.engine.count("session.bind"), 0)
        self.engine.registration_capability = True
        _, me = self.service.handle("GET", "/v1/me", token)
        expected = {"identity_id": principal.identity_id, "world_id": "world", "company_id": 2,
                    "run_id": principal.run_id, "mode": "text", "kind": "agent",
                    "display_name": principal.display_name}
        self.assertEqual(me, expected)
        self.assertIsNone(self.store.registration_for_run(principal.run_id))
        self.assertEqual(self.engine.count("session.bind"), 1)
        self.assertIs(self.engine.calls[-1]["owner_bound"], True)
        self.assertEqual(self.engine.calls[-1]["actor"], DynamicAgentAdmission._owner(principal))
        # ID reuse changes only native incarnation. The old durable claim and
        # bearer still exist, so backend-only social routes need the same gate.
        self.engine.owners[2] = dict(self.engine.owners[2], run_id="replacement-company-owner")
        self.service._chat_ready = True
        replacement = self.store.provision("world", "replacement-operator", 2)
        _, thread = self.service.handle("POST", "/v1/threads", replacement, {"scope": "company"})
        path = f"/v1/threads/{thread['id']}/messages"
        self.service.handle("POST", path, replacement,
            {"request_id": "replacement-secret", "body": "Private replacement company plans"})
        self.assertEqual(len(self.service.handle("GET", path, replacement)[1]["messages"]), 1)
        operations = [
            lambda: self.service.handle("GET", "/v1/me", token),
            lambda: self.service.handle("GET", "/v1/threads", token),
            lambda: self.service.handle("POST", "/v1/threads", token, {"scope": "company"}),
            lambda: self.service.handle("GET", path, token),
            lambda: self.service.handle("POST", path, token, {"request_id": "spy", "body": "Spy"}),
            lambda: self.service.events(token),
            lambda: self.service.wait_for_events(token, timeout=0),
            lambda: self.service.image(token, "replacement-private-image"),
        ]
        for operation in operations:
            self.assert_code("company_unavailable", operation)
        self.assertEqual(self.store.authenticate(token), principal)
        self.assertEqual(self.engine.count("company.register"), 0)
        self.assertEqual(self.engine.count("action.submit"), 0)

    def test_claimed_owner_with_no_native_engine_fails_unavailable(self):
        token, _ = self.legacy_claim(bound=True)
        self.service.engine = None
        self.assertEqual(self.assert_code("engine_unavailable", lambda:
            self.service.handle("GET", "/v1/me", token)).status, 503)

    def test_claimed_legacy_without_native_anchor_fails_closed_without_implicit_binding(self):
        token, principal = self.legacy_claim(bound=False)
        self.service.admission = None
        self.assertEqual(self.assert_code("company_unavailable", lambda:
            self.service.handle("GET", "/v1/me", token)).status, 409)
        self.assertFalse(self.engine.owners)
        self.assertEqual(self.store.authenticate(token), principal)
        binds = [call for call in self.engine.calls if call["type"] == "session.bind"]
        self.assertEqual(len(binds), 1)
        self.assertIs(binds[0]["owner_bound"], True)
        self.assertEqual(self.engine.count("company.register"), 0)
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(self.store.max_request_sequence("world"), 0)

    def test_unclaimed_private_operator_credential_keeps_existing_authentication_behavior(self):
        token = self.store.provision("world", "private-operator", 4, kind="agent")
        principal = self.store.authenticate(token)
        self.service.admission = None
        self.engine.unavailable.add("world")
        _, me = self.service.handle("GET", "/v1/me", token)
        self.assertEqual(me["run_id"], principal.run_id)
        self.assertNotIn("registration_id", me)
        self.assertNotIn("world_epoch", me)
        self.assertFalse(self.engine.calls)
        self.assertNotIn(principal.run_id, self.store.agent_join_claimed_runs("world"))


if __name__ == "__main__":
    unittest.main()
