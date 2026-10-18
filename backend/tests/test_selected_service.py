"""Selected observations preserve action ownership and public result boundaries."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from backend.errors import ServiceError
from backend.observations import ObservationImage, capability, validate_request
from backend.service import Service
from backend.store import Store
from backend.tests.test_service import FakeEngine, FakeRenderer


GENERATION = "0123456789abcdef0123456789abcdef"
OBSERVATION_REQUEST_ID = "obs1:1712345678901:abcdef0123456789abcdef0123456789"


class SelectedRenderer(FakeRenderer):
    """Exercise the service's barrier callback without native rendering IPC."""

    def __init__(self):
        super().__init__()
        self.configured = []
        self.observations = []
        self.barriers = []
        self.enabled = True

    def available(self, principal):
        return self.enabled

    def configure_authority(self, instance, max_lag):
        self.configured.append((instance, max_lag))

    def observation_capabilities(self, principal):
        return capability(GENERATION)

    def observe(self, principal, kind, body, resolve_barrier):
        request = validate_request(kind, body)
        self.observations.append((principal.run_id, kind, copy.deepcopy(body)))
        barrier = resolve_barrier(request.get("after_request_id"))
        self.barriers.append(barrier)
        if barrier is None:
            return {"status": "pending", "reason": "action_pending",
                    "observation_request_id": request["observation_request_id"]}
        return ObservationImage(
            data=b"selected image", observation_id="1" * 32, image_id="2" * 32,
            generation=GENERATION, kind=kind, width=64, height=64,
            causal_state="satisfied" if "after_request_id" in request else "none",
            reused=False, age_ms=0,
        )


class SelectedObservationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "state.db"))
        self.engine = FakeEngine()
        self.renderer = SelectedRenderer()
        self.service = Service(self.store, self.engine, renderer=self.renderer, autostart=False)
        self.tokens = {
            mode: self.store.provision("w1", mode + "-actor", company, mode=mode)
            for company, mode in enumerate(("text", "visual"), 1)
        }
        self.actors = {mode: self.store.authenticate(token) for mode, token in self.tokens.items()}

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def action(request_id="road"):
        return {"request_id": request_id,
                "action": {"name": "road.build", "parameters": {"start": 1, "end": 2}}}

    @staticmethod
    def observation(kind="world", after_request_id=None):
        body = {"observation_generation": GENERATION,
                "observation_request_id": OBSERVATION_REQUEST_ID, "wait_ms": 0}
        if kind == "world":
            body.update(selection={"kind": "map_center", "tile": {"x": 12, "y": 34},
                                   "zoom_level": "normal"},
                        output={"width": 64, "height": 64})
        else:
            body["panel"] = "company_finances"
        if after_request_id is not None:
            body["after_request_id"] = after_request_id
        return body

    def observe(self, mode="text", kind="world", after_request_id=None, **kwargs):
        suffix = "world" if kind == "world" else "native-info"
        return self.service.handle("POST", "/v1/view/observe/" + suffix, self.tokens[mode],
                                   self.observation(kind, after_request_id), **kwargs)

    def assert_error(self, code, status, operation):
        with self.assertRaises(ServiceError) as caught:
            operation()
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))

    def record(self, request_id="road", status="completed", result=None, mode="text", tick=999):
        actor = self.actors[mode]
        body = self.action(request_id)
        record, _ = self.store.accept_request(actor, request_id,
                                              {"action": body["action"], "preconditions": {}})
        if status != "accepted":
            record = self.store.update_request(actor, request_id, status, result=result, tick=tick)
        return record

    def test_selected_routes_are_available_in_both_modes_and_do_not_submit_actions(self):
        for mode in self.tokens:
            for kind in ("world", "native_info"):
                with self.subTest(mode=mode, kind=kind):
                    code, image = self.observe(mode, kind)
                    self.assertEqual(code, 200)
                    self.assertIsInstance(image, ObservationImage)
                    self.assertEqual((image.kind, image.causal_state), (kind, "none"))
                    self.assertEqual(self.renderer.observations[-1],
                                     (self.actors[mode].run_id, kind, self.observation(kind)))
                    self.assertEqual(self.renderer.barriers[-1], ("boot", None))
        self.assertEqual(self.store.max_request_sequence("w1"), 0)
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(self.engine.count("observation.query"), 0)
        self.assertEqual(self.renderer.validations, [])
        self.assertEqual(self.renderer.captures, [])

    def test_capabilities_add_selected_observations_and_keep_mode_action_contract(self):
        for mode, token in self.tokens.items():
            with self.subTest(mode=mode):
                code, public = self.service.handle("GET", "/v1/capabilities", token)
                self.assertEqual(code, 200)
                self.assertEqual(public["selected_observations"], capability(GENERATION))
                self.assertEqual(public["observation"],
                                 "screenshots" if mode == "visual" else "structured")
                actions = {row["action"]: row["available"] for row in public["actions"]}
                self.assertTrue(actions["road.build"])
                self.assertEqual(bool(public["tools"]), mode == "visual")

    def test_real_capability_response_matches_its_closed_public_schema(self):
        try:
            from jsonschema import Draft202012Validator, ValidationError
        except ImportError:
            self.skipTest("JSON Schema validation dependency is unavailable")
        schema = json.loads((Path(__file__).parents[1] / "schemas" / "public-api-v1.json").read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator({
            "$ref": "#/$defs/capabilitiesResponse", "$defs": schema["$defs"],
        })
        for mode, token in self.tokens.items():
            with self.subTest(mode=mode):
                _, public = self.service.handle("GET", "/v1/capabilities", token)
                validator.validate(public)
                public["selected_observations"]["native_frame"] = 123
                with self.assertRaises(ValidationError):
                    validator.validate(public)

    def test_selected_observations_preserve_typed_action_dispatch_and_retries(self):
        for mode, token in self.tokens.items():
            with self.subTest(mode=mode):
                body = self.action(mode + "-road")
                self.assertEqual(self.service.handle("POST", "/v1/actions", token, body)[0], 202)
                self.observe(mode)
                actor = self.actors[mode]
                self.service._process_record(actor, self.store.get_request(actor, body["request_id"]))
                code, receipt = self.service.handle("GET", "/v1/actions/" + body["request_id"], token)
                self.assertEqual((code, receipt["status"], receipt["result"]),
                                 (200, "completed", {"success": True}))
                self.assertEqual(self.service.handle("POST", "/v1/actions", token, body), (200, receipt))
                dispatched = [call for call in self.engine.calls if call["type"] == "action.submit"
                              and call["actor"]["run_id"] == actor.run_id]
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(dispatched[0]["action"], body["action"])
                self.assertNotIn("visual_intent", dispatched[0])
        self.assertEqual(self.renderer.validations, [])
        self.assertEqual(self.renderer.captures, [])

    def test_visual_selected_observations_do_not_open_structured_readers(self):
        self.observe("visual")
        for method, body, query in (("GET", None, {"query": "vehicles"}),
                                    ("POST", {"query": "vehicles"}, None)):
            with self.subTest(method=method):
                self.assert_error("forbidden", 403, lambda: self.service.handle(
                    method, "/v1/observations", self.tokens["visual"], body, query))
        self.assertEqual(self.engine.count("observation.query"), 0)

    def test_query_parameters_are_rejected_before_renderer_dispatch(self):
        for kind in ("world", "native_info"):
            with self.subTest(kind=kind):
                self.assert_error("invalid_request", 400,
                                  lambda: self.observe(kind=kind, query={"width": "64"}))
        self.assertEqual(self.renderer.observations, [])
        self.assertEqual(self.engine.calls, [])

    def test_unavailable_renderer_does_not_dispatch_selected_observations(self):
        self.renderer.enabled = False
        self.assert_error("renderer_unavailable", 503, self.observe)
        self.service.renderer = None
        self.assert_error("renderer_unavailable", 503, self.observe)
        self.assertEqual(self.renderer.observations, [])
        self.assertEqual(self.engine.calls, [])

    def test_completed_action_uses_its_private_applied_frame_for_both_modes(self):
        for mode in self.tokens:
            with self.subTest(mode=mode):
                record = self.record(mode=mode, result={
                    "success": True, "executed_frame": 37, "executed_boot_id": "boot"})
                code, image = self.observe(mode=mode, after_request_id="road")
                self.assertEqual((code, image.causal_state), (200, "satisfied"))
                self.assertEqual(self.renderer.barriers[-1], ("boot", 37))
                self.assertEqual(self.store.get_request(self.actors[mode], "road"), record)
        self.assertEqual(self.engine.count("action.status"), 0)
        self.assertEqual(self.engine.count("action.submit"), 0)

    def test_completed_receipt_without_private_frame_uses_fenced_status_lookup(self):
        record = self.record(result={"success": True})
        self.engine.status_reply = {"status": "completed", "engine_instance_id": "boot",
                                    "tick": 5000, "result": {"success": True,
                                    "executed_frame": 51, "executed_boot_id": "boot"}}
        self.assertEqual(self.observe(after_request_id="road")[0], 200)
        self.assertEqual(self.renderer.barriers[-1], ("boot", 51))
        status_calls = [call for call in self.engine.calls if call["type"] == "action.status"]
        self.assertEqual(len(status_calls), 1)
        self.assertEqual(status_calls[0]["request_id"], "road")
        self.assertEqual(status_calls[0]["request_sequence"], record["request_sequence"])
        self.assertEqual(status_calls[0]["engine_instance_id"], "boot")
        self.assertEqual(status_calls[0]["actor"]["run_id"], self.actors["text"].run_id)
        self.assertEqual(self.store.get_request(self.actors["text"], "road"), record)
        self.assertEqual(self.service.events(self.tokens["text"])["events"], [])

    def test_pending_actions_return_pending_without_treating_tick_as_a_frame(self):
        for status in ("accepted", "queued"):
            with self.subTest(status=status):
                self.record(status, status=status, result={"executed_frame": 37,
                                                         "executed_boot_id": "boot"})
                self.service._wake.clear()
                code, pending = self.observe(after_request_id=status)
                self.assertEqual((code, pending["status"], pending["reason"]),
                                 (202, "pending", "action_pending"))
                self.assertIsNone(self.renderer.barriers[-1])
                self.assertTrue(self.service._wake.is_set())
        self.assertEqual(self.engine.count("action.status"), 0)
        self.assertEqual(self.engine.count("action.submit"), 0)

    def test_uncertain_and_rejected_actions_cannot_satisfy_causality(self):
        for status in ("uncertain", "rejected"):
            with self.subTest(status=status):
                self.record(status, status=status, result={"executed_frame": 37,
                                                         "executed_boot_id": "boot"})
                self.assert_error("action_not_applied", 409,
                                  lambda: self.observe(after_request_id=status))
        self.assertEqual(self.renderer.barriers, [])
        self.assertEqual(self.engine.count("action.status"), 0)

    def test_barrier_does_not_read_another_identity_or_another_run(self):
        self.record(result={"executed_frame": 37, "executed_boot_id": "boot"})
        other_run = self.store.provision("w1", "text-actor", 1, mode="text")
        for label, token in (("other identity", self.tokens["visual"]), ("other run", other_run)):
            with self.subTest(owner=label):
                self.assert_error("not_found", 404, lambda: self.service.handle(
                    "POST", "/v1/view/observe/world", token, self.observation(after_request_id="road")))
        self.assertEqual(self.renderer.barriers, [])
        self.assertEqual(self.engine.count("action.status"), 0)

    def test_cross_boot_and_invalid_private_frames_do_not_satisfy_causality(self):
        for index, (boot, frame) in enumerate((
                ("older-boot", 37), ("boot", True), ("boot", -1),
                ("boot", 2**32), ("boot", 3.0), ("boot", "37"))):
            with self.subTest(boot=boot, frame=frame):
                request_id = "bad-frame-" + str(index)
                self.record(request_id, result={"executed_frame": frame, "executed_boot_id": boot})
                self.assert_error("causal_observation_unavailable", 409,
                                  lambda: self.observe(after_request_id=request_id))
        self.assertEqual(self.renderer.barriers, [])
        self.assertEqual(self.engine.count("action.status"), 0)

    def test_status_fallback_requires_completed_current_boot_applied_frame(self):
        self.record(result={"success": True})
        replies = [
            {"status": status, "result": {"executed_frame": 37, "executed_boot_id": "boot"}}
            for status in ("queued", "uncertain", "rejected", "unknown")
        ] + [
            {"status": "completed", "engine_instance_id": "older-boot",
             "result": {"executed_frame": 37, "executed_boot_id": "boot"}},
            {"status": "completed", "result": {"executed_frame": 37,
                                                 "executed_boot_id": "older-boot"}},
            {"status": "completed", "tick": 37, "result": {"success": True}},
            {"status": "completed", "result": {"executed_frame": True, "executed_boot_id": "boot"}},
            {"status": "completed", "result": [1]},
            {"status": "completed", "result": "invalid result"},
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                self.engine.status_reply = reply
                self.assert_error("causal_observation_unavailable", 409,
                                  lambda: self.observe(after_request_id="road"))
        self.assertEqual(self.renderer.barriers, [])
        self.assertEqual(self.store.get_request(self.actors["text"], "road")["result"], {"success": True})

    def test_restarted_or_recovering_authority_cannot_reuse_completed_action_frame(self):
        self.record(result={"executed_frame": 37, "executed_boot_id": "boot"})
        self.engine.instances["w1"] = "new-boot"
        self.assert_error("causal_observation_unavailable", 409,
                          lambda: self.observe(after_request_id="road"))
        self.engine.recovery_required = True
        self.assert_error("world_recovering", 503, self.observe)
        self.assertEqual(self.renderer.barriers, [])

    def test_private_frame_fields_never_reach_action_receipts_retries_or_events(self):
        private = {"success": True, "cost": 25, "executed_frame": 37,
                   "executed_boot_id": "boot", "source_frame": 36, "native_debug": "private"}
        self.engine.submit_reply = {"status": "completed", "tick": 999, "result": private}
        for mode, token in self.tokens.items():
            with self.subTest(mode=mode):
                actor = self.actors[mode]
                body = self.action(mode + "-road")
                self.service.handle("POST", "/v1/actions", token, body)
                self.service._process_record(actor, self.store.get_request(actor, body["request_id"]))
                self.observe(mode=mode, after_request_id=body["request_id"])
                _, receipt = self.service.handle("GET", "/v1/actions/" + body["request_id"], token)
                self.assertEqual(receipt["result"], {"success": True, "cost": 25})
                self.assertEqual(self.service.handle("POST", "/v1/actions", token, body), (200, receipt))
                self.store.append_event(actor, "action.status", {
                    "request_id": body["request_id"], "status": "completed", **private, "result": private})
                events = self.service.events(token)["events"]
                self.assertTrue(events)
                self.assertTrue(all(event["data"] == receipt for event in events))
                public = json.dumps({"receipt": receipt, "events": events})
                for key in ("executed_frame", "executed_boot_id", "source_frame", "native_debug"):
                    self.assertNotIn(key, public)
                self.assertEqual(self.store.get_request(actor, body["request_id"])["result"], private)


if __name__ == "__main__":
    unittest.main()
