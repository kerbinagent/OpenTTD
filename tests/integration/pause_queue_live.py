"""Opt-in native queued-before-pause publication and receipt regression.

This uses only a new disposable LiveWorld, its authenticated Unix RPC and finite
operator pause commands. It does not test public HTTP, renderer capture, player
perception or autonomous businesses. No process suspension or timing hook is
used. A missed queue window fails with evidence; it never counts as a pass.

    OPENTTD_TEST_BINARY=/path/to/openttd OPENTTD_TEST_BASESET=/path/to/baseset \
      OPENTTD_TEST_KEEP=1 python3 tests/integration/pause_queue_live.py -v

OPENTTD_TEST_PORT defaults to 26978 and must be unused. Other LiveWorld settings
(including OPENTTD_TEST_LIBRARY_PATH) apply normally.
"""

from __future__ import annotations

from contextlib import ExitStack
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import unittest
import uuid

from engine_harness import Action, LiveWorld, MAX_FRAME
from fixture_plans import MODES


def stamp():
    return {"utc": datetime.now(timezone.utc).isoformat(), "monotonic_ns": time.monotonic_ns()}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class QueueWorld(LiveWorld):
    """Trace real transport; allow only this fixture's finite operator controls."""

    allowed_console = frozenset({"pause", "unpause", "quit"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rpc_trace = []
        self.console_trace = []

    def rpc(self, kind, **fields):
        row = {"type": kind, "fields": copy.deepcopy(fields), "started": stamp()}
        self.rpc_trace.append(row)
        try:
            reply = super().rpc(kind, **fields)
            row["reply"] = reply
            return reply
        except BaseException as error:
            row["exception"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            row["finished"] = stamp()

    def console(self, command):
        if command not in self.allowed_console:
            raise AssertionError(f"Queue fixture forbids console command: {command}")
        row = {"command": command, "started": stamp()}
        self.console_trace.append(row)
        try:
            return super().console(command)
        finally:
            row["finished"] = stamp()


@unittest.skipUnless(os.environ.get("OPENTTD_TEST_BINARY") and os.environ.get("OPENTTD_TEST_BASESET"),
                     "Set matching integration binary and baseset for native queue acceptance")
class QueuedPauseLive(unittest.TestCase):
    def setUp(self):
        self.world = QueueWorld(os.environ["OPENTTD_TEST_BINARY"], os.environ["OPENTTD_TEST_BASESET"],
                                port=int(os.environ.get("OPENTTD_TEST_PORT", "26978")))
        self.evidence = {"result": "incomplete", "scope": "native queue publication and immutable retry outcome",
                         "not_covered": ["public HTTP", "renderer capture", "process restart",
                                         "autonomous image perception or business success", "arbitrary GameScript freeze"],
                         "batch": [], "rpc_trace": self.world.rpc_trace,
                         "operator_console": self.world.console_trace, "snapshots": []}
        self.addCleanup(self.close)
        self.files = [self.world.binary, self.world.fixture, Path(__file__),
                      Path(__file__).with_name("engine_harness.py"), Path(__file__).with_name("fixture_plans.py")]
        self.evidence["files_before"] = {str(path.resolve()): digest(path) for path in self.files}
        print(f"Native pause queue artifacts: {self.world.directory}", flush=True)
        self.world.start()
        self.world.finish_recovery()
        self.world.actor = self.world.bind(0)
        self.evidence.update(world_id=self.world.world_id, world_epoch=self.world.epoch,
                             engine_instance_id=self.world.instance)

    def close(self):
        try:
            self.evidence["files_after"] = {str(path.resolve()): digest(path) for path in getattr(self, "files", [])}
            if self.evidence.get("files_before") != self.evidence["files_after"]:
                self.evidence["result"] = "failed_runtime_changed"
            elif self.evidence["result"] == "incomplete":
                self.evidence["result"] = "failed_or_incomplete"
            path = self.world.directory / "pause-queue-result.json"
            path.write_text(json.dumps(self.evidence, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            print(f"Native pause queue evidence: {path}", flush=True)
            self.assertEqual(self.evidence.get("files_before"), self.evidence["files_after"])
        finally:
            self.world.close()

    def wait(self, label, predicate):
        self.evidence["stage"] = label
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail(f"Timed out waiting for {label}")

    def pause_state(self):
        reply = self.world.rpc("hello")
        self.assertEqual(reply["status"], "completed", reply)
        pause = reply["result"]["pause"]
        self.assertIs(type(pause["paused"]), bool)
        self.assertEqual(set(pause), {"paused", "reasons", "command_policy", "revision"})
        return reply

    def expected_pause(self, *, paused):
        def poll():
            reply = self.pause_state()
            value = reply["result"]["pause"]
            self.assertEqual(value["command_policy"], "no_construction", "Fixture must retain its saved native policy")
            if value["paused"] is paused:
                if paused:
                    self.assertIn("manual", value["reasons"])
                return reply
        return self.wait(f"paused={paused}, policy=no_construction", poll)

    def tile_snapshot(self, plan):
        return self.world.observe("map.region", {"x": plan["depot"] % 256,
                                                "y": plan["depot"] // 256, "width": 1, "height": 1})

    def stable_snapshot(self, label, plan):
        before = self.expected_pause(paused=True)
        company = self.world.observe("company")
        region = self.tile_snapshot(plan)
        after = self.expected_pause(paused=True)
        self.assertEqual({before["tick"], company["tick"], region["tick"], after["tick"]}, {before["tick"]},
                         "Finance assertions require one stable paused simulation tick")
        value = {"label": label, "tick": before["tick"], "pause": after["result"]["pause"],
                 "company": company, "tiles": region["tiles"]}
        self.evidence["snapshots"].append(value)
        return value

    def depot_snapshot(self):
        result = self.world.observe("depots", {"limit": 100})
        self.assertIsNone(result["next_after_id"], "Fixture depots must fit one bounded page")
        return result["items"]

    def make_action(self, name, parameters):
        self.world.sequence += 1
        action = Action(self.world.sequence, uuid.uuid4().hex, copy.deepcopy(self.world.actor),
                        {"name": name, "parameters": copy.deepcopy(parameters)})
        self.world.accepted.append(action)
        return action

    def batch_then_pause(self, target):
        actions = [self.make_action("company.rename", {"name": f"Queue filler {number}"}) for number in range(15)]
        actions.append(self.make_action(target["build"], target["parameters"]))
        with ExitStack() as stack:
            connections = []
            for action in actions:
                correlation = uuid.uuid4().hex
                request = {"protocol_version": 1, "type": "action.submit", "correlation_id": correlation,
                           "world_id": self.world.world_id, "world_epoch": self.world.epoch,
                           "engine_instance_id": self.world.instance, **action.fields()}
                data = json.dumps(request, separators=(",", ":"), allow_nan=False).encode() + b"\n"
                self.assertLessEqual(len(data), MAX_FRAME)
                connection = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                connection.settimeout(4)
                row = {"request": request, "send_started": stamp()}
                self.evidence["batch"].append(row)
                connection.connect(str(self.world.socket_path))
                connection.sendall(data)
                row["send_finished"] = stamp()
                connections.append((connection, row))

            def receive(index):
                connection, row = connections[index]
                response = bytearray()
                row["receive_started"] = stamp()
                while b"\n" not in response:
                    block = connection.recv(min(4096, MAX_FRAME + 1 - len(response)))
                    self.assertTrue(block, "Truncated batch reply")
                    response.extend(block)
                    self.assertLessEqual(len(response), MAX_FRAME)
                line, trailing = response.split(b"\n", 1)
                self.assertFalse(trailing, "Unexpected extra batch frame")
                reply = json.loads(line)
                row.update(reply=reply, receive_finished=stamp())
                request = row["request"]
                for key in ("protocol_version", "correlation_id", "world_id", "world_epoch", "engine_instance_id"):
                    self.assertEqual(reply[key], request[key])
                self.assertEqual(reply["type"], "action.submit.result")
                return reply

            initial = receive(15)
            self.assertEqual(initial["status"], "queued", "Missed race: target was not natively queued before pause")
            self.assertEqual(initial["result"]["request_sequence"], actions[-1].sequence)
            self.assertEqual(initial["result"]["request_id"], actions[-1].request_id)
            self.world.console("pause")
            for index in range(15):
                receive(index)
        return actions, initial

    @staticmethod
    def immutable_receipt(reply):
        # Checkpoint publication may advance this annotation without changing
        # the request's identity, executed boot, terminal outcome or error.
        return {"status": reply["status"], "error": reply.get("error"),
                "result": {key: value for key, value in reply["result"].items() if key != "checkpoint_state"}}

    def test_queued_work_rejected_uncharged_and_retries_remain_terminal(self):
        plan = next(item for item in MODES if item["mode"] == "rail")
        # command_pause_level is saved and NoNetwork; an ordinary multiplayer
        # operator cannot change it. Use the regression fixture's real policy.
        self.expected_pause(paused=False)
        actions, initial = self.batch_then_pause(plan)
        target = actions[-1]
        before = self.stable_snapshot("paused before target publication", plan)
        queued = self.world.rpc("action.status", **target.fields())
        self.assertEqual(queued["status"], "queued", "Missed race: no queued target remained after the paused finance baseline")
        terminal = self.world.wait(target, queued)
        self.assertEqual(terminal["status"], "rejected", terminal)
        self.assertEqual(terminal["error"]["code"], "world_paused", terminal)
        self.assertEqual(terminal["result"]["error"], terminal["error"])
        self.assertEqual(terminal["result"]["request_sequence"], target.sequence)
        self.assertEqual(terminal["result"]["request_id"], target.request_id)
        self.evidence["queue_target"] = {"action": target.fields(), "initial": initial,
                                         "paused_pending": queued, "terminal": terminal}
        after = self.stable_snapshot("paused after target rejection", plan)
        for field in ("balance", "loan", "income_this_quarter", "expenses_this_quarter", "infrastructure"):
            self.assertEqual(after["company"][field], before["company"][field])
        self.assertEqual(after["tiles"], before["tiles"])
        self.assertEqual(after["tick"], before["tick"])

        for filler in actions[:-1]:
            completed = self.world.wait(filler)
            self.assertEqual(completed["status"], "completed", completed)
            self.assertTrue(completed["result"]["success"], completed)
        allowed = self.world.action("company.rename", {"name": "Allowed while paused"})
        self.assertEqual(allowed["status"], "completed", allowed)
        self.assertTrue(allowed["result"]["success"], allowed)
        still_paused = self.stable_snapshot("permitted rename completed before resume", plan)
        self.assertEqual(still_paused["company"]["name"], "Allowed while paused")
        self.assertEqual(still_paused["company"]["balance"], before["company"]["balance"])
        self.assertEqual(still_paused["tick"], before["tick"])
        retry = self.world.rpc("action.submit", **target.fields())
        self.assertEqual(self.immutable_receipt(retry), self.immutable_receipt(terminal))
        self.world.console("unpause")
        self.expected_pause(paused=False)
        resumed_retry = self.world.rpc("action.submit", **target.fields())
        self.assertEqual(self.immutable_receipt(resumed_retry), self.immutable_receipt(terminal))

        # A deliberate new intent follows a real resume. Its native receipt
        # reports the positive actual cost. Running-time finance deltas are
        # deliberately not used to measure that fee.
        fresh_before = self.tile_snapshot(plan)
        depots_before = self.depot_snapshot()
        self.assertEqual(fresh_before["tiles"], before["tiles"])
        self.assertFalse(any(item["location"]["tile"] == plan["depot"] for item in depots_before))
        fresh_action, fresh_initial = self.world.enqueue(plan["build"], plan["parameters"])
        self.assertNotEqual(fresh_action.request_id, target.request_id)
        fresh = self.world.wait(fresh_action, fresh_initial)
        self.assertEqual(fresh["status"], "completed", fresh)
        self.assertTrue(fresh["result"]["success"], fresh)
        self.assertGreater(fresh["result"]["cost"], 0)
        fresh_after = self.tile_snapshot(plan)
        self.assertNotEqual(fresh_before["tiles"], fresh_after["tiles"])
        self.assertTrue(fresh_after["tiles"][0]["depot"])
        self.assertEqual(fresh_after["tiles"][0]["owner"], self.world.actor["company_id"])
        depots_after = self.depot_snapshot()
        self.assertEqual(len(depots_after), len(depots_before) + 1)
        for item in depots_before:
            self.assertIn(item["depot_id"], [depot["depot_id"] for depot in depots_after])
        self.assertEqual(len([item for item in depots_after if item["location"]["tile"] == plan["depot"]]), 1)
        fresh_retry = self.world.rpc("action.submit", **fresh_action.fields())
        self.assertEqual(self.immutable_receipt(fresh_retry), self.immutable_receipt(fresh))
        final = self.tile_snapshot(plan)
        self.assertEqual(final["tiles"], fresh_after["tiles"])
        final_depots = self.depot_snapshot()
        self.assertEqual([item["depot_id"] for item in final_depots], [item["depot_id"] for item in depots_after])
        self.evidence["fresh_intent"] = {"action": fresh_action.fields(), "initial": fresh_initial, "terminal": fresh,
                                          "retry": fresh_retry, "before": fresh_before, "after": fresh_after,
                                          "depots_before": depots_before, "depots_after": depots_after,
                                          "qualification": "after resume; positive native actual cost, no running balance-delta assertion"}
        self.assertEqual(self.evidence["files_before"], {str(path.resolve()): digest(path) for path in self.files})
        self.evidence["result"] = "pass"


if __name__ == "__main__":
    unittest.main()
