"""Finite shared pause metadata and the reference client's snapshot helper."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import projection
from backend.client import BackendClient
from backend.errors import ServiceError


class WorldStatusProjectionTests(unittest.TestCase):
    def setUp(self):
        self.pause = {"paused": True, "reasons": ["manual", "error", "link_graph"],
                      "command_policy": "no_construction", "revision": 7}

    def test_projects_only_native_pause_facts_in_order(self):
        raw = {**self.pause, "tick": 999, "raw_pause_flags": 12,
               "company": {"balance": 100}, "actor": "private"}
        expected = dict(self.pause)
        projected = projection.world_status(raw)
        self.assertEqual(projected, expected)
        self.assertEqual(projection.world_status({"paused": False, "reasons": [],
            "command_policy": "all_actions", "revision": 0}),
            {"paused": False, "reasons": [], "command_policy": "all_actions", "revision": 0})
        self.assertEqual(projection.world_status({**self.pause,
            "reasons": ["other"], "revision": (1 << 64) - 1})["revision"], (1 << 64) - 1)
        projected["reasons"].append("joining")
        self.assertEqual(raw["reasons"], ["manual", "error", "link_graph"])

    def test_malformed_pause_fails_closed_instead_of_claiming_unpaused(self):
        invalid = [None, [], {},
                   {key: value for key, value in self.pause.items() if key != "paused"},
                   {**self.pause, "paused": 1},
                   {**self.pause, "paused": False},
                   {**self.pause, "paused": True, "reasons": []},
                   {**self.pause, "reasons": "manual"},
                   {**self.pause, "reasons": ("manual",)},
                   {**self.pause, "reasons": ["manual", "manual"]},
                   {**self.pause, "reasons": ["error", "manual"]},
                   {**self.pause, "reasons": ["manual", "private"]},
                   {**self.pause, "reasons": [True]},
                   {**self.pause, "reasons": ["manual", "other"]},
                   {**self.pause, "reasons": ["manual"] * 9},
                   {**self.pause, "command_policy": "private"},
                   {**self.pause, "revision": True},
                   {**self.pause, "revision": -1},
                   {**self.pause, "revision": 1 << 64},
                   {**self.pause, "revision": 1.0}]
        for raw in invalid:
            with self.subTest(raw=repr(raw)[:100]), self.assertRaises(ServiceError) as caught:
                projection.world_status(raw)
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("world_status_unavailable", 503))

    def test_schema_and_client_expose_snapshot_without_new_stream_state(self):
        schema = json.loads((Path(__file__).parents[1] / "schemas" / "public-api-v1.json").read_text())
        defs = schema["$defs"]
        self.assertEqual(defs["pauseStatus"]["required"],
                         ["paused", "reasons", "command_policy", "revision"])
        self.assertEqual(defs["capabilitiesResponse"]["properties"]["world_status"]
                         ["properties"], {
            "endpoint": {"const": "/v1/world/status"},
            "event": {"const": "world.status"}})
        self.assertTrue(any(branch["properties"]["type"].get("const") == "world.status"
                            for branch in defs["event"]["oneOf"]))
        client = BackendClient("http://127.0.0.1:8080", "token")
        snapshot = {"world_id": "w1", "world_epoch": "initial",
                    "engine_instance_id": "boot", "pause": self.pause,
                    "event_cursor": "cursor-1"}
        with patch.object(client, "request", return_value=snapshot) as request:
            self.assertIs(client.world_status(), snapshot)
            request.assert_called_once_with("GET", "/v1/world/status")


if __name__ == "__main__":
    unittest.main()
