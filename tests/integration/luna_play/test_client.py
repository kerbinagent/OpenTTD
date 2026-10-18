"""Public player recovery keeps saved intent separate from a new decision."""

from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid


_SPEC = importlib.util.spec_from_file_location("luna_player_client", Path(__file__).with_name("client.py"))
player_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(player_module)


class FakePublicBackend:
    """Apply each ID once and optionally lose its first completed receipt."""

    def __init__(self):
        self.calls = []
        self.receipts = {}
        self.applied = 0
        self.lose_next_reply = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if (method, path) != ("POST", "/v1/actions"):
            raise AssertionError("Recovery must send the original intent without refreshing its targets")
        request_id = body["request_id"]
        if request_id not in self.receipts:
            self.applied += 1
            self.receipts[request_id] = {"request_id": request_id, "status": "completed",
                                         "result": {"success": True}}
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise OSError("Completed response lost in transport")
        return deepcopy(self.receipts[request_id])


class PlayerClientJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.artifacts = Path(self.temporary.name) / "player"
        self.backend = FakePublicBackend()
        credential = patch.object(player_module, "credential_from_env_or_file", return_value="test-secret")
        constructor = patch.object(player_module, "BackendClient", return_value=self.backend)
        credential.start()
        constructor.start()
        self.addCleanup(credential.stop)
        self.addCleanup(constructor.stop)

    def player(self):
        return player_module.PlayerClient("http://127.0.0.1:1", "unused-credential-file", self.artifacts)

    @staticmethod
    def intent(request_id="original-intent"):
        return {"request_id": request_id, "action": {"name": "vehicle.clone", "parameters": {
            "vehicle_id": {"id": 7, "generation": 41, "engine_instance_id": "original-boot"},
            "tile": {"x": 10, "y": 20}, "share_orders": True,
        }}}

    def journal(self):
        return [json.loads(line) for line in (self.artifacts / "public-io.jsonl").read_text().splitlines()]

    def test_lost_reply_recovered_after_reconstruction_with_exact_original_handles(self):
        body = self.intent()
        original = deepcopy(body)
        self.backend.lose_next_reply = True
        with self.assertRaises(OSError):
            self.player().issue(body)
        self.assertEqual(self.backend.applied, 1)

        # A subsequent observation or caller mutation must not repair an old
        # intent to target a different vehicle incarnation or engine boot.
        body["action"]["parameters"]["vehicle_id"].update(
            generation=42, engine_instance_id="replacement-boot")
        recovered = self.player().retry(original["request_id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self.backend.applied, 1)
        self.assertEqual([call[2] for call in self.backend.calls], [original, original])
        saved = json.loads((self.artifacts / "requests" / "original-intent.json").read_text())
        self.assertEqual(saved, original)
        self.assertEqual([row["submission"]["kind"] for row in self.journal() if "submission" in row],
                         ["new_intent", "exact_replay"])

    def test_same_id_with_type_conflict_is_rejected_before_network(self):
        player = self.player()
        body = self.intent()
        player.issue(body)
        changed = deepcopy(body)
        changed["action"]["parameters"]["share_orders"] = 1
        with self.assertRaises(ValueError):
            player.issue(changed)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertIs(player.actions.load(body["request_id"])["action"]["parameters"]["share_orders"], True)

    def test_cli_new_decisions_receive_independent_uuids(self):
        parameters = {"vehicle_id": self.intent()["action"]["parameters"]["vehicle_id"]}
        arguments = ["client.py", "--origin", "http://127.0.0.1:1", "--token-file", "unused",
                     "--artifacts", str(self.artifacts), "action", "vehicle.start_stop", json.dumps(parameters)]
        for _ in range(2):
            with patch.object(player_module.sys, "argv", arguments), redirect_stdout(io.StringIO()):
                player_module.main()
        first, second = [call[2] for call in self.backend.calls]
        self.assertEqual(first["action"], second["action"])
        self.assertNotEqual(first["request_id"], second["request_id"])
        for body in (first, second):
            self.assertEqual(uuid.UUID(body["request_id"]).version, 4)
        self.assertEqual(self.backend.applied, 2)

        # Recovering the older completed receipt does not create another
        # execution after a newer decision has changed the same target.
        self.player().retry(first["request_id"])
        self.assertEqual(self.backend.calls[-1][2], first)
        self.assertEqual(self.backend.applied, 2)


if __name__ == "__main__":
    unittest.main()
