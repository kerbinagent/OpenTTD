"""Opt-in native-client communication acceptance against a real engine/backend.

Run from the repository root with explicit integration binary, pristine baseline
binary, and OpenGFX assets. No running world is reused or modified::

    OPENTTD_TEST_BINARY=/path/to/integration/openttd \
    OPENTTD_TEST_PRISTINE_BINARY=/path/to/pristine/openttd \
    OPENTTD_TEST_BASESET=/path/to/baseset \
    OPENTTD_TEST_KEEP=1 python3 tests/integration/native_chat_live.py -v

OPENTTD_TEST_PORT defaults to 15979. OPENTTD_TEST_LIBRARY_PATH supplies a local
dependency prefix. This takes about one game month (usually one minute), because
native chat is sent through an ordinary scheduled client console script. Human
clients use the genuine framebuffer driver; no display server is required.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engine_harness import LiveWorld
from backend.bridge import UnixEngine
from backend.errors import ServiceError
from backend.service import Service
from backend.store import Store


@dataclass
class NativeClient:
    name: str
    directory: Path
    process: subprocess.Popen
    log: object

    def received(self):
        # NetworkTextMessage writes its display text to this dedicated debug log,
        # not stderr. Reading stderr alone cannot prove native presentation.
        path = self.directory / "data/openttd/save/autosave/commands-out.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def diagnostic(self):
        return (self.directory / "client.log").read_text(errors="replace")


REQUIRED = ("OPENTTD_TEST_BINARY", "OPENTTD_TEST_PRISTINE_BINARY", "OPENTTD_TEST_BASESET")


@unittest.skipUnless(all(os.environ.get(name) for name in REQUIRED),
                     "Set integration/pristine binaries and OpenGFX assets for live native acceptance")
class NativeChatAcceptance(unittest.TestCase):
    def setUp(self):
        self.binary = Path(os.environ["OPENTTD_TEST_BINARY"]).resolve()
        self.pristine = Path(os.environ["OPENTTD_TEST_PRISTINE_BINARY"]).resolve()
        self.baseset = Path(os.environ["OPENTTD_TEST_BASESET"]).resolve()
        self.assertTrue(self.binary.is_file())
        self.assertTrue(self.pristine.is_file())
        self.assertNotEqual(self.binary, self.pristine)
        self.assertTrue(self.baseset.is_dir())
        self.world = LiveWorld(self.binary, self.baseset,
                               port=int(os.environ.get("OPENTTD_TEST_PORT", "15979")))
        self.clients = []
        self.store = None
        self.service = None
        self.stage = "world startup"
        self.addCleanup(self.close)
        self.start_generated_world()
        self.world.finish_recovery()
        self.world.console("unpause")
        for company in range(3):
            self.world.bind(company)
            reply = self.world.rpc("company.access", company_id=company,
                                   allow_any=True, add_keys=[], remove_keys=[])
            self.assertIn(reply["status"], ("queued", "completed"), reply)
        self.store = Store(self.world.directory / "backend.sqlite3", world_id=self.world.world_id)
        self.tokens = {
            company: self.store.provision(self.world.world_id, f"agent{company}", company,
                                          kind="agent", display_name=f"Agent {company}")
            for company in range(3)
        }
        self.service = Service(self.store, UnixEngine(self.world.socket_path, timeout=4),
                               world_id=self.world.world_id, autostart=False,
                               checkpoint_interval=300)
        self.pump()

    def environment(self, data, config):
        environment = dict(os.environ)
        for key in tuple(environment):
            if key.startswith("OPENTTD_INTEGRATION_"):
                environment.pop(key)
        environment.update(XDG_DATA_HOME=str(data), XDG_CONFIG_HOME=str(config))
        if os.environ.get("OPENTTD_TEST_LIBRARY_PATH"):
            environment["LD_LIBRARY_PATH"] = os.environ["OPENTTD_TEST_LIBRARY_PATH"] + os.pathsep + environment.get("LD_LIBRARY_PATH", "")
        return environment

    def start_generated_world(self):
        world = self.world
        with world.config_file.open("a") as config:
            config.write("\n[game_creation]\nmap_x = 6\nmap_y = 6\nstarting_year = 1950\n")
        environment = self.environment(world.data, world.config)
        environment.update({
            "OPENTTD_INTEGRATION_SOCKET": str(world.socket_path),
            "OPENTTD_INTEGRATION_STATE_DIR": str(world.state),
            "OPENTTD_INTEGRATION_WORLD_ID": world.world_id,
            "OPENTTD_INTEGRATION_WORLD_EPOCH": world.epoch,
        })
        world.log = (world.directory / "engine.log").open("wb", buffering=0)
        world.process = subprocess.Popen(
            [str(self.binary), "-D", f"127.0.0.1:{world.port}", "-g", "-x",
             "-I", "OpenGFX", "-d", "net=9"],
            cwd=world.directory, env=environment, stdin=subprocess.PIPE,
            stdout=world.log, stderr=subprocess.STDOUT, start_new_session=True,
        )

        def ready():
            if world.process.poll() is not None:
                self.fail(f"Engine exited during generation:\n{world.log_tail()}")
            try:
                world.instance = world.rpc("hello")["engine_instance_id"]
                checkpoint = world.rpc("checkpoint.status")
                return checkpoint.get("status") == "completed" and checkpoint["result"].get("load_verified")
            except OSError:
                return False

        self.wait(ready, timeout=30)

    def close(self):
        if self.service is not None:
            self.service.close()
        if self.store is not None:
            self.store.close()
        for client in self.clients:
            if client.process.poll() is None:
                client.process.terminate()
                try:
                    client.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    client.process.kill()
                    client.process.wait(timeout=3)
            client.log.close()
        self.world.close()

    def pump(self):
        if self.service is not None:
            self.service._process_world(self.world.world_id, self.store.pending_requests())

    def wait(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            value = predicate()
            if value:
                return value
            time.sleep(0.08)
        self.fail(f"Timeout during {self.stage}; artifacts: {self.world.directory}\n{self.world.log_tail()}")

    def native(self, name, company, *, pristine=False, script=""):
        directory = self.world.directory / name
        data, config = directory / "data", directory / "config"
        scripts = data / "openttd/scripts"
        scripts.mkdir(parents=True)
        (config / "openttd").mkdir(parents=True)
        (data / "openttd/baseset").symlink_to(self.baseset, target_is_directory=True)
        (scripts / "on_client.scr").write_text(
            "schedule on-next-calendar-month scripts/run.scr\n" if script else "")
        (scripts / "run.scr").write_text(script)
        (config / "openttd/openttd.cfg").write_text(
            f"[network]\nclient_name = {name}\n[gui]\nshow_finances = false\n")
        binary = self.pristine if pristine else self.binary
        driver = "null:ticks=100000000" if pristine else "framebuffer"
        log = (directory / "client.log").open("wb", buffering=0)
        process = subprocess.Popen(
            [str(binary), "-n", f"127.0.0.1:{self.world.port}#{company + 1}",
             "-x", "-I", "OpenGFX", "-snull", "-mnull", f"-v{driver}",
             "-d", "net=9,desync=1"],
            cwd=directory, env=self.environment(data, config), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        client = NativeClient(name, directory, process, log)
        self.clients.append(client)
        return client

    def roster(self):
        return self.world.completed(self.world.rpc("chat.roster"))

    def thread(self, company, scope, companies=None):
        payload = {"scope": scope}
        if companies is not None:
            payload["company_ids"] = companies
        return self.service.handle("POST", "/v1/threads", self.tokens[company], payload)[1]["thread_id"]

    def history(self, company, thread):
        return self.service.handle("GET", f"/v1/threads/{thread}/messages", self.tokens[company])[1]["messages"]

    def send(self, thread, body):
        return self.service.handle("POST", f"/v1/threads/{thread}/messages", self.tokens[0],
                                   {"request_id": uuid.uuid4().hex, "body": body})[1]

    def assert_unavailable(self, company, thread):
        with self.assertRaises(ServiceError) as caught:
            self.history(company, thread)
        self.assertEqual(caught.exception.code, "thread_unavailable")

    def assert_native_visibility(self, expected, forbidden):
        self.wait(lambda: all(marker in client.received()
                              for client, markers in expected for marker in markers))
        for client, markers in expected:
            for marker in markers:
                self.assertEqual(client.received().count(marker), 1, (client.name, marker))
        for client, markers in forbidden:
            for marker in markers:
                self.assertNotIn(marker, client.received(), (client.name, marker))

    def test_matching_humans_private_chat_and_pristine_rejection(self):
        alpha = self.native("NativeAlpha", 0, script=(
            'say "NATIVE_PUBLIC_ACCEPTANCE"\n'
            'say_company 1 "NATIVE_COMPANY_ZERO_PRIVATE"\n'
            'say_company 2 "NATIVE_COMPANY_DIRECT_01"\n'
            'say_client 3 "NATIVE_IDENTITY_ALPHA_BETA"\n'))
        self.wait(lambda: any(x["label"] == alpha.name and x["company_id"] == 0 for x in self.roster()))
        beta = self.native("NativeBeta", 1)
        self.wait(lambda: any(x["label"] == beta.name and x["company_id"] == 1 for x in self.roster()))
        gamma = self.native("NativeGamma", 2)
        self.wait(lambda: any(x["label"] == gamma.name and x["company_id"] == 2 for x in self.roster()))
        roster = self.roster()
        # Message recipients are an admission snapshot. Refresh backend facts
        # after the last join before admitting messages intended for that user.
        self.pump()
        self.stage = "agent-to-native private delivery"
        public = self.thread(0, "world")
        company = self.thread(0, "company")
        direct = self.thread(0, "direct_company", [0, 1])
        outbound = [self.send(thread, body) for thread, body in (
            (public, "AGENT_PUBLIC_ACCEPTANCE"),
            (company, "AGENT_COMPANY_ZERO_PRIVATE"),
            (direct, "AGENT_COMPANY_DIRECT_01"),
        )]
        expected = [(alpha, ["AGENT_PUBLIC_ACCEPTANCE", "AGENT_COMPANY_ZERO_PRIVATE", "AGENT_COMPANY_DIRECT_01"]),
                    (beta, ["AGENT_PUBLIC_ACCEPTANCE", "AGENT_COMPANY_DIRECT_01"]),
                    (gamma, ["AGENT_PUBLIC_ACCEPTANCE"])]
        forbidden = [(beta, ["AGENT_COMPANY_ZERO_PRIVATE"]),
                     (gamma, ["AGENT_COMPANY_ZERO_PRIVATE", "AGENT_COMPANY_DIRECT_01"])]
        self.assert_native_visibility(expected, forbidden)

        # Repeat the real private mutation with its permanent sequence/identity.
        # The root ledger must return the receipt without another native send.
        record = self.store.get_internal(self.world.world_id, "chat:" + outbound[0]["message_id"])
        self.assertEqual(record["status"], "completed", record)
        repeated = self.world.rpc("chat.publish", request_id=record["request_id"],
                                  request_sequence=record["request_sequence"], message=record["payload"]["delivery"])
        self.assertEqual(repeated["status"], "completed", repeated)
        self.assert_native_visibility(expected, forbidden)

        self.stage = "ordinary scheduled native-client ingress"
        self.wait(lambda: self.store.connection.execute(
            "SELECT 1 FROM social_messages WHERE body=?", ("NATIVE_IDENTITY_ALPHA_BETA",)).fetchone(), timeout=90)
        self.assert_native_visibility(
            [(alpha, ["NATIVE_PUBLIC_ACCEPTANCE", "NATIVE_COMPANY_ZERO_PRIVATE", "NATIVE_COMPANY_DIRECT_01", "NATIVE_IDENTITY_ALPHA_BETA"]),
             (beta, ["NATIVE_PUBLIC_ACCEPTANCE", "NATIVE_COMPANY_DIRECT_01", "NATIVE_IDENTITY_ALPHA_BETA"]),
             (gamma, ["NATIVE_PUBLIC_ACCEPTANCE"])],
            [(beta, ["NATIVE_COMPANY_ZERO_PRIVATE"]),
             (gamma, ["NATIVE_COMPANY_ZERO_PRIVATE", "NATIVE_COMPANY_DIRECT_01", "NATIVE_IDENTITY_ALPHA_BETA"])])
        incoming = next(message for message in self.history(0, public)
                        if message["body"] == "NATIVE_PUBLIC_ACCEPTANCE")
        self.assertEqual(incoming["sender"]["kind"], "human")
        self.assertEqual(incoming["sender"]["company_id"], 0)
        self.assertEqual(incoming["sender"]["display_name"], alpha.name)
        self.assertTrue(incoming["tick_authoritative"])
        self.assertIn("NATIVE_COMPANY_ZERO_PRIVATE", [m["body"] for m in self.history(0, company)])
        self.assertIn("NATIVE_COMPANY_DIRECT_01", [m["body"] for m in self.history(1, direct)])
        self.assert_unavailable(1, company)
        self.assert_unavailable(2, direct)
        identity_thread = self.store.connection.execute(
            "SELECT thread_id FROM social_messages WHERE body=?", ("NATIVE_IDENTITY_ALPHA_BETA",)).fetchone()[0]
        for observer in self.tokens:
            self.assert_unavailable(observer, identity_thread)

        self.stage = "pristine native revision rejection"
        pristine = self.native("PristineUpstream", 0, pristine=True)
        self.wait(lambda: "Client::ReceiveServerError(): error=8" in pristine.diagnostic())
        self.assertFalse(any(x["label"] == pristine.name for x in self.roster()))
        revisions = {
            client.name: re.findall(r"Network revision name: (\S+)", client.diagnostic())
            for client in (alpha, pristine)
        }
        self.assertNotEqual(revisions[alpha.name], revisions[pristine.name])
        evidence = {"matching_native_company_join": True, "agent_native_scope_privacy": True,
                    "native_agent_scope_privacy": True, "native_identity_direct_privacy": True,
                    "delivery_retry_once": True, "pristine_rejected_error": 8,
                    "revisions": revisions, "roster": roster}
        (self.world.directory / "native-chat-result.json").write_text(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    unittest.main()
