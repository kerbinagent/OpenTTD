"""Opt-in land information through the real public HTTP boundary.

Uses the disposable stock fixture, two modes of company 0 and no renderer.
Run with the explicit binary/assets environment used by shared_capture_live.py;
default ports are 25978/18106. No existing world is reused or modified.

This checks DTOs, read admission and absence of gameplay submissions. Native
callback isolation is covered by test_integration_land_info.cpp, not this stock
HTTP smoke. A one-second timing precondition makes admission probes conclusive;
it is not a promised response-time bound.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import time
import unittest

from fixture_plans import MODES
import shared_capture_live as shared


class PublicLandInfo(shared.SharedCaptureActuators):
    def setUp(self):
        super().setUp()
        self.evidence["shared_fixture_source_sha256"] = self.evidence["test_source_sha256"]
        self.evidence["test_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        helper = Path(__file__).parent / "public_services_live.py"
        self.evidence["lifecycle_source_sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()

    def rejected_read(self, parameters, *, audit, status, code):
        body = {"query": "land.info", "parameters": parameters}
        with self.assertRaises(shared.BackendError) as caught:
            self.request("POST", "/v1/observations", body, audit=audit)
        error = caught.exception
        self.assertEqual((error.status, error.code), (status, code))
        self.evidence["expected_http_errors"].append({
            "audit": audit, "body": body, "status": error.status, "code": error.code})

    def assert_land_info(self, value):
        self.assertEqual(set(value), {
            "tick", "company_id", "tile", "tile_type", "height", "slope", "description",
            "owners", "local_authority", "build_date", "house", "object", "station_spec",
            "accepted_cargo", "clear_preview"})
        for key in ("tick", "company_id", "tile_type", "height", "slope"):
            self.assertIs(type(value[key]), int)
        self.assertEqual(value["company_id"], 0)
        self.assertEqual(value["tile"], {"tile": 10008, "x": 24, "y": 39})
        self.assertIsInstance(value["description"], str)
        self.assertIsInstance(value["owners"], list)
        for owner in value["owners"]:
            self.assertEqual(set(owner), {"slot", "kind", "company_id", "town_id", "label"})
            self.assertIs(type(owner["slot"]), int)
            self.assertIn(owner["kind"], ("none", "water", "town", "company", "other"))
            self.assertIsInstance(owner["label"], str)
            if owner["company_id"] is not None:
                self.assertIs(type(owner["company_id"]), int)
            if owner["town_id"] is not None:
                self.handle(owner["town_id"])
        if value["local_authority"] is not None:
            self.handle(value["local_authority"])
        for key, integer_fields in (
                ("build_date", {"year", "month", "day"}),
                ("house", {"house_type"}), ("object", {"object_type"}),
                ("station_spec", {"class_id", "spec_index"})):
            detail = value[key]
            if detail is not None:
                self.assertEqual(set(detail), integer_fields | (set() if key == "build_date" else {"name"}))
                for field in integer_fields:
                    self.assertIs(type(detail[field]), int)
                if key != "build_date":
                    self.assertIsInstance(detail["name"], str)
        self.assertIsInstance(value["accepted_cargo"], list)
        for cargo in value["accepted_cargo"]:
            self.assertEqual(set(cargo), {"cargo_id", "acceptance_eighths", "always_accepted"})
            self.assertIs(type(cargo["cargo_id"]), int)
            self.assertIs(type(cargo["acceptance_eighths"]), int)
            self.assertIs(type(cargo["always_accepted"]), bool)
        preview = value["clear_preview"]
        self.assertEqual(set(preview), {"status", "cost", "error", "advisory_tick"})
        self.assertIs(type(preview["advisory_tick"]), int)
        self.assertEqual(preview["advisory_tick"], value["tick"])
        self.assertIn(preview["status"], ("available", "blocked"))
        if preview["status"] == "available":
            self.assertIs(type(preview["cost"]), int)
            self.assertIsNone(preview["error"])
        else:
            self.assertIsNone(preview["cost"])
            self.assertEqual(set(preview["error"]), {"code", "message"})
            self.assertEqual(preview["error"]["code"], "query_test_failed")
            self.assertIsInstance(preview["error"]["message"], str)
        self.evidence.setdefault("land_info", []).append(value)

    def test_land_info_read_contract_and_admission(self):
        self.evidence["scope"] = "Stock public land.info DTO, shared admission and no gameplay submissions"
        self.evidence["not_covered"] = [
            "runtime NewGRF callback isolation", "hard callback or HTTP latency bounds",
            "renderer capture", "restart recovery", "autonomous play or business income"]
        self.evidence["generic_error_attribution"] = (
            "HTTP 503 observation_unavailable does not expose a native cause. "
            "LandInfo validates Parameters::Tile before AdmitWorldTextStart; "
            "AdmitWorldText enforces the one-second interval. See observations.cpp, "
            "actions_common.cpp, observations_world_details.cpp and native land-info cases.")
        tile = next(plan["depot"] for plan in MODES if plan["mode"] == "rail")
        self.assertEqual(tile, 10008)

        def ledger_counters():
            # Operator assertion only: no credentials, gameplay input or writes.
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as database:
                count, maximum = database.execute(
                    "SELECT COUNT(*),MAX(request_sequence) FROM action_requests WHERE world_id=?",
                    (self.world.world_id,)).fetchone()
                following, = database.execute(
                    "SELECT next_request_sequence FROM worlds WHERE id=?", (self.world.world_id,)).fetchone()
                return {"rows": count, "maximum_sequence": maximum, "next_sequence": following}

        before = ledger_counters()
        self.rejected_read({"tile": tile}, audit=False, status=403, code="forbidden")
        for parameters, status, code in (
                ({"tile": -1}, 400, "invalid_request"),
                ({"tile": {"x": 256, "y": 0}}, 503, "observation_unavailable")):
            time.sleep(1.1)
            invalid_started = time.monotonic()
            self.rejected_read(parameters, audit=True, status=status, code=code)
            valid_started = time.monotonic()
            observation = self.observe("land.info", {"tile": tile})
            invalid_probe_ms = (time.monotonic() - invalid_started) * 1000
            self.assert_land_info(observation)
            self.evidence.setdefault("invalid_admission_probes", []).append({
                "parameters": parameters, "through_valid_success_ms": invalid_probe_ms})
            self.assertLess(invalid_probe_ms, 1000, "Inconclusive admission probe: one-second precondition elapsed")
        self.rejected_read({"tile": tile}, audit=True, status=503, code="observation_unavailable")
        repeated_probe_ms = (time.monotonic() - valid_started) * 1000
        self.evidence["immediate_repeat_probe_ms"] = repeated_probe_ms
        self.assertLess(repeated_probe_ms, 1000, "Inconclusive repeat probe: one-second precondition elapsed")
        time.sleep(1.1)
        self.assert_land_info(self.observe("land.info", {"tile": tile}))
        after = ledger_counters()
        self.evidence["ledger_counters"] = {"before": before, "after": after}
        self.assertEqual(before, after)
        self.assertEqual(self.evidence["runtime_fingerprints"], shared.runtime_fingerprints(self.world.binary))
        self.evidence["result"] = "pass"


if __name__ == "__main__":
    unittest.main(defaultTest="PublicLandInfo.test_land_info_read_contract_and_admission")
