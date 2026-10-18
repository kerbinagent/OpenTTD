"""Service contract tests with durable state and a deterministic engine boundary."""

import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from backend.errors import ServiceError
from backend.catalog import (CATALOG, CATALOG_ACTION_NAMES, CATALOG_QUERY_NAMES,
                             CATALOG_HASH, CATALOG_HASH_ALGORITHM, action_spec)
from backend.service import Service
from backend.store import Store


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.instances = {}
        self.unavailable = set()
        self.submit_reply = {"status": "completed", "result": {"success": True},
                             "action_sequence": 7, "tick": 12}
        self.status_reply = {"status": "completed", "result": {"success": True},
                             "action_sequence": 7, "tick": 12}
        self.on_submit = None
        self.hello_role = "world"
        self.recovery_required = False
        self.pause = {"paused": False, "reasons": [], "command_policy": "no_actions", "revision": 0}
        self.hello_reply_override = None
        self.hello_error = None
        self.hello_capabilities = {
            **{key: copy.deepcopy(value) for key, value in CATALOG.items()
               if key not in ("actions", "queries")},
            "actions": list(CATALOG_ACTION_NAMES),
            "queries": list(CATALOG_QUERY_NAMES),
            "catalog_hash_algorithm": CATALOG_HASH_ALGORITHM,
            "catalog_hash": CATALOG_HASH,
        }
        self.roster_rows = []
        self.roster_status = "completed"
        self.roster_instance = None
        self.reference_reply = None
        self.estimate_reply = None
        self.bind_reply = {"status": "completed", "result": {"bound": True}}

    def valid_hello_reply(self, world_id):
        return {"status": "completed", "result": {
            "role": self.hello_role,
            "engine_instance_id": self.instances.get(world_id, "boot"),
            "world_epoch": "initial",
            "recovery_required": self.recovery_required,
            "pause": copy.deepcopy(self.pause),
            "native_max_lag_ticks": 100,
            "capabilities": copy.deepcopy(self.hello_capabilities),
        }}

    def call(self, envelope):
        self.calls.append(copy.deepcopy(envelope))
        world_id = envelope["world_id"]
        kind = envelope["type"]
        if world_id in self.unavailable:
            raise ServiceError("engine_unavailable", status=503)
        if kind == "hello":
            if self.hello_error:
                raise self.hello_error
            if self.hello_reply_override is not None:
                return copy.deepcopy(self.hello_reply_override)
            return self.valid_hello_reply(world_id)
        if kind == "session.bind":
            return copy.deepcopy(self.bind_reply)
        if kind == "chat.roster":
            return {"status": self.roster_status,
                    "engine_instance_id": self.roster_instance or self.instances.get(world_id, "boot"),
                    "result": {"clients": copy.deepcopy(self.roster_rows)}}
        if kind == "action.submit":
            if self.on_submit:
                self.on_submit(envelope)
            if envelope.get("action", {}).get("name") == "vehicle.start_stop":
                handle = envelope["action"]["parameters"]["vehicle_id"]
                if handle["engine_instance_id"] != envelope["engine_instance_id"]:
                    return {"status": "rejected", "result": {"success": False},
                            "error": {"code": "stale_handle"}}
            return copy.deepcopy(self.submit_reply)
        if kind == "action.status":
            return copy.deepcopy(self.status_reply)
        if kind == "control.reference":
            return copy.deepcopy(self.reference_reply)
        if kind == "action.estimate":
            return copy.deepcopy(self.estimate_reply)
        if kind == "checkpoint.status":
            return {"status": "completed", "result": {
                "checkpoint": {"checkpoint_id": "", "retired_through": 0},
                "retired_through": 0, "load_verified": True,
            }}
        if kind == "recovery.finish":
            return {"status": "completed", "result": {}}
        if kind == "observation.query":
            if envelope.get("query") == "company.access":
                params = envelope.get("parameters", {})
                result = {"tick": 12, "company_id": envelope["actor"]["company_id"],
                          "join_policy": "listed"}
                if "participant" in params:
                    result.update(participant=params["participant"], listed=True, permitted=True)
                return {"status": "completed", "result": result}
            return {"status": "completed", "result": {"vehicles": [], "save_path": "/private/save"}}
        raise AssertionError(f"Unexpected engine RPC: {kind}")

    def count(self, kind, world_id=None):
        return sum(call["type"] == kind and (world_id is None or call["world_id"] == world_id)
                   for call in self.calls)


class FakeRenderer:
    def __init__(self):
        self.validations = []
        self.captures = []
        self.releases = []
        self.consumed = False
        self.fail_capture = False
        self.child_phase = "pending"
        self.worker_instance_id = "worker-1"
        self.bundle = None
        self.prepared = []
        self.on_capture = None
        self.native_worker_keys = set()
        self.renderer_identity_unavailable = False
        self.verified_key_calls = 0

    def available(self, principal):
        return True

    def known_native_worker_keys(self):
        return set(self.native_worker_keys)

    def managed_native_worker_keys(self):
        return {key.lower() for key in self.native_worker_keys}

    def verified_native_worker_keys(self):
        self.verified_key_calls += 1
        if self.renderer_identity_unavailable:
            raise ServiceError("renderer_unavailable", status=503)
        return self.managed_native_worker_keys()

    def validate_gesture(self, principal, params):
        self.validations.append((principal.run_id, copy.deepcopy(params)))
        if self.consumed:
            raise ServiceError("image_unavailable", status=404)

    def configure_authority(self, instance, max_lag):
        return None

    def prepare_intent(self, principal, authority_instance, max_lag):
        self.prepared.append((principal.run_id, authority_instance, max_lag))
        return self.worker_instance_id

    def _fence(self, expected_worker_instance_id):
        if expected_worker_instance_id != self.worker_instance_id:
            raise ServiceError("renderer_unavailable", status=503)

    def capture_intent(self, principal, request_id, params, authority_instance, max_lag,
                       *, expected_worker_instance_id):
        self._fence(expected_worker_instance_id)
        if self.on_capture:
            self.on_capture(principal, request_id, expected_worker_instance_id)
        self.captures.append((request_id, copy.deepcopy(params), authority_instance, max_lag))
        if self.fail_capture:
            raise ServiceError("capture_failed", status=503)
        self.bundle = {"version": 2, "request_id": request_id, "image_id": params["image_id"],
                       "source_frame": 17,
                       "world_epoch": "initial", "worker_instance_id": self.worker_instance_id,
                       "authority_instance_id": authority_instance,
                       "owner": {"client_id": 5, "public_key": "key"},
                       "commands": [], "tile_proof": None}
        return copy.deepcopy(self.bundle)

    def release_intent(self, principal, request_id, phase, authority_rejected=False,
                       *, expected_worker_instance_id):
        self._fence(expected_worker_instance_id)
        self.releases.append((request_id, phase, authority_rejected))

    def intent_status(self, principal, request_id, *, expected_worker_instance_id):
        self._fence(expected_worker_instance_id)
        return {"phase": self.child_phase, "children": [], "bundle": copy.deepcopy(self.bundle)}

    def clear_children(self, principal, status, *, expected_worker_instance_id):
        self._fence(expected_worker_instance_id)
        return None


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "state.db"), max_pending_company=1)
        self.engine = FakeEngine()
        self.service = Service(self.store, self.engine, autostart=False)
        self.token = self.store.provision("w1", "alice", 1)
        self.principal = self.store.authenticate(self.token)

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.tmp.cleanup()

    def action(self, request_id="r1", name="road.build", parameters=None, **extra):
        return {"request_id": request_id,
                "action": {"name": name, "parameters": parameters if parameters is not None else {"start": 1, "end": 2}},
                **extra}

    @staticmethod
    def participant_spec():
        handle = {"type": "object", "required": ["id", "generation", "engine_instance_id"],
                  "additionalProperties": False, "properties": {
                      "id": {"type": "integer", "minimum": 0, "maximum": 0xFFFFFFFF},
                      "generation": {"type": "integer", "minimum": 1,
                                     "maximum": 0xFFFFFFFFFFFFFFFF},
                      "engine_instance_id": {"type": "string", "minLength": 1,
                                             "maxLength": 256}}}
        return {"name": "company.participant.authorize", "modes": ["text", "visual"],
                "parameters": {"type": "object", "required": ["participant", "allowed"],
                               "additionalProperties": False, "properties": {
                                   "participant": handle, "allowed": {"type": "boolean"}}},
                "result_fields": ["success", "cost", "error"]}

    @staticmethod
    def access_spec():
        handle = ServiceTests.participant_spec()["parameters"]["properties"]["participant"]
        return {"name": "company.access", "parameters": {"type": "object",
                "required": [], "additionalProperties": False,
                "properties": {"participant": handle}},
                "result_fields": ["tick", "company_id", "join_policy", "participant",
                                  "listed", "permitted"]}

    @contextmanager
    def estimate_enabled(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["conventions"]["action_estimate"] = {
            "endpoint": "/v1/actions/estimate", "modes": ["text", "visual"],
            "result_fields": ["success", "cost", "error", "advisory",
                              "test_and_execute_may_differ"],
            "semantics": "Native test only"}
        catalog["limits"].update(estimate_interval_ms=1000, estimate_result_bytes=4096)
        digest = hashlib.blake2b(json.dumps(catalog, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode("utf-8"), digest_size=32).hexdigest()
        self.engine.hello_capabilities["conventions"] = copy.deepcopy(catalog["conventions"])
        self.engine.hello_capabilities["limits"] = copy.deepcopy(catalog["limits"])
        self.engine.hello_capabilities["catalog_hash"] = digest
        with patch("backend.service.CATALOG", catalog), \
                patch("backend.catalog.CATALOG", catalog), \
                patch("backend.catalog.CATALOG_HASH", digest):
            yield

    def visual(self, request_id="gesture", x=2):
        return self.action(request_id=request_id, name="ui.apply",
                           parameters={"image_id": "image-1", "gesture": "click", "x": x, "y": 3})

    def visual_actor(self):
        renderer = FakeRenderer()
        self.service.renderer = renderer
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        return token, self.store.authenticate(token), renderer

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def process(self, principal=None, request_id="r1"):
        principal = principal or self.principal
        return self.service._process_record(
            principal, self.store.get_request(principal, request_id)
        )

    def test_world_status_is_shared_and_cursor_precedes_fresh_hello(self):
        visual_token = self.store.provision("w1", "viewer", 2, mode="visual")
        self.engine.pause = {"paused": True, "reasons": ["manual"],
                             "command_policy": "no_construction", "revision": 7,
                             "private_tick": 999}
        expected = {"world_id": "w1", "world_epoch": "initial", "engine_instance_id": "boot",
                    "pause": {"paused": True, "reasons": ["manual"],
                              "command_policy": "no_construction", "revision": 7}}
        for token in (self.token, visual_token):
            code, current = self.service.handle("GET", "/v1/world/status", token)
            self.assertEqual(code, 200)
            self.assertEqual({k: v for k, v in current.items() if k != "event_cursor"}, expected)
            self.assertIsInstance(current["event_cursor"], str)
            self.assertEqual(self.service.handle("GET", "/v1/capabilities", token)[1]["world_status"],
                             {"endpoint": "/v1/world/status", "event": "world.status"})

        before = self.store.current_event_cursor(self.principal)
        original = self.engine.call
        def racing_hello(envelope):
            if envelope["type"] == "hello":
                writer = threading.Thread(target=lambda: self.store.append_event(self.principal, "example", {}))
                writer.start()
                writer.join(timeout=1)
                self.assertFalse(writer.is_alive(), "Status RPC held the Store lock")
            return original(envelope)
        with patch.object(self.engine, "call", side_effect=racing_hello):
            _, current = self.service.handle("GET", "/v1/world/status", self.token)
        self.assertEqual(current["event_cursor"], before)
        self.assertEqual([event["type"] for event in self.store.events(self.principal, after=before)["events"]], ["example"])

    def test_world_status_refuses_recovery_and_malformed_native_pause(self):
        self.engine.recovery_required = True
        self.assert_code("world_recovering", lambda: self.service.handle("GET", "/v1/world/status", self.token))
        self.engine.recovery_required = False
        self.engine.pause = {"paused": "false", "reasons": [], "command_policy": "no_actions", "revision": 0}
        self.assert_code("world_status_unavailable", lambda: self.service.handle("GET", "/v1/world/status", self.token))
        self.assertNotIn("world_status", self.service.handle("GET", "/v1/capabilities", self.token)[1])
        self.store.begin_recovery("w1", "boot-new")
        hellos = self.engine.count("hello")
        self.assert_code("world_recovering", lambda: self.service.handle("GET", "/v1/world/status", self.token))
        self.assertEqual(self.engine.count("hello"), hellos)

    def test_action_admission_is_fast_idempotent_and_rejects_body_actor(self):
        self.assert_code("invalid_request", lambda: self.service.handle(
            "POST", "/v1/actions", self.token, self.action(actor={"company_id": 2})
        ))
        status, result = self.service.handle("POST", "/v1/actions", self.token, self.action())
        self.assertEqual((status, result["status"]), (202, "accepted"))
        self.assertEqual([call["type"] for call in self.engine.calls], ["hello"])
        same_status, same = self.service.handle("POST", "/v1/actions", self.token, self.action())
        self.assertEqual((same_status, same), (status, result))
        self.assertEqual(len(self.service.events(self.token)["events"]), 1)
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", self.token, self.action(parameters={"start": 1, "end": 3})
        ))
        self.assertEqual(len(self.store.pending_requests()), 1)

    def test_action_cannot_name_another_company(self):
        self.store.provision("w1", "bob", 2)
        self.assert_code("invalid_request", lambda: self.service.handle(
            "POST", "/v1/actions", self.token,
            self.action(parameters={"company_id": 2, "start": 1, "end": 2})
        ))
        self.assertEqual(self.store.pending_requests(), [])

    def test_legacy_two_field_handle_receipt_replays_after_service_restart(self):
        old_body = self.action(request_id="old-vehicle", name="vehicle.start_stop",
                               parameters={"vehicle_id": {"id": 12, "generation": 3}})
        self.store.accept_request(self.principal, "old-vehicle", {
            "action": old_body["action"], "preconditions": {},
        })
        self.store.update_request(self.principal, "old-vehicle", "completed",
                                  result={"success": True})
        self.service.close()
        self.service = Service(self.store, self.engine, autostart=False, world_id="w1")
        before = len(self.engine.calls)
        code, replayed = self.service.handle("POST", "/v1/actions", self.token, old_body)
        self.assertEqual((code, replayed["status"], replayed["result"]),
                         (200, "completed", {"success": True}))
        self.assertEqual(self.service.handle("GET", "/v1/actions/old-vehicle", self.token)[1], replayed)
        self.assertEqual(len(self.engine.calls), before)
        changed = self.action(request_id="old-vehicle", name="vehicle.start_stop",
                              parameters={"vehicle_id": {"id": 12, "generation": 4}})
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", self.token, changed
        ))
        self.assertEqual(len(self.engine.calls), before)
        self.assertEqual(self.store.max_request_sequence("w1"), 1)

    def test_catalog_mode_parameter_is_not_actor_mode(self):
        visual_token = self.store.provision("w1", "viewer", 2, mode="visual")
        visual_actor = self.store.authenticate(visual_token)
        vehicle = {"id": 9, "generation": 3, "engine_instance_id": "boot"}
        target_company = {"id": 3, "generation": 5, "engine_instance_id": "boot"}
        version = "ov1:" + "a" * 64
        actions = (
            ("company.loan.increase", {"mode": "max"}),
            ("company.loan.decrease", {"mode": "interval"}),
            ("order.unshare", {"vehicle_id": vehicle, "order_version": version, "mode": "keep"}),
            ("order.refit", {"vehicle_id": vehicle, "order_version": version,
                             "position": 0, "mode": "none"}),
            ("vehicle.service_interval.set", {"vehicle_id": vehicle, "mode": "default"}),
            ("company.buy", {"company_id": target_company}),
        )
        for actor_mode, token, principal in (
            ("text", self.token, self.principal),
            ("visual", visual_token, visual_actor),
        ):
            for index, (name, parameters) in enumerate(actions):
                request_id = f"mode-{actor_mode}-{index}"
                code, admitted = self.service.handle("POST", "/v1/actions", token,
                    self.action(request_id, name, parameters))
                self.assertEqual((code, admitted["status"]), (202, "accepted"))
                completed = self.process(principal, request_id)
                self.assertEqual(completed["status"], "completed")
                submit = [call for call in self.engine.calls if call["type"] == "action.submit"][-1]
                self.assertEqual(submit["actor"]["mode"], actor_mode)
                self.assertEqual(submit["actor"]["company_id"], principal.company_id)
                self.assertEqual(submit["action"]["parameters"], parameters)

            before = self.store.max_request_sequence("w1")
            for invalid in ("visual", True, 7, None):
                self.assert_code("invalid_request", lambda: self.service.handle(
                    "POST", "/v1/actions", token,
                    self.action(f"bad-mode-{actor_mode}-{invalid}",
                                "company.loan.increase", {"mode": invalid})))
            self.assert_code("invalid_request", lambda: self.service.handle(
                "POST", "/v1/actions", token,
                self.action(f"bad-order-{actor_mode}", "order.unshare",
                            {"vehicle_id": vehicle, "order_version": version, "mode": "reset"})))
            self.assert_code("invalid_request", lambda: self.service.handle(
                "POST", "/v1/actions", token,
                self.action(f"bad-buy-{actor_mode}", "company.buy", {"company_id": 3})))
            self.assert_code("invalid_request", lambda: self.service.handle(
                "POST", "/v1/actions", token,
                self.action(f"bad-company-{actor_mode}", "company.loan.increase",
                            {"mode": "max", "company_id": 3})))
            for forged in ({"mode": "text"}, {"actor": {"mode": "text"}}):
                self.assert_code("invalid_request", lambda: self.service.handle(
                    "POST", "/v1/actions", token,
                    self.action(f"forged-{actor_mode}", "company.loan.increase",
                                {"mode": "max"}, **forged)))
            self.assertEqual(self.store.max_request_sequence("w1"), before)

    def test_catalog_action_parameters_do_not_collide_with_reserved_actor_fields(self):
        reserved = {"world_id", "actor", "identity_id", "run_id", "credentials", "company_id"}

        def top_level_properties(schema):
            if not isinstance(schema, dict):
                return set()
            result = set(schema.get("properties", {}))
            for branch in schema.get("anyOf", ()):
                result.update(top_level_properties(branch))
            return result

        modes = set()
        for spec in CATALOG["actions"]:
            if spec["name"] == "ui.apply":
                continue
            fields = top_level_properties(spec["parameters"])
            if "mode" in fields:
                modes.add(spec["name"])
            allowed = {"company_id"} if spec["name"] in ("company.give_money", "company.buy") else set()
            self.assertEqual(fields & reserved, allowed, spec["name"])
        self.assertTrue({"company.loan.increase", "company.loan.decrease", "order.unshare",
                         "order.refit", "vehicle.service_interval.set"} <= modes)

    def test_estimate_is_shared_advisory_feedback_without_action_ledger_or_renderer(self):
        visual_token = self.store.provision("w1", "viewer", 2, mode="visual")
        self.engine.estimate_reply = {"status": "completed", "engine_instance_id": "boot",
            "result": {"success": True, "cost": -25, "advisory": True,
                       "test_and_execute_may_differ": True,
                       "vehicle_id": {"id": 9}, "capacity": 999, "map": {"private": True}}}
        request = {"action": {"name": "company.loan.increase", "parameters": {"mode": "max"}}}
        expected = {"estimate": {"success": True, "cost": -25, "advisory": True,
                                 "test_and_execute_may_differ": True}}
        with self.estimate_enabled():
            for actor_mode, token in (("text", self.token), ("visual", visual_token)):
                capability = self.service.handle("GET", "/v1/capabilities", token)[1]
                self.assertEqual(capability["action_estimate"], {
                    "endpoint": "/v1/actions/estimate", "interval_ms": 1000,
                    "result_bytes": 4096})
                self.assertEqual(self.service.handle(
                    "POST", "/v1/actions/estimate", token, request), (200, expected))
                sent = [call for call in self.engine.calls if call["type"] == "action.estimate"][-1]
                self.assertEqual(sent["actor"]["mode"], actor_mode)
                self.assertEqual(sent["actor"]["company_id"],
                                 self.store.authenticate(token).company_id)
                self.assertEqual(sent["action"], request["action"])
                self.assertEqual(sent["preconditions"], {})
                self.assertFalse({"request_id", "request_sequence", "payload_sha256",
                                  "visual_intent", "image_id"} & set(sent))
                self.assertEqual(self.service.events(token)["events"], [])
            self.assertEqual(self.store.max_request_sequence("w1"), 0)
            self.assertEqual(self.engine.count("action.submit"), 0)
            self.assertEqual(self.service.handle("POST", "/v1/actions", self.token,
                {"request_id": "execute", **request})[0], 202)
            self.assertEqual(self.store.max_request_sequence("w1"), 1)

    def test_estimate_shape_policy_and_private_refusals(self):
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        good = {"action": {"name": "company.loan.increase", "parameters": {"mode": "max"}}}
        legacy = copy.deepcopy(CATALOG)
        legacy["conventions"].pop("action_estimate", None)
        legacy["limits"].pop("estimate_interval_ms", None)
        legacy["limits"].pop("estimate_result_bytes", None)
        legacy_hash = hashlib.blake2b(json.dumps(legacy, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode("utf-8"), digest_size=32).hexdigest()
        self.engine.hello_capabilities["conventions"] = copy.deepcopy(legacy["conventions"])
        self.engine.hello_capabilities["limits"] = copy.deepcopy(legacy["limits"])
        self.engine.hello_capabilities["catalog_hash"] = legacy_hash
        with patch("backend.service.CATALOG", legacy), \
                patch("backend.catalog.CATALOG", legacy), \
                patch("backend.catalog.CATALOG_HASH", legacy_hash):
            self.assertNotIn("action_estimate", self.service.handle("GET", "/v1/capabilities", token)[1])
            self.assert_code("estimate_unavailable", lambda: self.service.handle(
                "POST", "/v1/actions/estimate", token, good))
            self.assertEqual(self.engine.count("action.estimate"), 0)
        with self.estimate_enabled():
            bad_bodies = (
                {}, {**good, "request_id": "not-an-action"},
                {**good, "actor": {"mode": "text"}},
                {**good, "preconditions": {"company_cash": 1}},
                {"action": {**good["action"], "credentials": "private"}},
                {"action": {"name": "company.loan.increase", "parameters": {"mode": "visual"}}},
                {"action": {"name": "company.loan.increase", "parameters": {"mode": True}}},
                {"action": {"name": "company.buy", "parameters": {"company_id": 3}}},
            )
            before = self.engine.count("action.estimate")
            for body in bad_bodies:
                with self.subTest(body=body):
                    self.assert_code("invalid_request", lambda: self.service.handle(
                        "POST", "/v1/actions/estimate", token, body))
            self.assertEqual(self.engine.count("action.estimate"), before)
            for name in ("ui.apply", "legacy.unknown"):
                self.assert_code("estimate_unavailable", lambda: self.service.handle(
                    "POST", "/v1/actions/estimate", token,
                    {"action": {"name": name, "parameters": {}}}))
            self.assertEqual(self.engine.count("action.estimate"), before)
            matching_hash = self.engine.hello_capabilities["catalog_hash"]
            self.engine.hello_capabilities["catalog_hash"] = "0" * 64
            self.assert_code("catalog_mismatch", lambda: self.service.handle(
                "POST", "/v1/actions/estimate", token, good))
            self.engine.hello_capabilities["catalog_hash"] = matching_hash
            self.assertEqual(self.engine.count("action.estimate"), before)

            for native_code, expected in (("estimate_rate_limited", ("estimate_rate_limited", 429)),
                                          ("estimate_unavailable", ("estimate_unavailable", 409)),
                                          ("private_diagnostic", ("estimate_unavailable", 409)),
                                          ("checkpoint_busy", ("world_checkpoint_unavailable", 503)),
                                          ("recovery_required", ("world_recovering", 503))):
                self.engine.estimate_reply = {"status": "rejected", "error": {
                    "code": native_code, "message": "private detail"}}
                with self.subTest(native_code=native_code), self.assertRaises(ServiceError) as caught:
                    self.service.handle("POST", "/v1/actions/estimate", token, good)
                self.assertEqual((caught.exception.code, caught.exception.status), expected)
            self.engine.estimate_reply = {"status": "completed", "result": {
                "success": False, "cost": 0, "advisory": True,
                "test_and_execute_may_differ": False,
                "error": {"code": "gameplay_rejected", "message": "Tracks obstructed",
                          "secret": "private"}, "location": {"private": True}}}
            self.assertEqual(self.service.handle("POST", "/v1/actions/estimate", token, good),
                (200, {"estimate": {"success": False, "cost": 0, "advisory": True,
                                    "test_and_execute_may_differ": False,
                                    "error": {"code": "gameplay_rejected",
                                              "message": "Tracks obstructed"}}}))
            self.engine.estimate_reply = {"status": "completed", "result": {
                "success": True, "cost": True, "advisory": True,
                "test_and_execute_may_differ": False}}
            with self.assertRaises(ServiceError) as malformed:
                self.service.handle("POST", "/v1/actions/estimate", token, good)
            self.assertEqual((malformed.exception.code, malformed.exception.status),
                             ("engine_unavailable", 503))
            self.engine.estimate_reply = {"status": "completed", "engine_instance_id": "old",
                "result": {"success": True, "cost": 1, "advisory": True,
                           "test_and_execute_may_differ": False}}
            self.assert_code("world_recovering", lambda: self.service.handle(
                "POST", "/v1/actions/estimate", token, good))
            self.assertEqual(self.store.max_request_sequence("w1"), 0)

    def test_estimate_participant_target_uses_fresh_filtered_roster(self):
        renderer = FakeRenderer()
        renderer.native_worker_keys.add("a" * 64)
        self.service.renderer = renderer
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        human = {"id": 12, "generation": 3, "engine_instance_id": "boot"}
        worker = {"id": 7, "generation": 2, "engine_instance_id": "boot"}
        self.engine.roster_rows = [
            {"public_key": "b" * 64, "company_id": 2, "label": "Human", "native_handle": human},
            {"public_key": "a" * 64, "company_id": 2, "label": "Worker", "native_handle": worker},
        ]
        self.engine.estimate_reply = {"status": "rejected", "error": {
            "code": "estimate_unavailable", "message": "private NoEst detail"}}
        def request(handle):
            return {"action": {"name": "company.participant.authorize",
                               "parameters": {"participant": handle, "allowed": True}}}
        with self.estimate_enabled():
            self.assert_code("participant_unavailable", lambda: self.service.handle(
                "POST", "/v1/actions/estimate", token, request(worker)))
            self.assertEqual(self.engine.count("action.estimate"), 0)
            self.assert_code("estimate_unavailable", lambda: self.service.handle(
                "POST", "/v1/actions/estimate", token, request(human)))
            self.assertEqual(self.engine.count("action.estimate"), 1)
        self.assertEqual((renderer.validations, renderer.prepared, renderer.captures), ([], [], []))
        self.assertEqual(self.store.max_request_sequence("w1"), 0)

    def test_estimate_first_bind_checkpoint_and_world_authority_are_503(self):
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        request = {"action": {"name": "company.loan.increase",
                              "parameters": {"mode": "max"}}}
        with self.estimate_enabled():
            for code, expected in (("checkpoint_busy", "world_checkpoint_unavailable"),
                                   ("engine_fence", "world_recovering"),
                                   ("world_unavailable", "engine_unavailable")):
                self.engine.bind_reply = {"status": "rejected", "error": {
                    "code": code, "message": "private"}}
                with self.subTest(code=code), self.assertRaises(ServiceError) as caught:
                    self.service.handle("POST", "/v1/actions/estimate", token, request)
                self.assertEqual((caught.exception.code, caught.exception.status), (expected, 503))
                self.assertEqual(self.engine.count("action.estimate"), 0)
            self.engine.bind_reply = {"status": "completed", "result": {"bound": True}}
            self.engine.estimate_reply = {"status": "rejected", "error": {
                "code": "world_unavailable", "message": "private"}}
            with self.assertRaises(ServiceError) as caught:
                self.service.handle("POST", "/v1/actions/estimate", token, request)
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("engine_unavailable", 503))
            self.assertEqual(self.engine.count("action.estimate"), 1)
        self.assertEqual(self.store.max_request_sequence("w1"), 0)

    def test_catalog_hash_mismatch_blocks_fresh_work_but_preserves_historical_receipt(self):
        historical = self.action(request_id="historical", name="legacy.retired",
                                 parameters={"legacy": 7}, preconditions={"old": True})
        self.store.accept_request(self.principal, "historical", {
            "action": historical["action"], "preconditions": historical["preconditions"],
        })
        self.store.update_request(self.principal, "historical", "completed", result={"success": True})
        self.engine.hello_capabilities["catalog_hash"] = "0" * 64
        before = len(self.engine.calls)
        self.assertEqual(self.service.handle("GET", "/v1/actions/historical", self.token)[1]["status"],
                         "completed")
        self.assertEqual(self.service.handle("POST", "/v1/actions", self.token, historical)[0], 200)
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", self.token,
            {**historical, "preconditions": {"old": False}}))
        self.assertEqual(len(self.engine.calls), before)
        self.assert_code("catalog_mismatch", lambda: self.service.handle(
            "POST", "/v1/actions", self.token, self.action(request_id="fresh")))
        self.assert_code("catalog_mismatch", lambda: self.service.handle(
            "POST", "/v1/observations", self.token,
            {"query": "company", "parameters": {}}))
        self.assertEqual(self.store.max_request_sequence("w1"), 1)
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(self.engine.count("observation.query"), 0)

    def test_malformed_catalog_forms_fail_closed_before_new_admission(self):
        valid = copy.deepcopy(self.engine.hello_capabilities)
        variants = {
            "missing hash": lambda cap: cap.pop("catalog_hash"),
            "legacy dict specs": lambda cap: cap.__setitem__("actions", {"road.build": {}}),
            "duplicate names": lambda cap: cap["actions"].append(cap["actions"][0]),
            "wrong order": lambda cap: cap["actions"].reverse(),
            "boolean version": lambda cap: cap.__setitem__("version", True),
        }
        for number, (label, change) in enumerate(variants.items()):
            with self.subTest(label=label):
                self.engine.hello_capabilities = copy.deepcopy(valid)
                change(self.engine.hello_capabilities)
                self.assert_code("catalog_mismatch", lambda: self.service.handle(
                    "POST", "/v1/actions", self.token, self.action(request_id=f"bad-{number}")))
                self.assertEqual(self.store.max_request_sequence("w1"), 0)
                self.assertEqual(self.engine.count("action.submit"), 0)

    def test_wrong_hello_role_and_refresh_mismatch_clear_cache_until_compatible(self):
        self.engine.hello_role = "renderer"
        with self.assertRaises(ServiceError) as role_error:
            self.service.handle("POST", "/v1/actions", self.token, self.action(request_id="first"))
        self.assertEqual(role_error.exception.status, 503)
        self.assertEqual(self.store.max_request_sequence("w1"), 0)
        self.engine.hello_role = "world"
        self.service._hello(self.principal)
        self.assertTrue(self.service._actions)
        self.engine.hello_capabilities["catalog_hash"] = "0" * 64
        self.assert_code("catalog_mismatch", lambda: self.service._hello(self.principal, refresh=True))
        self.assertIsNone(self.service._hello_value)
        self.assertEqual((self.service._actions, self.service._queries), ({}, {}))
        self.assert_code("catalog_mismatch", lambda: self.service.handle(
            "POST", "/v1/actions", self.token, self.action(request_id="fresh")))
        self.assertEqual(self.store.max_request_sequence("w1"), 0)
        self.engine.hello_capabilities["catalog_hash"] = CATALOG_HASH
        self.assertEqual(self.service.handle(
            "POST", "/v1/actions", self.token, self.action(request_id="fresh"))[0], 202)
        self.assertEqual(self.store.max_request_sequence("w1"), 1)

    def test_any_failed_hello_refresh_clears_old_discovery_cache(self):
        historical = self.action(request_id="saved")
        self.store.accept_request(self.principal, "saved", {
            "action": historical["action"], "preconditions": {},
        })
        self.store.update_request(self.principal, "saved", "completed", result={"success": True})
        self.service.handle("GET", "/v1/me", self.token)
        base = self.engine.valid_hello_reply("w1")
        bad_status = copy.deepcopy(base)
        bad_status["status"] = "rejected"
        missing_instance = copy.deepcopy(base)
        del missing_instance["result"]["engine_instance_id"]
        empty_instance = copy.deepcopy(base)
        empty_instance["result"]["engine_instance_id"] = ""
        wrong_epoch = copy.deepcopy(base)
        wrong_epoch["result"]["world_epoch"] = "other-epoch"
        wrong_outer_world = copy.deepcopy(base)
        wrong_outer_world["world_id"] = "other-world"
        cases = [
            ("status", bad_status, None),
            ("malformed result", {"status": "completed", "result": []}, None),
            ("missing instance", missing_instance, None),
            ("empty instance", empty_instance, None),
            ("wrong epoch", wrong_epoch, None),
            ("wrong outer world", wrong_outer_world, None),
            ("transport error", None, ServiceError("engine_unavailable", status=503)),
        ]
        for number, (label, bad_reply, error) in enumerate(cases):
            with self.subTest(label=label):
                self.engine.hello_reply_override = None
                self.engine.hello_error = None
                self.service._hello(self.principal, refresh=True)
                self.assertTrue(self.service._actions)
                self.assertTrue(self.service._queries)
                self.engine.hello_reply_override = bad_reply
                self.engine.hello_error = error
                with self.assertRaises(ServiceError) as caught:
                    self.service._hello(self.principal, refresh=True)
                self.assertEqual(caught.exception.status, 503)
                self.assertIsNone(self.service._hello_value)
                self.assertEqual((self.service._actions, self.service._queries), ({}, {}))
                before = len(self.engine.calls)
                self.assertEqual(self.service.handle("GET", "/v1/actions/saved", self.token)[1]["status"],
                                 "completed")
                self.assertEqual(self.service.handle("POST", "/v1/actions", self.token, historical)[0], 200)
                self.assertEqual(len(self.engine.calls), before)
                with self.assertRaises(ServiceError) as fresh_error:
                    self.service.handle("POST", "/v1/actions", self.token,
                                        self.action(request_id=f"fresh-{number}"))
                self.assertEqual(fresh_error.exception.status, 503)
                self.assertEqual(self.store.max_request_sequence("w1"), 1)
        self.engine.hello_reply_override = None
        self.engine.hello_error = None
        self.assertEqual(self.service.handle("POST", "/v1/actions", self.token,
                                             self.action(request_id="fresh-after"))[0], 202)
        self.assertEqual(self.store.max_request_sequence("w1"), 2)

    def test_shared_visual_action_probes_catalog_before_admission(self):
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        self.engine.hello_capabilities["catalog_hash"] = "0" * 64
        spec = copy.deepcopy(action_spec("road.build"))
        spec["modes"] = ["text", "visual"]
        with patch("backend.service.action_spec", return_value=spec):
            self.assert_code("catalog_mismatch", lambda: self.service.handle(
                "POST", "/v1/actions", token, self.action(request_id="raw", name="road.build")))
        self.assertEqual(self.engine.count("hello"), 1)
        self.assertEqual(self.store.max_request_sequence("w1"), 0)

    def test_retired_text_action_with_old_preconditions_replays_but_fresh_policy_applies(self):
        historical = self.action(request_id="retired", name="legacy.retired",
                                 parameters={"legacy_target": 7},
                                 preconditions={"company_cash": 100})
        self.store.accept_request(self.principal, "retired", {
            "action": historical["action"], "preconditions": historical["preconditions"],
        })
        self.store.update_request(self.principal, "retired", "completed",
                                  result={"success": True, "cost": 9, "secret": "drop"})
        self.service.close()
        self.service = Service(self.store, self.engine, autostart=False, world_id="w1")
        before = len(self.engine.calls)
        code, replayed = self.service.handle("POST", "/v1/actions", self.token, historical)
        self.assertEqual(code, 200)
        self.assertEqual(replayed["status"], "completed")
        self.assertEqual(replayed["result"], {"success": True, "cost": 9})
        self.assertEqual(self.service.handle("GET", "/v1/actions/retired", self.token)[1], replayed)
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", self.token,
            {**historical, "preconditions": {"company_cash": 101}}
        ))
        self.assert_code("unsupported_action", lambda: self.service.handle(
            "POST", "/v1/actions", self.token,
            self.action(request_id="fresh-retired", name="legacy.retired",
                        parameters={"legacy_target": 7})
        ))
        self.assert_code("invalid_request", lambda: self.service.handle(
            "POST", "/v1/actions", self.token,
            self.action(request_id="fresh-preconditions", preconditions={"company_cash": 100})
        ))
        self.assertEqual(len(self.engine.calls), before)
        self.assertEqual(self.store.max_request_sequence("w1"), 1)

    def test_historical_visual_tool_receipt_replays_without_renderer(self):
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        principal = self.store.authenticate(token)
        historical = self.action(request_id="old-tool", name="tool.apply",
                                 parameters={"tool": "road.build", "view_id": "old-view",
                                             "screen_target": {"x": 1, "y": 2}},
                                 preconditions={"renderer_generation": "g1"})
        self.store.accept_request(principal, "old-tool", {
            "action": historical["action"], "preconditions": historical["preconditions"],
        })
        self.store.update_request(principal, "old-tool", "rejected",
                                  error={"code": "old_tool_failed", "secret": "private"})
        self.service.close()
        self.service = Service(self.store, self.engine, autostart=False, world_id="w1", renderer=None)
        before = len(self.engine.calls)
        code, replayed = self.service.handle("POST", "/v1/actions", token, historical)
        self.assertEqual((code, replayed), (200, {
            "request_id": "old-tool", "status": "rejected",
            "checkpoint_state": "uncheckpointed", "error": {"code": "old_tool_failed"}}))
        self.assertEqual(self.service.handle("GET", "/v1/actions/old-tool", token)[1], replayed)
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", token,
            {**historical, "action": {"name": "tool.apply", "parameters": {
                **historical["action"]["parameters"], "screen_target": {"x": 3, "y": 2}}}}
        ))
        self.assert_code("unsupported_action", lambda: self.service.handle(
            "POST", "/v1/actions", token,
            self.action(request_id="fresh-tool", name="tool.apply",
                        parameters=historical["action"]["parameters"])
        ))
        self.assertEqual(len(self.engine.calls), before)
        self.assertEqual(self.store.max_request_sequence("w1"), 1)

    def test_terminal_v1_visual_intent_receipt_replays_without_renderer(self):
        token, principal, _ = self.visual_actor()
        body = self.visual(request_id="old-v1")
        self.store.accept_request(principal, "old-v1", {
            "action": body["action"], "preconditions": {},
        })
        self.store.begin_intent_capture(principal, "old-v1", "worker-1")
        historical = {"version": 1, "request_id": "old-v1",
                      "image_id": "image-1", "world_epoch": "initial",
                      "worker_instance_id": "worker-1", "commands": [],
                      "tiles": []}
        self.store.persist_visual_intent(principal, "old-v1", historical)
        self.store.update_request(principal, "old-v1", "completed",
                                  result={"success": True})
        self.service.close()
        self.service = Service(self.store, self.engine, renderer=None,
                               autostart=False, world_id="w1")
        before = len(self.engine.calls)
        code, replayed = self.service.handle("POST", "/v1/actions", token, body)
        self.assertEqual((code, replayed["status"]), (200, "completed"))
        self.assertEqual(self.store.get_request(principal, "old-v1")["visual_intent"],
                         historical)
        self.assertEqual(len(self.engine.calls), before)
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", token, self.visual(request_id="old-v1", x=4)))

    def test_historical_vehicle_purchase_projects_two_field_result_handle(self):
        body = self.action(request_id="old-purchase", name="vehicle.build",
                           parameters={"tile": 12, "engine_id": 3})
        self.store.accept_request(self.principal, "old-purchase", {
            "action": body["action"], "preconditions": {},
        })
        old_handle = {"id": 17, "generation": 2}
        self.store.update_request(self.principal, "old-purchase", "completed",
                                  result={"success": True, "vehicle_id": old_handle})
        self.service.close()
        self.service = Service(self.store, self.engine, autostart=False, world_id="w1")
        before = len(self.engine.calls)
        code, replayed = self.service.handle("POST", "/v1/actions", self.token, body)
        self.assertEqual(code, 200)
        self.assertEqual(replayed["result"], {"success": True, "vehicle_id": old_handle})
        self.assertEqual(self.service.handle("GET", "/v1/actions/old-purchase", self.token)[1], replayed)
        self.assertEqual(len(self.engine.calls), before)

    def test_fresh_vehicle_handle_requires_nonempty_engine_instance_id(self):
        invalid = [
            {"id": 12, "generation": 3},
            {"id": 12, "generation": 3, "engine_instance_id": ""},
            {"id": 12, "generation": 3, "engine_instance_id": 17},
            {"id": 12, "generation": 3, "engine_instance_id": "x" * 257},
        ]
        for number, handle in enumerate(invalid):
            with self.subTest(handle=handle):
                self.assert_code("invalid_request", lambda number=number, handle=handle:
                                 self.service.handle("POST", "/v1/actions", self.token,
                                     self.action(request_id=f"bad-handle-{number}",
                                                 name="vehicle.start_stop", parameters={"vehicle_id": handle})))
        self.assertEqual(self.store.max_request_sequence("w1"), 0)
        self.assertEqual(self.store.pending_requests(), [])
        self.assertEqual(self.engine.calls, [])

    def test_valid_three_field_handle_is_forwarded_unchanged_and_engine_rejects_stale(self):
        old_handle = {"id": 12, "generation": 3, "engine_instance_id": "oldboot"}
        body = self.action(request_id="stale", name="vehicle.start_stop",
                           parameters={"vehicle_id": old_handle})
        code, admitted = self.service.handle("POST", "/v1/actions", self.token, body)
        self.assertEqual((code, admitted["status"]), (202, "accepted"))
        completed = self.service._process_record(self.principal, self.store.get_request(self.principal, "stale"))
        self.assertEqual(completed["status"], "rejected")
        submitted = next(call for call in self.engine.calls if call["type"] == "action.submit")
        self.assertEqual(submitted["engine_instance_id"], "boot")
        self.assertEqual(submitted["action"]["parameters"]["vehicle_id"], old_handle)
        self.assertEqual(self.service.handle("GET", "/v1/actions/stale", self.token)[1]["error"],
                         {"code": "stale_handle"})

    def test_participants_filters_only_native_worker_membership_by_key(self):
        renderer = FakeRenderer()
        self.service.renderer = renderer
        worker_key = "a" * 64
        human_key = "b" * 64
        renderer.native_worker_keys.add(worker_key.upper())
        agent_token = self.store.provision("w1", "agent-pilot", 2, kind="agent", display_name="Pilot")
        human_token = self.store.provision("w1", "api-human", 2, kind="human", display_name="Pilot")
        self.service.social.update_native_roster("w1", [
            {"public_key": worker_key, "company_id": 2, "label": "Pilot"},
            {"public_key": human_key, "company_id": 2, "label": "Pilot"},
        ])
        self.engine.roster_rows = [
            {"public_key": worker_key, "company_id": 2, "label": "Pilot"},
            {"public_key": human_key, "company_id": 2, "label": "Pilot"},
        ]
        worker_membership = self.service.social.native_memberships_for_keys("w1", {worker_key})
        human_membership = self.service.social.native_memberships_for_keys("w1", {human_key})
        self.assertNotEqual(worker_membership, human_membership)
        before = len(self.engine.calls)
        code, public = self.service.handle("GET", "/v1/participants", self.token)
        self.assertEqual(code, 200)
        self.assertEqual([call["type"] for call in self.engine.calls[before:]],
                         ["hello", "chat.roster"])
        self.assertEqual((renderer.prepared, renderer.validations, renderer.captures, renderer.releases),
                         ([], [], [], []))
        people = public["participants"]
        ids = {person["identity_id"] for person in people}
        self.assertEqual(len(people), 4)  # caller, API agent, API human, native human
        self.assertIn(self.store.authenticate(agent_token).identity_id, ids)
        self.assertIn(self.store.authenticate(human_token).identity_id, ids)
        self.assertIn(next(iter(human_membership))[0], ids)
        self.assertNotIn(next(iter(worker_membership))[0], ids)
        self.assertEqual(sum(person["display_name"] == "Pilot" for person in people), 3)
        encoded = json.dumps(public)
        self.assertNotIn(worker_key, encoded)
        self.assertNotIn(human_key, encoded)
        self.assertNotIn("public_key", encoded)

    def test_participant_handles_require_fresh_roster_and_renderer_coverage(self):
        renderer = FakeRenderer()
        self.service.renderer = renderer
        human_key, worker_key = "b" * 64, "a" * 64
        human = {"id": 12, "generation": 3, "engine_instance_id": "boot"}
        worker = {"id": 7, "generation": 2, "engine_instance_id": "boot"}
        rows = [
            {"public_key": human_key, "company_id": 2, "label": "Human",
             "native_handle": human},
            {"public_key": worker_key, "company_id": 2, "label": "Worker",
             "native_handle": worker},
        ]
        renderer.native_worker_keys.add(worker_key)
        self.service.social.update_native_roster("w1", rows)
        self.engine.roster_rows = rows
        with patch.object(self.service.social, "update_native_roster",
                          side_effect=AssertionError("GET must not write social state")):
            status, result = self.service.handle("GET", "/v1/participants", self.token)
        self.assertEqual(status, 200)
        native = [row for row in result["participants"] if row["kind"] == "human"
                  and row["identity_id"].startswith("native:")]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0]["native_handle"], human)
        self.assertEqual(renderer.verified_key_calls, 1)
        self.assertNotIn(human_key, json.dumps(result))
        self.assertNotIn(worker_key, json.dumps(result))

        renderer.renderer_identity_unavailable = True
        degraded = self.service.handle("GET", "/v1/participants", self.token)[1]
        self.assertEqual(len([row for row in degraded["participants"]
                              if row["identity_id"].startswith("native:")]), 1)
        self.assertTrue(all("native_handle" not in row for row in degraded["participants"]))
        self.engine.unavailable.add("w1")
        degraded = self.service.handle("GET", "/v1/participants", self.token)[1]
        self.assertTrue(all("native_handle" not in row for row in degraded["participants"]))

        self.engine.unavailable.clear()
        visual = self.store.provision("w1", "viewer", 2, mode="visual")
        visual_rows = self.service.handle("GET", "/v1/participants", visual)[1]["participants"]
        self.assertTrue(all("native_handle" not in row for row in visual_rows))
        self.assertEqual(len([row for row in visual_rows
                              if row["identity_id"].startswith("native:")]), 1)
        renderer.renderer_identity_unavailable = False
        visual_rows = self.service.handle("GET", "/v1/participants", visual)[1]["participants"]
        native = [row for row in visual_rows if row["identity_id"].startswith("native:")]
        self.assertEqual(native[0]["native_handle"], human)

    def test_fresh_participant_authorize_fences_handle_before_sequence(self):
        renderer = FakeRenderer()
        self.service.renderer = renderer
        human = {"id": 12, "generation": 3, "engine_instance_id": "boot"}
        worker = {"id": 7, "generation": 2, "engine_instance_id": "boot"}
        renderer.native_worker_keys.add("a" * 64)
        self.engine.roster_rows = [
            {"public_key": "b" * 64, "company_id": 2, "label": "Human",
             "native_handle": human},
            {"public_key": "b" * 64, "company_id": 3, "label": "Other connection",
             "native_handle": {"id": 4, "generation": 1, "engine_instance_id": "boot"}},
            {"public_key": "a" * 64, "company_id": 2, "label": "Worker",
             "native_handle": worker},
            {"public_key": "c" * 64, "company_id": None, "label": "Local server"},
        ]

        def submit(request_id, handle, allowed=True):
            return self.service.handle("POST", "/v1/actions", self.token,
                self.action(request_id, "company.participant.authorize",
                            {"participant": handle, "allowed": allowed}))

        with patch("backend.service.action_spec", return_value=self.participant_spec()):
            for label, candidate, error in (
                ("bool id", {**human, "id": True}, "invalid_request"),
                ("bool generation", {**human, "generation": False}, "invalid_request"),
                ("overflow generation", {**human, "generation": 1 << 64}, "invalid_request"),
                ("wrong instance", {**human, "engine_instance_id": "old"}, "participant_unavailable"),
                ("reused client", {**human, "generation": 2}, "participant_unavailable"),
                ("pool index", {**human, "id": 0}, "participant_unavailable"),
                ("worker", worker, "participant_unavailable"),
            ):
                with self.subTest(label=label):
                    self.assert_code(error, lambda: submit("bad-" + label.replace(" ", "-"), candidate))
                    self.assertEqual(self.store.max_request_sequence("w1"), 0)
            renderer.renderer_identity_unavailable = True
            self.assert_code("renderer_unavailable", lambda: submit("renderer-down", human))
            self.assertEqual(self.store.max_request_sequence("w1"), 0)
            renderer.renderer_identity_unavailable = False
            self.engine.roster_instance = "old"
            self.assert_code("native_roster_unavailable", lambda: submit("stale-roster", human))
            self.engine.roster_instance = None
            self.assertEqual(self.store.max_request_sequence("w1"), 0)
            code, accepted = submit("allow-human", human)
            self.assertEqual((code, accepted["status"]), (202, "accepted"))
            self.assertEqual(self.store.max_request_sequence("w1"), 1)
            self.assertEqual([call["type"] for call in self.engine.calls[-2:]],
                             ["hello", "chat.roster"])

        self.engine.unavailable.add("w1")
        before = len(self.engine.calls)
        code, repeat = submit("allow-human", human)
        self.assertEqual((code, repeat), (202, accepted))
        self.assertEqual(len(self.engine.calls), before)
        self.assert_code("idempotency_conflict", lambda: submit("allow-human", human, False))
        self.assertEqual(len(self.engine.calls), before)
        self.assertEqual(self.service.handle("GET", "/v1/actions/allow-human", self.token)[1], accepted)

        self.engine.unavailable.clear()
        visual = self.store.provision("w1", "viewer", 2, mode="visual")
        with patch("backend.service.action_spec", return_value=self.participant_spec()):
            self.assert_code("participant_unavailable", lambda: self.service.handle("POST", "/v1/actions", visual,
                self.action("visual-worker", "company.participant.authorize",
                            {"participant": worker, "allowed": True})))
            code, admitted = self.service.handle("POST", "/v1/actions", visual,
                self.action("visual-authorize", "company.participant.authorize",
                            {"participant": human, "allowed": True}))
        self.assertEqual((code, admitted["status"]), (202, "accepted"))
        self.assertEqual(self.store.get_request(self.store.authenticate(visual), "visual-authorize")["intent_state"], "none")

    def test_company_access_target_uses_fresh_filtered_roster(self):
        renderer = FakeRenderer()
        self.service.renderer = renderer
        human = {"id": 12, "generation": 3, "engine_instance_id": "boot"}
        worker = {"id": 7, "generation": 2, "engine_instance_id": "boot"}
        renderer.native_worker_keys.add("a" * 64)
        self.engine.roster_rows = [
            {"public_key": "b" * 64, "company_id": 2, "label": "Human",
             "native_handle": human},
            {"public_key": "a" * 64, "company_id": 2, "label": "Worker",
             "native_handle": worker},
        ]
        original_hello = self.service._hello

        def access_hello(*args, **kwargs):
            value = original_hello(*args, **kwargs)
            self.service._queries["company.access"] = {}
            return value

        def read(params):
            return self.service.handle("POST", "/v1/observations", self.token,
                                       {"query": "company.access", "parameters": params})

        with patch.object(self.service, "_hello", side_effect=access_hello), \
                patch("backend.service.query_spec", return_value=self.access_spec()):
            code, result = read({"participant": human})
            self.assertEqual(code, 200)
            self.assertEqual(result["observation"]["participant"], human)
            self.assertEqual(result["observation"]["join_policy"], "listed")
            before = self.engine.count("observation.query")
            self.assert_code("participant_unavailable", lambda: read({"participant": worker}))
            self.assert_code("participant_unavailable", lambda: read({
                "participant": {**human, "generation": 2}}))
            self.assertEqual(self.engine.count("observation.query"), before)
            renderer.renderer_identity_unavailable = True
            self.assert_code("renderer_unavailable", lambda: read({"participant": human}))
            self.assertEqual(self.engine.count("observation.query"), before)
            code, own = read({})
            self.assertEqual(code, 200)
            self.assertEqual(own["observation"]["join_policy"], "listed")
            self.assertNotIn("participant", own["observation"])
            self.assertEqual(self.engine.count("observation.query"), before + 1)

    def test_get_orders_live_detail_requires_exact_boolean_strings(self):
        spec = copy.deepcopy(next(entry for entry in CATALOG["queries"]
                                  if entry["name"] == "orders"))
        handle = {"id": 4, "generation": 7, "engine_instance_id": "boot"}
        with patch("backend.service.query_spec", return_value=spec):
            for raw, expected in (("true", True), ("false", False)):
                with self.subTest(raw=raw):
                    code, _ = self.service.handle("GET", "/v1/observations", self.token,
                        query={"query": "orders", "vehicle_id": handle,
                               "include_live_detail": raw})
                    self.assertEqual(code, 200)
                    sent = next(call for call in reversed(self.engine.calls)
                                if call["type"] == "observation.query")
                    self.assertIs(sent["parameters"]["include_live_detail"], expected)
            before = self.engine.count("observation.query")
            for raw in ("True", "FALSE", "1", "", None, True, False, 0):
                with self.subTest(raw=raw):
                    self.assert_code("invalid_request", lambda: self.service.handle(
                        "GET", "/v1/observations", self.token,
                        query={"query": "orders", "vehicle_id": handle,
                               "include_live_detail": raw}))
            self.assertEqual(self.engine.count("observation.query"), before)

            code, _ = self.service.handle("POST", "/v1/observations", self.token,
                {"query": "orders", "parameters": {"vehicle_id": handle,
                                                    "include_live_detail": True}})
            self.assertEqual(code, 200)
            self.assert_code("invalid_request", lambda: self.service.handle(
                "POST", "/v1/observations", self.token,
                {"query": "orders", "parameters": {"vehicle_id": handle,
                                                    "include_live_detail": "true"}}))
    def test_get_display_text_preserves_kind_and_normalizes_byte_cursor(self):
        spec = {"name": "display.text", "parameters": {
            "type": "object", "required": ["kind", "cargo_id"],
            "additionalProperties": False, "properties": {
                "kind": {"type": "string", "enum": ["cargo_name"]},
                "cargo_id": {"type": "integer", "minimum": 0, "maximum": 63},
                "after_byte": {"type": "integer", "minimum": 0,
                               "maximum": 0xFFFFFFFFFFFFFFFF},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2048},
                "expected_content_version": {"type": "string", "minLength": 64,
                                             "maxLength": 64}}},
            "result_fields": ["text"]}
        query = {"query": "display.text", "kind": "cargo_name", "cargo_id": "0",
                 "after_byte": str(0xFFFFFFFFFFFFFFFF), "limit": "2048",
                 "expected_content_version": "a" * 64}
        hello = self.engine.valid_hello_reply("w1")["result"]
        with patch("backend.service.query_spec", return_value=spec), patch.object(
                self.service, "_hello", return_value=hello):
            code, _ = self.service.handle("GET", "/v1/observations", self.token, query=query)
            self.assertEqual(code, 200)
            sent = next(call for call in reversed(self.engine.calls)
                        if call["type"] == "observation.query")
            self.assertEqual(sent["parameters"], {
                "kind": "cargo_name", "cargo_id": 0, "after_byte": 0xFFFFFFFFFFFFFFFF,
                "limit": 2048, "expected_content_version": "a" * 64})
            before = self.engine.count("observation.query")
            for raw in (True, False, None, 1.5, -1, "-1", "+1", " 1", "1.0",
                        str(0x10000000000000000)):
                with self.subTest(raw=raw):
                    self.assert_code("invalid_request", lambda: self.service.handle(
                        "GET", "/v1/observations", self.token,
                        query={**query, "after_byte": raw}))
            self.assertEqual(self.engine.count("observation.query"), before)

    def test_decision_information_uses_existing_text_observation_route(self):
        integer = lambda maximum: {"type": "integer", "minimum": 0, "maximum": maximum}
        def spec(name, properties, required, fields):
            return {"name": name, "modes": ["text"], "parameters": {
                "type": "object", "required": required, "additionalProperties": False,
                "properties": properties}, "result_fields": fields}
        specs = {
            "cargo.payment_rates": spec("cargo.payment_rates", {"cargo_id": integer(63)},
                ["cargo_id"], ["tick", "company_id", "cargo_id", "samples"]),
            "catalog.bridges": spec("catalog.bridges", {}, [],
                ["tick", "company_id", "items"]),
            "engine.purchase_info": spec("engine.purchase_info",
                {"engine_id": integer(63999)}, ["engine_id"],
                ["tick", "company_id", "engine_id", "performance"]),
        }
        replies = {
            "cargo.payment_rates": {"tick": 7, "company_id": 1, "cargo_id": 3,
                "samples": [{"transit_periods": 4, "display_time": 10, "income": -15,
                             "private": 1}], "private": 1},
            "catalog.bridges": {"tick": 7, "company_id": 1,
                "items": [{"bridge_type": 0, "maximum_speed": None,
                           "private": 1}], "private": 1},
            "engine.purchase_info": {"tick": 7, "company_id": 1,
                "engine_id": 5, "performance": {"power_hp": 1000,
                                                   "private": 1}, "private": 1},
        }
        original_call = self.engine.call
        def call(envelope):
            if envelope["type"] == "observation.query":
                self.engine.calls.append(copy.deepcopy(envelope))
                return {"status": "completed", "result": replies[envelope["query"]]}
            return original_call(envelope)
        hello = self.engine.valid_hello_reply("w1")["result"]
        self.service._queries = {name: {} for name in specs}
        with patch("backend.service.query_spec", side_effect=specs.get), \
                patch.object(self.service, "_hello", return_value=hello), \
                patch.object(self.engine, "call", side_effect=call):
            for name, params, expected in (
                    ("cargo.payment_rates", {"cargo_id": 3},
                     {"tick": 7, "company_id": 1, "cargo_id": 3, "samples": [
                         {"transit_periods": 4, "display_time": 10, "income": -15}]}),
                    ("catalog.bridges", {}, {"tick": 7, "company_id": 1,
                        "items": [{"bridge_type": 0, "maximum_speed": None}]}),
                    ("engine.purchase_info", {"engine_id": 5},
                     {"tick": 7, "company_id": 1, "engine_id": 5,
                      "performance": {"power_hp": 1000}})):
                with self.subTest(name=name):
                    code, result = self.service.handle("POST", "/v1/observations",
                        self.token, {"query": name, "parameters": params})
                    self.assertEqual((code, result["observation"]), (200, expected))
                    sent = [call for call in self.engine.calls if call["type"] ==
                            "observation.query"][-1]
                    self.assertEqual((sent["query"], sent["parameters"]), (name, params))
                    self.assertEqual(sent["actor"]["company_id"], 1)
                    self.assertEqual(sent["actor"]["mode"], "text")
            for name, key, raw in (("cargo.payment_rates", "cargo_id", "3"),
                                   ("engine.purchase_info", "engine_id", "5")):
                code, result = self.service.handle("GET", "/v1/observations",
                    self.token, query={"query": name, key: raw})
                self.assertEqual(code, 200)
                self.assertEqual(result["observation"][key], int(raw))
                sent = [call for call in self.engine.calls if call["type"] ==
                        "observation.query"][-1]
                self.assertEqual(sent["parameters"], {key: int(raw)})
            code, _ = self.service.handle("GET", "/v1/observations", self.token,
                                          query={"query": "catalog.bridges"})
            self.assertEqual(code, 200)
            before = self.engine.count("observation.query")
            for name, params in (
                    ("cargo.payment_rates", {"cargo_id": True}),
                    ("cargo.payment_rates", {"cargo_id": 64}),
                    ("catalog.bridges", {"limit": 1}),
                    ("engine.purchase_info", {"engine_id": -1}),
                    ("engine.purchase_info", {"engine_id": 64000}),
                    ("engine.purchase_info", {"engine_id": 5, "private": 1})):
                with self.subTest(name=name, params=params):
                    self.assert_code("invalid_request", lambda: self.service.handle(
                        "POST", "/v1/observations", self.token,
                        {"query": name, "parameters": params}))
            for raw in (True, "-1", "+1", " 1", "1.0", "64000",
                        str(0x10000000000000000)):
                with self.subTest(raw=raw):
                    self.assert_code("invalid_request", lambda: self.service.handle(
                        "GET", "/v1/observations", self.token,
                        query={"query": "engine.purchase_info", "engine_id": raw}))
            self.assertEqual(self.engine.count("observation.query"), before)
            visual_token = self.store.provision("w1", "decision_visual", 1, mode="visual")
            before_calls = len(self.engine.calls)
            for name, params in (("cargo.payment_rates", {"cargo_id": 3}),
                                 ("catalog.bridges", {}),
                                 ("engine.purchase_info", {"engine_id": 5})):
                for method in ("POST", "GET"):
                    with self.subTest(name=name, method=method):
                        if method == "POST":
                            operation = lambda: self.service.handle(method,
                                "/v1/observations", visual_token,
                                {"query": name, "parameters": params})
                        else:
                            operation = lambda: self.service.handle(method,
                                "/v1/observations", visual_token,
                                query={"query": name, **params})
                        self.assert_code("forbidden", operation)
            self.assertEqual(len(self.engine.calls), before_calls)
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(self.store.max_request_sequence("w1"), 0)

    def test_get_question_generation_cursor_is_strict_and_preserved(self):
        spec = {"name": "goal.questions", "parameters": {"anyOf": [
            {"type": "object", "additionalProperties": False, "properties": {
                "after_id": {"type": "integer", "enum": [-1]},
                "after_generation": {"type": "integer", "enum": [0]}}},
            {"type": "object", "required": ["after_id"],
             "additionalProperties": False, "properties": {
                "after_id": {"type": "integer", "minimum": 0, "maximum": 65535},
                "after_generation": {"type": "integer", "minimum": 0,
                                     "maximum": 0xFFFFFFFFFFFFFFFF}}}]},
            "result_fields": ["items", "next_after_id", "next_after_generation"]}
        with patch("backend.service.query_spec", return_value=spec):
            for cursor, expected in (
                ({"after_id": "-1", "after_generation": "0"},
                 {"after_id": -1, "after_generation": 0}),
                ({"after_id": "7", "after_generation": str(0xFFFFFFFFFFFFFFFF)},
                 {"after_id": 7, "after_generation": 0xFFFFFFFFFFFFFFFF}),
                ({"after_id": "7"}, {"after_id": 7}),
            ):
                with self.subTest(cursor=cursor):
                    code, _ = self.service.handle("GET", "/v1/observations", self.token,
                        query={"query": "goal.questions", **cursor})
                    self.assertEqual(code, 200)
                    sent = next(call for call in reversed(self.engine.calls)
                                if call["type"] == "observation.query")
                    self.assertEqual(sent["parameters"], expected)
            before = self.engine.count("observation.query")
            for key, invalids in (
                ("after_generation", (True, False, None, 1.5, -1, "-1", "+1", " 1",
                                      "1.0", str(0x10000000000000000))),
                ("after_id", (True, False, None, 1.5, -2, "-2", "+1", " 1", "65536")),
            ):
                for raw in invalids:
                    with self.subTest(key=key, raw=raw):
                        self.assert_code("invalid_request", lambda: self.service.handle(
                            "GET", "/v1/observations", self.token,
                            query={"query": "goal.questions", "after_id": "7", key: raw}))
            self.assert_code("invalid_request", lambda: self.service.handle(
                "GET", "/v1/observations", self.token,
                query={"query": "goal.questions", "after_id": "-1", "after_generation": "1"}))
            self.assertEqual(self.engine.count("observation.query"), before)

    def test_visual_mode_refuses_structured_and_unknown_raw_surfaces(self):
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        self.assert_code("forbidden", lambda: self.service.handle(
            "GET", "/v1/observations", token, query={"kind": "vehicles"}
        ))
        self.assert_code("unsupported_action", lambda: self.service.handle(
            "POST", "/v1/actions", token,
            self.action(name="tool.apply", parameters={"tool": "road.build", "view_id": "v1",
                                                       "screen_target": {"x": 3, "y": 4}})
        ))
        self.assertEqual(self.engine.calls, [])

    def test_worker_submits_once_then_polls_queued(self):
        self.engine.submit_reply = {"status": "queued"}
        self.service.handle("POST", "/v1/actions", self.token, self.action())
        self.assertIsNone(self.process())
        queued = self.store.get_request(self.principal, "r1")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["result"], {"engine_instance_id": "boot"})
        self.assertEqual(self.engine.count("action.submit"), 1)
        final = self.process()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.assertEqual(self.engine.count("action.status"), 1)

    def test_reconnect_marks_queued_uncertain_without_resubmit(self):
        self.engine.submit_reply = {"status": "queued"}
        self.service.handle("POST", "/v1/actions", self.token, self.action())
        self.process()
        self.engine.instances["w1"] = "restart"
        result = self.process()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.engine.count("action.submit"), 1)
        self.assertEqual(self.engine.count("action.status"), 0)

    def test_engine_error_projection_and_visual_status_are_generic(self):
        self.engine.submit_reply = {"status": "rejected", "error": {
            "code": "invalid", "save_path": "/secret/save", "raw_command": "secret command"},
            "result": {"success": False, "credentials": "secret token"}}
        self.service.handle("POST", "/v1/actions", self.token, self.action())
        self.process()
        _, public = self.service.handle("GET", "/v1/actions/r1", self.token)
        self.assertEqual(public["error"], {"code": "invalid"})
        self.assertEqual(public["result"], {"success": False})
        self.assertNotIn("secret", str(self.service.events(self.token)))
        visual_token = self.store.provision("w1", "viewer", 2, mode="visual")
        visual = self.store.authenticate(visual_token)
        # Inject a durable internal record to exercise projection without
        # advertising any public visual mutation endpoint.
        self.store.accept_request(visual, "v1", {"action": {"name": "tool.apply", "parameters": {}},
                                                   "preconditions": {}})
        self.store.update_request(visual, "v1", "rejected",
                                  error={"code": "invalid", "detail": "private"},
                                  result={"money": 900}, tick=88)
        self.store.append_event(visual, "action.status", {"request_id": "v1", "status": "rejected",
                                                          "money": 900, "detail": "private"})
        _, projected = self.service.handle("GET", "/v1/actions/v1", visual_token)
        self.assertEqual(projected, {"request_id": "v1", "status": "rejected",
                                     "checkpoint_state": "uncheckpointed",
                                     "result": {}, "error": {"code": "invalid"}})
        self.assertTrue(all(event["data"] == projected
                            for event in self.service.events(visual_token)["events"]
                            if event["type"] == "action.status"))

    def test_group_handle_receipt_is_text_only_and_visual_events_hide_private_result(self):
        group = {"id": 7, "generation": 3, "engine_instance_id": "boot"}
        self.store.accept_request(self.principal, "group-result", {
            "action": {"name": "group.create", "parameters": {"name": "Depot group"}},
            "preconditions": {},
        })
        self.store.update_request(self.principal, "group-result", "completed", result={
            "success": True, "cost": 25, "group_id": group, "native_debug": "secret",
        })
        _, text_receipt = self.service.handle("GET", "/v1/actions/group-result", self.token)
        self.assertEqual(text_receipt["result"], {"success": True, "cost": 25, "group_id": group})
        self.assertNotIn("secret", json.dumps(text_receipt))

        visual_token, visual, _ = self.visual_actor()
        self.store.accept_request(visual, "visual-result", {
            "action": {"name": "ui.apply", "parameters": {"image_id": "image-1"}},
            "preconditions": {},
        })
        private = {"success": True, "cost": 25, "group_id": group, "native_debug": "secret"}
        self.store.update_request(visual, "visual-result", "completed", result=private, tick=17)
        self.store.append_event(visual, "action.status", {
            "request_id": "visual-result", "status": "completed", "result": private,
            "tick": 17, "checkpoint_state": "checkpointed",
        })
        expected = {"request_id": "visual-result", "status": "completed"}
        self.assertEqual(self.service.handle("GET", "/v1/actions/visual-result", visual_token)[1], expected)
        self.assertEqual(self.service.events(visual_token)["events"][0]["data"], expected)

    def test_visual_semantic_action_uses_ordinary_ledger_when_renderer_unavailable(self):
        renderer = FakeRenderer()
        renderer.available = lambda principal: False
        self.service.renderer = renderer
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        actor = self.store.authenticate(token)
        group = {"id": 7, "generation": 3, "engine_instance_id": "boot"}
        spec = copy.deepcopy(action_spec("group.create"))
        spec["modes"] = ["text", "visual"]
        original_spec = action_spec

        def shared_spec(name):
            return spec if name == "group.create" else original_spec(name)

        self.engine.submit_reply = {"status": "queued"}
        self.engine.status_reply = {"status": "completed", "tick": 17, "result": {
            "success": True, "cost": 25, "group_id": group,
            "capacity": 999, "raw_result": "private"}}
        body = self.action("semantic", "group.create", {"vehicle_type": "road"})
        with patch("backend.service.action_spec", side_effect=shared_spec):
            code, admitted = self.service.handle("POST", "/v1/actions", token, body)
            self.assertEqual((code, admitted), (202, {
                "request_id": "semantic", "status": "accepted",
                "checkpoint_state": "uncheckpointed"}))
            self.assertIsNone(self.service._process_record(
                actor, self.store.get_request(actor, "semantic")))
            queued = self.store.get_request(actor, "semantic")
            self.assertEqual(queued["intent_state"], "none")
            self.assertIsNone(queued["visual_intent"])
            self.assertNotIn("authority_queued", queued["result"])
            completed = self.service._process_record(actor, queued)
            self.assertEqual(completed["status"], "completed")
            public = self.service.handle("GET", "/v1/actions/semantic", token)[1]
            self.assertEqual(public, {"request_id": "semantic", "status": "completed",
                                      "checkpoint_state": "uncheckpointed",
                                      "result": {"success": True, "cost": 25, "group_id": group}})
            replay = [event["data"] for event in self.service.events(token)["events"]
                      if event["type"] == "action.status"]
            self.assertTrue(all(event == public for event in replay))
            self.engine.unavailable.add("w1")
            before_retry = len(self.engine.calls)
            self.assertEqual(self.service.handle("POST", "/v1/actions", token, body), (200, public))
            self.assertEqual(len(self.engine.calls), before_retry)
        self.service._actions.pop("group.create", None)
        with patch("backend.service.action_spec", return_value=None):
            self.assertEqual(self.service.handle("GET", "/v1/actions/semantic", token)[1], public)
            self.assertEqual(self.service.handle("POST", "/v1/actions", token, body), (200, public))
            replay = [event["data"] for event in self.service.events(token)["events"]
                      if event["type"] == "action.status"]
            self.assertTrue(all(event == public for event in replay))
        self.assertEqual(len(self.engine.calls), before_retry)
        submit = next(call for call in self.engine.calls if call["type"] == "action.submit")
        self.assertNotIn("visual_intent", submit)
        status = next(call for call in self.engine.calls if call["type"] == "action.status")
        self.assertNotIn("visual_intent", status)
        self.assertEqual(self.engine.count("observation.query"), 0)
        self.assertEqual((renderer.validations, renderer.prepared, renderer.captures), ([], [], []))
        self.assertEqual(self.store.unsynced_visual_requests("w1"), [])

    def test_control_reference_uses_catalog_shape_and_private_read_rpc(self):
        token = self.store.provision("w1", "viewer", 2, mode="visual")
        handle = {"id": 7, "generation": 3, "engine_instance_id": "boot"}
        descriptor = {"parameters": {"anyOf": [{"type": "object", "required": ["kind", "id"],
            "additionalProperties": False, "properties": {
                "kind": {"type": "string", "enum": ["group"]},
                "id": {"type": "integer", "minimum": 0, "maximum": 63999}}}]},
            "result_fields": ["kind", "reference", "order_version", "page", "page_version", "engine_id"]}
        catalog = copy.deepcopy(CATALOG)
        catalog["conventions"]["control_reference"] = descriptor
        self.engine.reference_reply = {"status": "completed", "result": {
            "kind": "group", "reference": handle, "private": "hidden"}}
        with patch("backend.service.CATALOG", catalog):
            capabilities = self.service.handle("GET", "/v1/capabilities", token)[1]
            self.assertEqual(capabilities["control_reference"], {
                "endpoint": "/v1/control/reference", **descriptor})
            before = self.engine.count("control.reference")
            self.assert_code("invalid_request", lambda: self.service.handle(
                "POST", "/v1/control/reference", token, {"kind": "group", "id": True}))
            self.assertEqual(self.engine.count("control.reference"), before)
            code, result = self.service.handle("POST", "/v1/control/reference", token,
                                               {"kind": "group", "id": 7})
            self.assertEqual((code, result), (200, {"kind": "group", "reference": handle}))
            call = next(call for call in self.engine.calls if call["type"] == "control.reference")
            self.assertEqual(call["parameters"], {"kind": "group", "id": 7})
            self.assertEqual(call["actor"]["mode"], "visual")
            self.engine.reference_reply = {"status": "rejected", "error": {
                "code": "private", "message": "secret"}}
            self.assert_code("control_reference_unavailable", lambda: self.service.handle(
                "POST", "/v1/control/reference", token, {"kind": "group", "id": 7}))
            self.engine.reference_reply = {"status": "completed", "result": {
                "kind": "group", "reference": {**handle, "engine_instance_id": "old"}}}
            self.assert_code("control_reference_unavailable", lambda: self.service.handle(
                "POST", "/v1/control/reference", token, {"kind": "group", "id": 7}))
        self.assertEqual(self.store.max_request_sequence("w1"), 0)

    def test_control_reference_preserves_uppercase_native_versions_in_both_modes(self):
        visual_token = self.store.provision("w1", "viewer", 2, mode="visual")
        vehicle = {"id": 7, "generation": 3, "engine_instance_id": "boot"}
        page = {"id": 3, "generation": 4, "engine_instance_id": "boot"}
        element = {"id": 4, "generation": 5, "engine_instance_id": "boot"}
        order_version = "ov1:" + "A1" * 32
        page_version = "spv1:" + "B2" * 32
        cases = (
            ({"kind": "vehicle", "id": vehicle["id"]},
             {"kind": "vehicle", "reference": vehicle, "order_version": order_version}),
            ({"kind": "story_page", "id": page["id"]},
             {"kind": "story_page", "reference": page, "page": page,
              "page_version": page_version}),
            ({"kind": "story_element", "id": element["id"]},
             {"kind": "story_element", "reference": element, "page": page,
              "page_version": page_version}),
        )
        for actor_mode, token in (("text", self.token), ("visual", visual_token)):
            for request, expected in cases:
                with self.subTest(actor_mode=actor_mode, kind=request["kind"]):
                    self.engine.reference_reply = {"status": "completed", "result": {
                        **expected, "orders": ["private"], "capacity": 999}}
                    code, projected = self.service.handle(
                        "POST", "/v1/control/reference", token, request)
                    self.assertEqual((code, projected), (200, expected))
                    sent = [call for call in self.engine.calls
                            if call["type"] == "control.reference"][-1]
                    self.assertEqual(sent["actor"]["mode"], actor_mode)
                    self.assertEqual(sent["parameters"], request)
            for request, bad_version in ((cases[0][0], "ov1:" + "G" * 64),
                                         (cases[1][0], "spv2:" + "B" * 64)):
                valid = cases[0][1] if request["kind"] == "vehicle" else cases[1][1]
                field = "order_version" if request["kind"] == "vehicle" else "page_version"
                self.engine.reference_reply = {"status": "completed", "result": {
                    **valid, field: bad_version}}
                self.assert_code("control_reference_unavailable", lambda: self.service.handle(
                    "POST", "/v1/control/reference", token, request))
        self.assertEqual(self.store.max_request_sequence("w1"), 0)

    def test_unavailable_world_does_not_block_another(self):
        other_store = Store(os.path.join(self.tmp.name, "other.db"), world_id="w2")
        token_other = other_store.provision("w2", "bob", 2)
        principal_other = other_store.authenticate(token_other)
        other_service = Service(other_store, self.engine, autostart=False, world_id="w2")
        try:
            self.service.handle("POST", "/v1/actions", self.token, self.action())
            other_service.handle("POST", "/v1/actions", token_other, self.action(request_id="other"))
            self.engine.unavailable.add("w1")
            self.assert_code("engine_unavailable", lambda: self.process())
            second = other_service._process_record(principal_other, other_store.get_request(principal_other, "other"))
            self.assertEqual(second["status"], "completed")
            self.assertEqual(self.store.get_request(self.principal, "r1")["status"], "accepted")
        finally:
            other_service.close()
            other_store.close()

    def test_capability_matrix_is_honest_in_four_run_modes(self):
        pairs = [("agent", "text"), ("human", "text"),
                 ("agent", "visual"), ("human", "visual")]
        for index, (kind, mode) in enumerate(pairs):
            token = self.store.provision("w1", f"actor{index}", index + 3, mode=mode, kind=kind)
            code, response = self.service.handle("GET", "/v1/capabilities", token)
            self.assertEqual(code, 200)
            self.assertEqual(response["mode"], mode)
            self.assertEqual(response["observation"], "screenshots" if mode == "visual" else "structured")
            actions = {item["action"]: item["available"] for item in response["actions"]}
            expected = {spec["name"] for spec in CATALOG["actions"]
                        if mode in spec.get("modes", ()) and spec["name"] != "ui.apply"}
            expected.update(CATALOG["human_only"])
            self.assertEqual(set(actions), expected)
            self.assertNotIn("ui.apply", actions)
            if mode == "visual":
                self.assertEqual(response["tools"], [])
            else:
                self.assertTrue(actions["road.build"])
                self.assertTrue(actions["rail.build"])
                self.assertTrue(actions["airport.build"])

    def test_revoked_queued_request_remains_uncertain_and_uses_capacity(self):
        self.engine.submit_reply = {"status": "queued"}
        self.service.handle("POST", "/v1/actions", self.token, self.action())
        self.process()
        self.store.revoke_membership("w1", "alice", 1)
        immutable = self.store.get_principal(self.principal.run_id)
        queued = self.store.pending_requests()[0]
        resolved = self.service._process_record(immutable, queued)
        self.assertEqual(resolved["status"], "uncertain")
        self.assertEqual(resolved["error"], {"code": "authorization_revoked"})
        new_token = self.store.provision("w1", "new", 1)
        self.assert_code("company_capacity", lambda: self.service.handle(
            "POST", "/v1/actions", new_token, self.action(request_id="new")
        ))

    def test_revoked_accepted_request_is_rejected_and_releases_capacity(self):
        self.service.handle("POST", "/v1/actions", self.token, self.action())
        accepted = self.store.pending_requests()[0]
        self.store.revoke_membership("w1", "alice", 1)
        immutable = self.store.get_principal(self.principal.run_id)
        resolved = self.service._process_record(immutable, accepted)
        self.assertEqual(resolved["status"], "rejected")
        self.assertEqual(resolved["error"], {"code": "authorization_revoked"})
        new_token = self.store.provision("w1", "new", 1)
        status, _ = self.service.handle("POST", "/v1/actions", new_token, self.action(request_id="new"))
        self.assertEqual(status, 202)

    def test_visual_capture_is_durable_before_submit_and_retry_never_recaptures(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "queued"}
        body = self.visual()
        code, admitted = self.service.handle("POST", "/v1/actions", token, body)
        self.assertEqual((code, admitted), (202, {"request_id": "gesture", "status": "accepted"}))
        self.assertEqual(len(renderer.validations), 1)
        self.assertEqual(renderer.captures, [])
        self.assertEqual([call["type"] for call in self.engine.calls], ["hello"])

        def check_durable(envelope):
            stored = self.store.get_request(principal, "gesture")
            self.assertEqual(stored["intent_state"], "persisted")
            self.assertEqual(stored["status"], "queued")
            self.assertEqual(envelope["visual_intent"], stored["visual_intent"])
            self.assertEqual(envelope["request_sequence"], stored["request_sequence"])

        self.engine.on_submit = check_durable
        def check_marker(principal, request_id, expected_worker):
            stored = self.store.get_request(principal, request_id)
            self.assertEqual(stored["intent_state"], "capturing")
            self.assertEqual(stored["expected_worker_instance_id"], expected_worker)
            self.assertIsNone(stored["visual_intent"])

        renderer.on_capture = check_marker
        self.assertIsNone(self.service._process_record(principal, self.store.get_request(principal, "gesture")))
        self.assertEqual(len(renderer.captures), 1)
        self.assertEqual(self.engine.count("action.submit"), 1)
        renderer.consumed = True
        again_code, again = self.service.handle("POST", "/v1/actions", token, body)
        self.assertEqual((again_code, again["status"]), (202, "queued"))
        self.assertEqual(len(renderer.validations), 1)
        self.assertEqual(len(renderer.captures), 1)
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", token, self.visual(x=4)))
        self.assertEqual(len(renderer.validations), 1)

    def test_visual_text_retry_keeps_exact_unicode_text_without_recapture(self):
        token, principal, renderer = self.visual_actor()
        original_text = "  Héllo 👋  "
        parameters = {"image_id": "image-1", "gesture": "text", "x": 2, "y": 3,
                      "text": original_text}
        body = self.action(request_id="typed", name="ui.apply", parameters=parameters)
        self.assertEqual(self.service.handle("POST", "/v1/actions", token, body)[0], 202)
        completed = self.service._process_record(principal, self.store.get_request(principal, "typed"))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["intent_state"], "persisted")
        self.assertEqual(completed["payload"]["action"]["parameters"], parameters)
        self.assertEqual(renderer.captures[0][1], parameters)
        submit = next(call for call in self.engine.calls if call["type"] == "action.submit")
        self.assertEqual(submit["action"]["parameters"]["text"], original_text)
        self.assertEqual(submit["visual_intent"], completed["visual_intent"])
        renderer.consumed = True
        before_calls = len(self.engine.calls)
        before_events = len(self.service.events(token)["events"])
        code, receipt = self.service.handle("POST", "/v1/actions", token, body)
        self.assertEqual((code, receipt), (200, {"request_id": "typed", "status": "completed"}))
        self.assertEqual((len(renderer.validations), len(renderer.prepared), len(renderer.captures)), (1, 1, 1))
        self.assertEqual(len(self.engine.calls), before_calls)
        self.assertEqual(len(self.service.events(token)["events"]), before_events)
        changed = self.action(request_id="typed", name="ui.apply",
                              parameters={**parameters, "text": "  Héllo 👋 "})
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", token, changed))
        self.assertEqual((len(renderer.validations), len(renderer.prepared), len(renderer.captures)), (1, 1, 1))
        self.assertEqual(len(self.engine.calls), before_calls)
        self.assertEqual(self.store.max_request_sequence("w1"), 1)

    def test_visual_control_retry_preserves_choice_without_recapture(self):
        token, principal, renderer = self.visual_actor()
        params = {"image_id": "image-1", "gesture": "control",
                  "control": "road_stop.orientation",
                  "orientation": "drive_through_ne_sw", "x": 2, "y": 3}
        body = self.action(request_id="orientation", name="ui.apply",
                           parameters=params)
        code, capabilities = self.service.handle("GET", "/v1/capabilities", token)
        self.assertEqual(code, 200)
        self.assertIn("control", capabilities["tools"][0]["gestures"])
        self.assertEqual(self.service.handle("POST", "/v1/actions", token, body)[0], 202)
        completed = self.service._process_record(
            principal, self.store.get_request(principal, "orientation"))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(renderer.captures[0][1], params)
        submit = next(call for call in self.engine.calls
                      if call["type"] == "action.submit")
        self.assertEqual(submit["action"]["parameters"], params)
        self.assertEqual(submit["visual_intent"]["commands"], [])
        self.assertIsNone(submit["visual_intent"]["tile_proof"])
        renderer.consumed = True
        before_calls = len(self.engine.calls)
        code, receipt = self.service.handle("POST", "/v1/actions", token, body)
        self.assertEqual((code, receipt),
                         (200, {"request_id": "orientation", "status": "completed"}))
        self.assertEqual(len(renderer.captures), 1)
        self.assertEqual(len(self.engine.calls), before_calls)
        changed = self.action(request_id="orientation", name="ui.apply",
                              parameters={**params, "orientation": "terminal_ne"})
        self.assert_code("idempotency_conflict", lambda: self.service.handle(
            "POST", "/v1/actions", token, changed))
        self.assertEqual(len(renderer.captures), 1)
        self.service.close()
        self.service = Service(self.store, self.engine, renderer=None,
                               autostart=False, world_id="w1")
        before_calls = len(self.engine.calls)
        self.assertEqual(self.service.handle("POST", "/v1/actions", token, body),
                         (200, {"request_id": "orientation", "status": "completed"}))
        self.assertEqual(len(self.engine.calls), before_calls)

    def test_visual_queued_status_reuses_original_bundle_and_public_status_only(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "queued"}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        original = self.store.get_request(principal, "gesture")["visual_intent"]
        self.engine.status_reply = {"status": "completed", "result": {"success": True, "cost": 500}, "tick": 20}
        completed = self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.engine.calls[-1]["type"], "action.status")
        self.assertEqual(self.engine.calls[-1]["visual_intent"], original)
        self.assertEqual(len(renderer.captures), 1)
        self.assertEqual(self.service.handle("GET", "/v1/actions/gesture", token)[1],
                         {"request_id": "gesture", "status": "completed"})
        self.assertTrue(all(set(event["data"]) <= {"request_id", "status"}
                            for event in self.service.events(token)["events"]
                            if event["type"] == "action.status"))

    def test_visual_capture_failure_aborts_without_second_click(self):
        token, principal, renderer = self.visual_actor()
        renderer.fail_capture = True
        self.service.handle("POST", "/v1/actions", token, self.visual())
        result = self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.assertEqual((result["status"], result["intent_state"]), ("rejected", "aborted"))
        self.assertEqual(result["error"], {"code": "visual_capture_aborted"})
        self.assertEqual(result["expected_worker_instance_id"], "worker-1")
        self.assertEqual(self.store.checkpoint_ack_prefix("w1"), result["request_sequence"])
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(len(renderer.captures), 1)
        self.assertEqual(self.store.pending_requests(), [])
        self.assertEqual(len(renderer.captures), 1)
        self.assertEqual(self.service.handle("GET", "/v1/actions/gesture", token)[1],
                         {"request_id": "gesture", "status": "rejected"})

    def test_visual_resumed_capturing_marker_aborts_without_renderer_call(self):
        token, principal, renderer = self.visual_actor()
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.store.begin_intent_capture(principal, "gesture", "worker-1")
        self.service.close()
        self.service = Service(self.store, self.engine, renderer=renderer, autostart=False, world_id="w1")
        result = self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.assertEqual((result["status"], result["intent_state"]), ("rejected", "aborted"))
        self.assertEqual(renderer.prepared, [])
        self.assertEqual(renderer.captures, [])
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(self.store.checkpoint_ack_prefix("w1"), result["request_sequence"])

    def test_visual_accepted_persisted_bundle_survives_recovery_without_recapture(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "queued"}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.store.begin_intent_capture(principal, "gesture", "worker-1")
        bundle = {"request_id": "gesture", "image_id": "image-1", "world_epoch": "initial",
                  "worker_instance_id": "worker-1", "authority_instance_id": "boot", "commands": []}
        self.store.persist_visual_intent(principal, "gesture", bundle)
        hello = {"engine_instance_id": "boot", "world_epoch": "initial",
                 "recovery_required": True, "native_max_lag_ticks": 100,
                 "capabilities": {"actions": ["road.build", "ui.apply"]}}
        self.service._recover(principal, hello)
        recovered = self.store.get_request(principal, "gesture")
        self.assertEqual((recovered["status"], recovered["intent_state"]), ("accepted", "persisted"))
        self.assertEqual(recovered["visual_intent"], bundle)
        self.service._process_record(principal, recovered)
        self.assertEqual(renderer.prepared, [])
        self.assertEqual(renderer.captures, [])
        submitted = [call for call in self.engine.calls if call["type"] == "action.submit"]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["visual_intent"], bundle)

    def test_visual_terminal_feedback_releases_then_acks_without_reclick(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "completed", "result": {"success": True}}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.assertEqual(self.store.get_request(principal, "gesture")["status"], "completed")
        self.service._sync_visual_feedback()
        self.assertEqual(renderer.releases[-1], ("gesture", "commit", False))
        self.assertEqual(self.store.get_request(principal, "gesture")["ui_state"], "released")
        renderer.child_phase = "completed"
        self.service._sync_visual_feedback()  # rotate past the end of the scan
        self.service._sync_visual_feedback()
        self.assertEqual(renderer.releases[-1], ("gesture", "ack", False))
        self.assertEqual(self.store.get_request(principal, "gesture")["ui_state"], "acked")
        self.assertEqual(len(renderer.captures), 1)
        self.assertEqual(self.service.handle("GET", "/v1/actions/gesture", token)[1],
                         {"request_id": "gesture", "status": "completed"})

    def test_visual_ack_delete_crash_retries_only_ack_after_release(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "completed", "result": {"success": True}}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.service._sync_visual_feedback()
        self.assertEqual(self.store.get_request(principal, "gesture")["ui_state"], "released")
        before = len(renderer.releases)
        renderer.child_phase = "unknown"
        self.service._sync_visual_feedback()  # rotate past the end of the scan
        self.service._sync_visual_feedback()
        self.assertEqual(renderer.releases[before:], [("gesture", "ack", False)])
        self.assertEqual(self.store.get_request(principal, "gesture")["ui_state"], "acked")
        self.service._sync_visual_feedback()
        self.assertEqual(len(renderer.releases), before + 1)

    def test_visual_replacement_worker_cannot_release_old_intent(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "completed", "result": {"success": True}}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        renderer.worker_instance_id = "worker-2"
        self.service._sync_visual_feedback()
        self.assertEqual(self.store.get_request(principal, "gesture")["ui_state"], "pending")
        self.assertEqual(renderer.releases, [])
        renderer.worker_instance_id = "worker-1"
        # The cleanup scan rotates its cursor after a blocked row.
        self.service._sync_visual_feedback()
        self.service._sync_visual_feedback()
        self.assertEqual(renderer.releases[-1], ("gesture", "commit", False))
        self.assertEqual(self.store.get_request(principal, "gesture")["ui_state"], "released")

    def test_visual_rejection_before_authority_queue_releases_reject(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "rejected", "result": {},
                                    "error": {"code": "visual_context_stale"}}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.service._sync_visual_feedback()
        self.assertEqual(renderer.releases[-1], ("gesture", "reject", True))
        self.assertEqual(self.service.handle("GET", "/v1/actions/gesture", token)[1],
                         {"request_id": "gesture", "status": "rejected"})

    def test_visual_native_command_failure_releases_commit(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "rejected", "result": {"success": False},
                                    "error": {"code": "not_enough_money"}}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.service._sync_visual_feedback()
        self.assertEqual(renderer.releases[-1], ("gesture", "commit", False))
        self.assertEqual(self.service.handle("GET", "/v1/actions/gesture", token)[1],
                         {"request_id": "gesture", "status": "rejected"})

    def test_visual_recovery_rolled_back_always_releases_reject(self):
        token, principal, renderer = self.visual_actor()
        self.engine.submit_reply = {"status": "queued"}
        self.service.handle("POST", "/v1/actions", token, self.visual())
        self.service._process_record(principal, self.store.get_request(principal, "gesture"))
        self.store.begin_recovery("w1", "boot")
        self.store.resolve_recovery_request(principal, "gesture", "resolved_rolled_back",
                                            result={"success": False, "authority_queued": True})
        self.store.finish_recovery("w1", scan_completed=True)
        self.service._sync_visual_feedback()
        self.assertEqual(renderer.releases[-1], ("gesture", "reject", True))
        self.assertEqual(self.service.handle("GET", "/v1/actions/gesture", token)[1],
                         {"request_id": "gesture", "status": "resolved_rolled_back"})


if __name__ == "__main__":
    unittest.main()
