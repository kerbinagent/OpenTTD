"""Shared actuator feedback and reference metadata must not become observations."""

import copy
import math
import unittest

from backend import projection
from backend.errors import ServiceError


class ActionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.handle = {"id": 3, "generation": 7, "engine_instance_id": "boot"}
        self.order_version = "ov1:" + "a" * 64
        self.page_version = "spv1:" + "b" * 64
        self.upper_order_version = "ov1:" + "A1" * 32
        self.upper_page_version = "spv1:" + "B2" * 32

    def test_visual_command_feedback_preserves_cost_and_created_reference_without_statistics(self):
        raw = {"success": True, "cost": 42, "vehicle_id": self.handle,
               "capacity": 100, "mail_capacity": 10,
               "cargo_capacities": [{"cargo_id": 0, "capacity": 100}],
               "location": {"x": 10, "y": 20}, "private": "secret",
               "order_version": self.order_version}
        spec = {"result_fields": list(raw)}
        self.assertEqual(projection.outcome(raw, spec, visual=True), {
            "success": True, "cost": 42, "vehicle_id": self.handle,
            "order_version": self.order_version})
        self.assertEqual(projection.outcome(raw, spec)["capacity"], 100)

    def test_visual_native_error_is_readable_utf8_bounded_and_has_no_raw_string_id(self):
        raw = {"success": False, "cost": -2, "error": {
            "code": "gameplay_rejected", "message": "é" * 2000 + "\x00\ue000",
            "string_id": 123, "private": "secret"}}
        value = projection.outcome(raw, {"result_fields": ["success", "cost", "error"]}, visual=True)
        self.assertEqual(value["cost"], -2)
        self.assertEqual(set(value["error"]), {"code", "message"})
        self.assertLessEqual(len(value["error"]["message"].encode("utf-8")), 2048)
        self.assertNotIn("\x00", value["error"]["message"])
        self.assertNotIn("\ue000", value["error"]["message"])
        self.assertEqual(projection.error({"code": "ok", "message": "Tracks obstructed"}, visual=True),
                         {"code": "ok", "message": "Tracks obstructed"})
        self.assertNotIn("page_version", projection.outcome(
            {"page_version": "hidden state"}, {"result_fields": ["page_version"]}, visual=True))

    def test_visual_outcome_preserves_native_version_case_for_all_opaque_tokens(self):
        raw = {"success": True, "cost": 12,
               "order_version": self.upper_order_version,
               "source_order_version": "ov1:" + "aB" * 32,
               "page_version": self.upper_page_version,
               "capacity": 900, "private": "hidden"}
        fields = {"result_fields": list(raw)}
        expected = {key: raw[key] for key in ("success", "cost", "order_version",
                                               "source_order_version", "page_version")}
        self.assertEqual(projection.outcome(raw, fields, visual=True), expected)
        self.assertEqual(projection.outcome(raw, fields)["order_version"], self.upper_order_version)
        for key, bad in (("order_version", "ov1:" + "G" * 64),
                         ("source_order_version", "ov2:" + "A" * 64),
                         ("page_version", "spv1:" + "B" * 63)):
            with self.subTest(key=key):
                contaminated = projection.outcome({**raw, key: bad}, fields, visual=True)
                self.assertNotIn(key, contaminated)

    def test_control_reference_returns_only_kind_specific_identity_and_tokens(self):
        raw = {"kind": "vehicle", "reference": {**self.handle, "profit": 999},
               "order_version": self.order_version, "page": self.handle,
               "page_version": self.page_version, "engine_id": 9, "orders": [1, 2]}
        self.assertEqual(projection.control_reference(raw, "vehicle", "boot"), {
            "kind": "vehicle", "reference": self.handle, "order_version": self.order_version})
        self.assertEqual(projection.control_reference(
            {**raw, "order_version": self.upper_order_version}, "vehicle", "boot"),
            {"kind": "vehicle", "reference": self.handle,
             "order_version": self.upper_order_version})
        self.assertEqual(projection.control_reference(
            {"kind": "engine", "engine_id": 9, "price": 999}, "engine", "boot"),
            {"kind": "engine", "engine_id": 9})
        page = {"kind": "story_page", "reference": self.handle, "page": self.handle,
                "page_version": self.page_version, "text": "not observation data"}
        self.assertEqual(projection.control_reference(page, "story_page", "boot"),
                         {key: value for key, value in page.items() if key != "text"})
        for kind, reference in (("story_page", self.handle),
                                ("story_element", {**self.handle, "id": 4})):
            with self.subTest(kind=kind):
                self.assertEqual(projection.control_reference({
                    "kind": kind, "reference": reference, "page": self.handle,
                    "page_version": self.upper_page_version,
                    "private": "hidden"}, kind, "boot"),
                    {"kind": kind, "reference": reference, "page": self.handle,
                     "page_version": self.upper_page_version})
        for kind in ("station", "waypoint", "depot", "group", "town", "sign", "company", "question"):
            with self.subTest(kind=kind):
                self.assertEqual(projection.control_reference(
                    {"kind": kind, "reference": self.handle, "order_version": self.order_version}, kind, "boot"),
                    {"kind": kind, "reference": self.handle})

    def test_control_reference_fails_closed_on_stale_or_malformed_identity_and_tokens(self):
        valid = {"kind": "vehicle", "reference": self.handle, "order_version": self.order_version}
        invalid = []
        for field, value in (("id", True), ("generation", False), ("generation", 0),
                             ("engine_instance_id", "old-boot")):
            raw = copy.deepcopy(valid)
            raw["reference"][field] = value
            invalid.append(raw)
        for token in ("ov1:" + "G" * 64, "ov2:" + "A" * 64,
                      "ov1:" + "A" * 63, "ov1:" + "A" * 65,
                      self.order_version + "\n", "", True):
            invalid.append(dict(valid, order_version=token))
        invalid.extend([{}, dict(valid, kind="station"), dict(valid, reference={}),
                        dict(valid, ignored="x" * 8192), dict(valid, ignored="\ud800")])
        for raw in invalid:
            with self.subTest(raw=repr(raw)[:120]), self.assertRaises(ServiceError) as caught:
                projection.control_reference(raw, "vehicle", "boot")
            self.assertEqual(caught.exception.code, "control_reference_unavailable")
            self.assertEqual(caught.exception.status, 409)
        for raw in ({"kind": "story_element", "reference": self.handle},
                    {"kind": "story_page", "reference": self.handle,
                     "page": dict(self.handle, id=4), "page_version": self.page_version},
                    {"kind": "story_page", "reference": self.handle,
                     "page": self.handle, "page_version": "spv1:" + "Z" * 64},
                    {"kind": "story_element", "reference": self.handle,
                     "page": self.handle, "page_version": "spv2:" + "B" * 64},
                    {"kind": "story_element", "reference": self.handle,
                     "page": self.handle, "page_version": "spv1:" + "B" * 63}):
            with self.assertRaises(ServiceError):
                projection.control_reference(raw, raw["kind"], "boot")


    def test_estimate_projects_only_finite_scalar_feedback(self):
        success = {"success": True, "cost": -(1 << 63), "advisory": True,
                   "test_and_execute_may_differ": False}
        self.assertEqual(projection.estimate({**success, "vehicle_id": self.handle,
            "capacity": 500, "tick": 12, "map": {"private": True}}), success)
        rejected = {"success": False, "cost": (1 << 63) - 1,
                    "advisory": True, "test_and_execute_may_differ": True,
                    "error": {"code": "gameplay_rejected", "message": "é\x00blocked",
                              "string_id": 123, "private": "secret"}}
        self.assertEqual(projection.estimate(rejected), {
            "success": False, "cost": (1 << 63) - 1, "advisory": True,
            "test_and_execute_may_differ": True,
            "error": {"code": "gameplay_rejected", "message": "éblocked"}})
        boundary = {**rejected, "error": {"code": "gameplay_rejected",
                                          "message": "é" * 512}}
        self.assertEqual(projection.estimate(boundary)["error"]["message"], "é" * 512)

    def test_estimate_rejects_malformed_approved_fields_and_wire_limits(self):
        valid = {"success": True, "cost": -1, "advisory": True,
                 "test_and_execute_may_differ": False}
        invalid = [None, [], {},
                   {**valid, "success": 1}, {**valid, "cost": True},
                   {**valid, "cost": 1 << 63}, {**valid, "cost": -(1 << 63) - 1},
                   {**valid, "cost": math.inf}, {**valid, "advisory": 1},
                   {**valid, "advisory": False},
                   {**valid, "test_and_execute_may_differ": 0},
                   {**valid, "error": {"code": "gameplay_rejected", "message": "wrong phase"}},
                   {**valid, "success": False},
                   {**valid, "private": "x" * 4096},
                   {**valid, "private": "\ud800"}]
        failure = {**valid, "success": False,
                   "error": {"code": "gameplay_rejected", "message": "Blocked"}}
        invalid.extend([{**failure, "error": {"code": "internal", "message": "private"}},
                        {**failure, "error": {"code": "gameplay_rejected"}},
                        {**failure, "error": {"code": "gameplay_rejected", "message": True}},
                        {**failure, "error": {"code": "gameplay_rejected", "message": "é" * 513}},
                        {**failure, "error": {"code": "gameplay_rejected", "message": "\ud800"}}])
        for raw in invalid:
            with self.subTest(raw=repr(raw)[:100]), self.assertRaises(ServiceError) as caught:
                projection.estimate(raw)
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("engine_unavailable", 503))


if __name__ == "__main__":
    unittest.main()
