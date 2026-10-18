"""Pure speed-attribution and audit-compatibility checks; no native world needed."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import load_world


NS = 1_000_000_000


def observation(tick, start, received, **changes):
    value = {"tick": tick, "rpc_started_monotonic_ns": int(start * NS),
             "rpc_received_monotonic_ns": int(received * NS),
             "observed_monotonic": int(received * NS) / NS,
             "world_id": "world-0", "world_epoch": "epoch-0", "engine_instance_id": "instance-0",
             "hello_type": "hello.result", "hello_status": "completed", "hello_role": "world"}
    value.update(changes)
    return value


class TickBracketTests(unittest.TestCase):
    def test_capture_stores_exact_bracket_and_authoritative_fence(self):
        class World:
            world_id, epoch, instance = "world-0", "epoch-0", "instance-0"

            def rpc(self, name):
                self.request_name = name
                return {"tick": 100, "type": "hello.result", "world_id": self.world_id, "world_epoch": self.epoch,
                        "engine_instance_id": self.instance, "status": "completed",
                        "result": {"role": "world"}}

        world = World()
        with mock.patch.object(load_world.time, "monotonic_ns", side_effect=[123 * NS, 123 * NS + 2_000_000]):
            sample = load_world.Load.tick_sample(SimpleNamespace(world=world))
        self.assertEqual(world.request_name, "hello")
        self.assertEqual(sample["rpc_received_monotonic_ns"]-sample["rpc_started_monotonic_ns"], 2_000_000)
        self.assertEqual(sample["observed_monotonic"], sample["rpc_received_monotonic_ns"] / NS)

    def test_narrow_brackets_corroborate_pass_and_prove_slow(self):
        first = observation(100, 0, .01)
        normal = observation(285, 5, 5.01)
        passed = load_world.tick_window_diagnostic(first, normal, .999)
        self.assertGreaterEqual(passed["normal_speed_lower_bound"], .95)
        self.assertEqual(passed["classification"], "receipt_pass_corroborated")
        slow = load_world.tick_window_diagnostic(first, observation(270, 5, 5.01), .918)
        self.assertLess(slow["normal_speed_upper_bound"], .95)
        self.assertEqual(slow["classification"], "proven_slow_within_brackets")

    def test_delayed_receipt_and_threshold_straddle_remain_inconclusive(self):
        first = observation(100, 0, .01)
        delayed = observation(285, 5, 5.4)
        receipt = (delayed["tick"]-first["tick"])*load_world.TICK_SECONDS / (5.4-.01)
        self.assertLess(receipt, .95)
        result = load_world.tick_window_diagnostic(first, delayed, receipt)
        self.assertGreaterEqual(result["normal_speed_upper_bound"], .95)
        self.assertEqual(result["classification"], "timing_inconclusive")
        straddle = load_world.tick_window_diagnostic(first, observation(276, 5, 5.1), .934)
        self.assertLess(straddle["normal_speed_lower_bound"], .95)
        self.assertGreaterEqual(straddle["normal_speed_upper_bound"], .95)
        self.assertEqual(straddle["classification"], "timing_inconclusive")

    def test_overlap_has_explicit_unbounded_upper(self):
        result = load_world.tick_window_diagnostic(observation(100, 0, 5.2),
                                                   observation(285, 5, 5.5), .9)
        self.assertEqual(result["minimum_elapsed_ns"], 0)
        self.assertIsNone(result["normal_speed_upper_bound"])
        self.assertEqual(result["upper_bound_reason"], "overlapping_rpc_brackets")
        self.assertEqual(result["classification"], "timing_inconclusive")

    def test_invalid_types_order_and_fences_fail(self):
        first = observation(100, 0, .01)
        for bad in (observation(True, 5, 5.01), observation(285, 5, 5.01, rpc_started_monotonic_ns=True),
                    observation(285, 5, 4), observation(285, 5, 5.01, world_epoch="changed"),
                    observation(285, 5, 5.01, engine_instance_id="changed"),
                    observation(285, 5, 5.01, hello_type="wrong.result"),
                    observation(285, 5, 5.01, hello_status="queued"),
                    observation(285, 5, 5.01, observed_monotonic=5.02),
                    observation(99, 5, 5.01), observation(285, 0, 5.01)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                load_world.tick_window_diagnostic(first, bad, .9)
        with self.assertRaises(ValueError):
            load_world.tick_window_diagnostic(first, observation(285, 5, 5.01), True)

    def test_marked_raw_missing_or_changed_fence_fails_without_dropping_window(self):
        first = observation(100, 0, .01)
        valid = observation(285, 5, 5.01)
        base = [{"world": 0, "observation": first}]
        manifest = {"0": "world-0"}
        rows = [{"worlds": {"0": {**valid, "normal_speed_ratio": .999}}}]
        legacy = load_world.analyze_tick_brackets(load_world.LEGACY_TICK_MEASUREMENT, [], rows, manifest)
        self.assertEqual(legacy["status"], "legacy_unavailable")
        self.assertTrue(legacy["proof_valid"])
        self.assertFalse(load_world.analyze_tick_brackets(None, base, rows, manifest)["proof_valid"])
        self.assertFalse(load_world.analyze_tick_brackets("future_format", base, rows, manifest)["proof_valid"])
        missing = load_world.analyze_tick_brackets(load_world.TICK_MEASUREMENT_FORMAT, [], rows, manifest)
        self.assertFalse(missing["proof_valid"])
        self.assertEqual(len(missing["windows"]["0"]), 1)
        changed = copy.deepcopy(rows)
        changed[0]["worlds"]["0"]["engine_instance_id"] = "other"
        failed = load_world.analyze_tick_brackets(load_world.TICK_MEASUREMENT_FORMAT, base, changed, manifest)
        self.assertFalse(failed["proof_valid"])
        self.assertEqual(len(failed["windows"]["0"]), 1)

    def test_marked_raw_recomputes_each_window_without_promoting_failed_receipt(self):
        first = observation(100, 0, .01)
        normal = observation(285, 5, 5.01)
        slow = observation(455, 10, 10.01)
        baseline = [{"world": 0, "observation": first}]
        rows = [{"worlds": {"0": {**normal, "normal_speed_ratio":
                                     185*load_world.TICK_SECONDS/(normal["observed_monotonic"]-first["observed_monotonic"])}}},
                {"worlds": {"0": {**slow, "normal_speed_ratio":
                                     170*load_world.TICK_SECONDS/(slow["observed_monotonic"]-normal["observed_monotonic"])}}}]
        result = load_world.analyze_tick_brackets(load_world.TICK_MEASUREMENT_FORMAT,
                                                   baseline, rows, {"0": "world-0"})
        self.assertTrue(result["proof_valid"], result["errors"])
        self.assertEqual(len(result["windows"]["0"]), 2)
        self.assertEqual(result["windows"]["0"][1]["classification"], "proven_slow_within_brackets")
        self.assertLess(result["windows"]["0"][1]["receipt_ratio"], .95)


class AuditCompatibilityTests(unittest.TestCase):
    def audit_fixture(self, directory, *, measurement_format=load_world.LEGACY_TICK_MEASUREMENT,
                      corrupt_worlds=False, valid_brackets=False):
        config = {"duration": 1800, "vehicles": 500, "viewers": 50, "worlds": 4,
                  "action_rate": 1, "chat_rate": 1, "map": [512, 512]}
        if measurement_format is not load_world.LEGACY_TICK_MEASUREMENT:
            config["tick_measurement_format"] = measurement_format
        manifest = {"worlds": [{"id": f"world-{i}", "directory": str(directory)} for i in range(4)]}
        summary = {"steady_seconds": 1800, "action_posts_attempted": 0, "chat_posts_attempted": 0,
                   "inflight_generators": 0, "unresolved_steady_http": {}, "steady_http_started": {}}
        for name, data in (("configuration.json", config), ("manifest.json", manifest), ("summary.json", summary)):
            (directory / name).write_text(json.dumps(data))
        rows = [{"kind": "steady_started", "monotonic": 0, "wall_time": 0, "phase": "steady"}]
        if valid_brackets:
            for world in range(4):
                rows.append({"kind": "tick_baseline", "monotonic": .01, "phase": "steady", "world": world,
                             "observation": observation(100, 0, .01, world_id=f"world-{world}")})
        for index in range(4):
            if valid_brackets:
                current = observation(285+185*index, 5.0*(index+1), 5.0*(index+1)+.01)
                previous = observation(100+185*index, 5.0*index, 5.0*index+.01)
                ratio = (current["tick"]-previous["tick"])*load_world.TICK_SECONDS / (
                    current["observed_monotonic"]-previous["observed_monotonic"])
                worlds = {str(world): {**current, "world_id": f"world-{world}", "normal_speed_ratio": ratio}
                          for world in range(4)}
            else:
                worlds = {str(world): {"tick": 100+185*index, "observed_monotonic": 5.0*index,
                                        "normal_speed_ratio": None if index == 0 else .999}
                          for world in range(4)}
            rows.append({"kind": "world_sample", "monotonic": 5.0*(index+1) if valid_brackets else 5.0*index,
                         "phase": "steady",
                         "worlds": worlds, "processes": {}, "active_heads": 500, "moving_now": 500,
                         "connected_sse": 50, "backend_pending_actions": 0, "backend_pending_chat": 0})
        if corrupt_worlds:
            rows[2]["worlds"] = None
        rows.append({"kind": "run_finished", "monotonic": 1800, "phase": "drain", "completed": True})
        (directory / "samples.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
        return load_world.analyze_results(directory)

    def test_legacy_verdict_and_attempt_denominators_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            legacy = self.audit_fixture(directory)
            self.assertEqual(legacy["tick_bracket_diagnostics"]["status"], "legacy_unavailable")
            self.assertNotIn("tick_measurement_proof", legacy["checks"])
            self.assertEqual(legacy["http_attempts"], {})
            self.assertFalse(legacy["target_pass"])
            self.assertEqual(legacy["result_label"], "TARGET_NOT_MET")
            marked = self.audit_fixture(directory, measurement_format=load_world.TICK_MEASUREMENT_FORMAT)
            self.assertEqual(marked["http_attempts"], legacy["http_attempts"])
            self.assertEqual(marked["observed_interior_minimum_five_second_speed_ratio"],
                             legacy["observed_interior_minimum_five_second_speed_ratio"])
            self.assertEqual(marked["checks"]["sampled_window_normal_speed"],
                             legacy["checks"]["sampled_window_normal_speed"])
            self.assertFalse(marked["checks"]["tick_measurement_proof"])
            null_marked = self.audit_fixture(directory, measurement_format=None)
            self.assertFalse(null_marked["checks"]["tick_measurement_proof"])
            self.assertEqual(null_marked["tick_bracket_diagnostics"]["status"], "unknown_format")

    def test_valid_marked_audit_keeps_strict_checks_and_all_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            legacy = self.audit_fixture(directory)
            marked = self.audit_fixture(directory, measurement_format=load_world.TICK_MEASUREMENT_FORMAT,
                                        valid_brackets=True)
            self.assertTrue(marked["checks"]["tick_measurement_proof"], marked["tick_bracket_diagnostics"]["errors"])
            self.assertEqual(len(marked["tick_bracket_diagnostics"]["windows"]["0"]), 4)
            self.assertEqual({key: value for key, value in marked["checks"].items() if key != "tick_measurement_proof"},
                             legacy["checks"])
            self.assertEqual(marked["http_attempts"], legacy["http_attempts"])
            self.assertEqual(marked["result_label"], legacy["result_label"])

    def test_marked_malformed_world_map_fails_proof_without_audit_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.audit_fixture(Path(temporary), measurement_format=load_world.TICK_MEASUREMENT_FORMAT,
                                        corrupt_worlds=True)
            self.assertFalse(result["checks"]["tick_measurement_proof"])
            self.assertFalse(result["target_pass"])
            self.assertEqual(len(result["tick_bracket_diagnostics"]["windows"]["0"]), 4)


if __name__ == "__main__":
    unittest.main()
