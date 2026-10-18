"""Depot facing is mechanical command feedback, shared by both player modes."""

import json
from pathlib import Path
import tempfile
import unittest

from backend import projection
from backend.catalog import action_spec, validate_parameters
from backend.errors import ServiceError
from backend.service import Service
from backend.store import Store
from backend.tests.test_service import FakeEngine


class DepotDirectionTests(unittest.TestCase):
    DEPOTS = ("road.depot.build", "rail.depot.build")

    def test_explicit_directions_and_auto_share_existing_depot_actions(self):
        for name in self.DEPOTS:
            spec = action_spec(name)
            self.assertEqual(spec["modes"], ["text", "visual"])
            for direction in (0, 1, 2, 3, "auto"):
                parameters = {"tile": {"x": 10, "y": 20}, "direction": direction}
                with self.subTest(name=name, direction=direction):
                    self.assertEqual(validate_parameters(spec, parameters), parameters)

    def test_direction_remains_required_and_closed(self):
        for name in self.DEPOTS:
            spec = action_spec(name)
            invalid = [{"tile": 10}]
            invalid.extend({"tile": 10, "direction": direction} for direction in (
                True, False, None, 1.0, -1, 4, "0", "AUTO", "automatic", {}, []))
            for parameters in invalid:
                with self.subTest(name=name, parameters=parameters):
                    with self.assertRaises(ServiceError):
                        validate_parameters(spec, parameters)

    def test_other_placement_directions_do_not_gain_auto(self):
        spec = action_spec("road.stop.build")
        parameters = {"tile": 10, "direction": 0, "stop_type": "bus"}
        self.assertEqual(validate_parameters(spec, parameters), parameters)
        with self.assertRaises(ServiceError):
            validate_parameters(spec, {**parameters, "direction": "auto"})

    def test_actual_depot_facing_survives_both_mode_projections(self):
        handle = {"id": 3, "generation": 7, "engine_instance_id": "boot"}
        for name in self.DEPOTS:
            spec = action_spec(name)
            self.assertIn("direction", spec["result_fields"])
            for direction in range(4):
                raw = {"success": True, "cost": 500, "depot_id": handle,
                       "direction": direction, "candidate_sites": [99],
                       "nearby_demand": 1000}
                expected = {key: value for key, value in raw.items()
                            if key not in ("candidate_sites", "nearby_demand")}
                for visual in (False, True):
                    with self.subTest(name=name, direction=direction, visual=visual):
                        self.assertEqual(projection.outcome(raw, spec, visual=visual), expected)

    def test_invalid_or_noncompleted_direction_is_not_command_feedback(self):
        for name in self.DEPOTS:
            for visual in (False, True):
                for direction in (True, False, None, 1.0, -1, 4, "auto"):
                    with self.subTest(name=name, visual=visual, direction=direction):
                        self.assertNotIn("direction", projection.outcome(
                            {"success": True, "cost": 0, "direction": direction},
                            action_spec(name), visual=visual))
                for success in (False, None):
                    raw = {"cost": 0, "direction": 2}
                    if success is not None:
                        raw["success"] = success
                    self.assertNotIn("direction", projection.outcome(
                        raw, action_spec(name), visual=visual))

    def test_direction_does_not_expand_unrelated_outcomes(self):
        for name in ("water.depot.build", "road.build", "vehicle.build"):
            for visual in (False, True):
                self.assertNotIn("direction", projection.outcome(
                    {"success": True, "cost": 1, "direction": 2},
                    action_spec(name), visual=visual))

    def test_auto_selection_errors_preserve_safe_machine_feedback(self):
        for code in ("depot_direction_unavailable", "depot_direction_ambiguous"):
            raw = {"success": False, "cost": 0,
                   "error": {"code": code, "message": "Choose an explicit depot direction.",
                             "private": "not public"}}
            expected = {**raw, "error": {key: value for key, value in raw["error"].items()
                                        if key != "private"}}
            for name in self.DEPOTS:
                for visual in (False, True):
                    with self.subTest(code=code, name=name, visual=visual):
                        self.assertEqual(projection.outcome(
                            raw, action_spec(name), visual=visual), expected)

    def test_failed_auto_estimates_preserve_only_safe_feedback(self):
        for code in ("depot_direction_unavailable", "depot_direction_ambiguous"):
            expected = {"success": False, "cost": 0, "advisory": True,
                        "test_and_execute_may_differ": False,
                        "error": {"code": code, "message": "Choose an explicit direction."}}
            with self.subTest(code=code):
                self.assertEqual(projection.estimate({**expected,
                    "candidate_sites": [99], "direction": 2,
                    "error": {**expected["error"], "private": "hidden"}}), expected)
                for changed in ({"cost": 1}, {"test_and_execute_may_differ": True},
                                {"error": {"code": "unknown", "message": "No."}},
                                {"error": {"code": [], "message": "No."}}):
                    with self.subTest(changed=changed), self.assertRaises(ServiceError) as error:
                        projection.estimate({**expected, **changed})
                    self.assertEqual(error.exception.code, "engine_unavailable")

    def test_public_response_schema_accepts_depot_feedback_contract(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas" /
                             "public-api-v1.json").read_text())
        definitions = schema["$defs"]
        self.assertEqual(definitions["actionResult"]["properties"]["direction"],
                         {"type": "integer", "minimum": 0, "maximum": 3})
        self.assertFalse(definitions["actionResult"]["additionalProperties"])
        self.assertNotIn("direction", definitions["actionResult"]["required"])
        self.assertEqual(definitions["estimateResult"]["properties"]["error"]
                         ["properties"]["code"], {"enum": ["gameplay_rejected",
                         "depot_direction_unavailable", "depot_direction_ambiguous"]})


class DepotEstimateServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "state.db", max_pending_company=1)
        self.engine = FakeEngine()
        self.service = Service(self.store, self.engine, autostart=False)

    def tearDown(self):
        self.service.close()
        self.store.close()
        self.directory.cleanup()

    def test_auto_selection_refusals_are_estimates_in_both_modes_without_submission(self):
        for mode in ("text", "visual"):
            token = self.store.provision("w1", mode, 1 if mode == "text" else 2, mode=mode)
            self.assertEqual(self.service.handle("GET", "/v1/capabilities", token)[0], 200)
            for name in DepotDirectionTests.DEPOTS:
                for code in ("depot_direction_unavailable", "depot_direction_ambiguous"):
                    result = {"success": False, "cost": 0, "advisory": True,
                              "test_and_execute_may_differ": False,
                              "error": {"code": code, "message": "Choose an explicit direction."}}
                    self.engine.estimate_reply = {"status": "completed",
                        "engine_instance_id": "boot", "result": result}
                    request = {"action": {"name": name,
                               "parameters": {"tile": 10, "direction": "auto"}}}
                    with self.subTest(mode=mode, name=name, code=code):
                        self.assertEqual(self.service.handle("POST", "/v1/actions/estimate",
                                         token, request), (200, {"estimate": result}))
                        sent = self.engine.calls[-1]
                        self.assertEqual(sent["type"], "action.estimate")
                        self.assertEqual(sent["action"], request["action"])
                        self.assertEqual(sent["actor"]["mode"], mode)
                        self.assertEqual(self.service.events(token)["events"], [])
        self.assertEqual(self.engine.count("action.submit"), 0)
        self.assertEqual(self.store.max_request_sequence("w1"), 0)


if __name__ == "__main__":
    unittest.main()
