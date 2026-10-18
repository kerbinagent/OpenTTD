"""Native physical conventions and opaque-token transfer without replay refresh."""

from copy import deepcopy
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from backend.player_actions import SavedActions
from backend.player_mechanics import prepare_order, rail_track, road_bits


ROOT = Path(__file__).resolve().parents[2]
DEST = {"id": 7, "generation": 3, "engine_instance_id": "selected-boot"}
SOURCE = {"id": 8, "generation": 4, "engine_instance_id": "selected-boot"}


class Resolver:
    def __init__(self):
        self.calls = []
        self.version = "ov1:" + "aB" * 32
        self.changed = False

    def request(self, method, path, body):
        self.calls.append((method, path, deepcopy(body)))
        chosen = deepcopy(DEST if body["id"] == DEST["id"] else SOURCE)
        if self.changed:
            chosen["generation"] += 1
        return {"kind": "vehicle", "reference": chosen, "order_version": self.version}


class PlayerMechanicsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.actions = SavedActions(self.tmp.name)
        self.client = Resolver()

    def test_physical_rail_edges_match_native_exit_direction_table(self):
        source = (ROOT / "src/table/track_data.h").read_text()
        table = source.split("_track_exitdir_to_trackdir", 1)[1].split("}}};", 1)[0]
        rows = re.findall(r"\{([^{}]+)\}", table)
        expected_names = ("x", "y", "upper", "lower", "left", "right")
        physical_edges = ("NE", "SE", "SW", "NW")
        self.assertEqual(len(rows), 6)
        for row, name in zip(rows, expected_names):
            entries = [entry.strip() for entry in row.split(",")]
            edges = [edge for edge, entry in zip(physical_edges, entries) if entry != "Trackdir::Invalid"]
            self.assertEqual(len(edges), 2)
            self.assertEqual(rail_track(*edges), name)
            self.assertEqual(rail_track(*reversed(edges)), name)
        # Actual overnight curve-repair boundary choice, now direct arithmetic.
        self.assertEqual(rail_track("NE", "SE"), "right")

    def test_road_bits_remove_only_actor_selected_arithmetic(self):
        self.assertEqual(road_bits("NW"), 1)
        self.assertEqual(road_bits("SW"), 2)
        self.assertEqual(road_bits("SE"), 4)
        self.assertEqual(road_bits("NE"), 8)
        self.assertEqual(road_bits("NW", "SE"), 5)
        self.assertEqual(road_bits("NW", "SW", "SE", "NE"), 15)
        for operation in (lambda: rail_track("NE", "NE"), lambda: rail_track("east", "NW"),
                          lambda: rail_track(True, "NW"), lambda: road_bits(),
                          lambda: road_bits("NE", "NE"), lambda: road_bits(True)):
            with self.assertRaises(ValueError):
                operation()

    def test_copy_programmatically_carries_all_68_token_characters(self):
        with patch("backend.player_mechanics.time.sleep", side_effect=lambda seconds: self.assertEqual(len(self.client.calls), 1)) as sleep:
            body = prepare_order(self.client, self.actions, "order.copy", DEST, {"share": False}, source_ref=SOURCE)
            sleep.assert_called_once_with(1.05)
        parameters = body["action"]["parameters"]
        self.assertEqual(parameters["order_version"], self.client.version)
        self.assertEqual(parameters["source_order_version"], self.client.version)
        self.assertEqual(len(parameters["order_version"]), 68)
        self.assertEqual(parameters["vehicle_id"], DEST)
        self.assertEqual(parameters["source_vehicle_id"], SOURCE)
        self.assertFalse(parameters["share"])
        self.assertEqual(len(self.client.calls), 2)
        self.assertTrue(all(call[:2] == ("POST", "/v1/control/reference") for call in self.client.calls))
        original = self.actions.load(body["request_id"])
        self.client.version = "ov1:" + "0" * 64
        self.client.changed = True
        # Reopening the journal is the entire retry preparation; no resolver.
        reopened = SavedActions(self.tmp.name)
        self.assertEqual(reopened.load(body["request_id"]), original)
        self.assertEqual(len(self.client.calls), 2)

    def test_selected_incarnation_mismatch_rejects_without_saving_or_retargeting(self):
        for field, replacement in (("generation", 2), ("engine_instance_id", "previous-boot"), ("id", True)):
            selected = {**DEST, field: replacement}
            with self.assertRaises(ValueError):
                prepare_order(self.client, self.actions, "order.delete", selected, {"position": 0})
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_choices_unchanged_and_filled_fields_cannot_be_supplied(self):
        choices = {"destination": {"x": 97, "y": 95}, "load": "full", "position": 0}
        body = prepare_order(self.client, self.actions, "order.insert", DEST, choices)
        choices["destination"]["x"] = 98
        self.assertEqual(body["action"]["parameters"]["destination"]["x"], 97)
        for name, parameters, source in (("vehicle.depot", {}, None), ("order.copy", {}, None),
                                         ("order.delete", {}, SOURCE), ("order.delete", {"order_version": "manual"}, None),
                                         ("order.copy", {"source_vehicle_id": SOURCE}, SOURCE)):
            with self.assertRaises(ValueError):
                prepare_order(self.client, self.actions, name, DEST, parameters, source_ref=source)


if __name__ == "__main__":
    unittest.main()
