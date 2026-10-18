"""Opt-in live OpenTTD fixture runner using only the private operator RPC.

The game owns all costs, ownership decisions, object allocation and saves. This
module starts ordinary dedicated servers; it provides no state mutation shortcut.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
MAX_FRAME = 65536
TERMINAL = {"completed", "rejected", "uncertain", "retired", "resolved_rolled_back"}


class RPCError(AssertionError):
    """A live engine rejected an operation required by the fixture."""


@dataclass(frozen=True)
class Action:
    sequence: int
    request_id: str
    actor: dict
    payload: dict

    def fields(self):
        return {
            "request_sequence": self.sequence,
            "request_id": self.request_id,
            "actor": self.actor,
            "action": self.payload,
        }


class LiveWorld:
    """One isolated loopback server, private checkpoint directory and RPC client."""

    def __init__(self, binary, baseset, *, port=13978, fixture=None, generate=False):
        self.binary = Path(binary).resolve()
        self.baseset = Path(baseset).resolve()
        self.fixture = Path(fixture or ROOT / "regression/regression/test.sav").resolve()
        self.generate = generate
        self.directory = Path(tempfile.mkdtemp(prefix="ottd-live-"))
        self.directory.chmod(0o700)
        self.state = self.directory / "state"
        self.ipc = self.directory / "ipc"
        self.data = self.directory / "data"
        self.config = self.directory / "config"
        for path in (self.state, self.ipc, self.data / "openttd", self.config / "openttd"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        (self.data / "openttd/baseset").symlink_to(self.baseset, target_is_directory=True)
        self.socket_path = self.ipc / "world.sock"
        self.world_id = "live-test-" + uuid.uuid4().hex
        self.epoch = "initial"
        self.instance = None
        self.actor = None
        self.sequence = 0
        self.process = None
        self.log = None
        self.port = port
        self.checkpoint = None
        self.accepted = []
        self.config_file = self.config / "openttd/openttd.cfg"
        self.config_file.write_text(
            "[network]\n"
            "server_name = Private live integration test\n"
            "server_game_type = local\n"
            "pause_on_join = false\n"
            "min_active_clients = 0\n"
            "frame_freq = 0\n"
            "max_companies = 15\n"
            "[gui]\n"
            "pause_on_newgame = false\n"
            "autosave = off\n"
            "[game_creation]\n"
            "map_x = 6\n"
            "map_y = 6\n"
            "[misc]\n"
            "language = english.lng\n"
            "savegame_format = lzma:2\n",
            encoding="utf-8",
        )

    def __enter__(self):
        try:
            self.start()
            self.finish_recovery()
            self.actor = self.bind(0)
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def log_tail(self):
        path = self.directory / "engine.log"
        return path.read_text(encoding="utf-8", errors="replace")[-6000:] if path.exists() else ""

    def start(self, save=None):
        if self.process is not None:
            raise RuntimeError("World already running")
        environment = dict(os.environ)
        environment.update({
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "OPENTTD_INTEGRATION_SOCKET": str(self.socket_path),
            "OPENTTD_INTEGRATION_STATE_DIR": str(self.state),
            "OPENTTD_INTEGRATION_WORLD_ID": self.world_id,
            "OPENTTD_INTEGRATION_WORLD_EPOCH": self.epoch,
        })
        environment.pop("OPENTTD_INTEGRATION_ROLE", None)
        if os.environ.get("OPENTTD_TEST_LIBRARY_PATH"):
            environment["LD_LIBRARY_PATH"] = os.environ["OPENTTD_TEST_LIBRARY_PATH"] + os.pathsep + environment.get("LD_LIBRARY_PATH", "")
        self.log = (self.directory / "engine.log").open("ab", buffering=0)
        command = [str(self.binary), "-D", f"127.0.0.1:{self.port}", "-g"]
        if save or not self.generate:
            command.append(str(save or self.fixture))
        else:
            command.extend(["-G", "1"])
        command.extend(["-x", "-I", "OpenGFX", "-d", "net=1,misc=2"])
        self.process = subprocess.Popen(
            command,
            cwd=self.directory, env=environment,
            stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RPCError(f"Engine exited {self.process.returncode}:\n{self.log_tail()}")
            try:
                hello = self.rpc("hello")
                self.instance = hello["engine_instance_id"]
                status = self.rpc("checkpoint.status")
                if status.get("status") == "completed" and status["result"].get("load_verified"):
                    # Loading a save may preserve an ordinary player's pause flag.
                    self.console("unpause")
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.03)
        raise RPCError(f"Engine did not become ready:\n{self.log_tail()}")

    def stop(self):
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    self.console("quit")
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
        finally:
            if process.stdin:
                process.stdin.close()
            if self.log:
                self.log.close()
            self.process = None
            self.log = None

    def close(self):
        self.stop()
        if os.environ.get("OPENTTD_TEST_KEEP") == "1":
            print(f"Live engine artifacts: {self.directory}", flush=True)
        else:
            shutil.rmtree(self.directory, ignore_errors=True)

    def console(self, command):
        """Ordinary operator console for lifecycle/configuration, never test cheats."""
        if self.process and self.process.poll() is None and self.process.stdin:
            self.process.stdin.write((command + "\n").encode("utf-8"))
            self.process.stdin.flush()

    def rpc(self, kind, **fields):
        correlation = uuid.uuid4().hex
        request = {
            "protocol_version": 1, "type": kind, "correlation_id": correlation,
            "world_id": self.world_id, "world_epoch": self.epoch,
        }
        if kind != "hello":
            request["engine_instance_id"] = self.instance
        request.update(fields)
        data = json.dumps(request, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME:
            raise ValueError("Test request exceeds the private frame limit")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(4)
            connection.connect(str(self.socket_path))
            connection.sendall(data)
            response = bytearray()
            while b"\n" not in response:
                chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(response)))
                if not chunk:
                    raise RPCError(f"Truncated {kind} reply; engine log:\n{self.log_tail()}")
                response.extend(chunk)
                if len(response) > MAX_FRAME:
                    raise RPCError(f"Oversized {kind} reply")
        line, trailing = response.split(b"\n", 1)
        if trailing:
            raise RPCError("Unexpected extra private RPC frame")
        reply = json.loads(line)
        if reply.get("protocol_version") != 1 or reply.get("correlation_id") != correlation:
            raise RPCError("Reply correlation or protocol mismatch")
        if reply.get("world_id") != self.world_id or reply.get("world_epoch") != self.epoch:
            raise RPCError("Reply escaped the test world fence")
        if kind != "hello" and reply.get("engine_instance_id") != self.instance:
            raise RPCError("Reply escaped the engine instance fence")
        return reply

    @staticmethod
    def completed(reply):
        if reply.get("status") != "completed":
            raise RPCError(json.dumps(reply, sort_keys=True))
        return reply["result"]

    def finish_recovery(self, actions=()):
        for action in actions:
            reply = self.rpc("recovery.reconcile", **action.fields())
            result = reply.get("result") or {}
            if reply.get("status") not in TERMINAL or (reply["status"] != "retired" and (
                result.get("request_sequence") != action.sequence or result.get("request_id") != action.request_id
            )):
                raise RPCError(f"Recovery did not reconcile the accepted identity: {reply}")
        self.completed(self.rpc("recovery.finish"))

    def bind(self, company, *, identity=None, mode="text", create=True):
        deadline = time.monotonic() + 5
        while True:
            ensured = self.rpc("company.ensure", company_id=company, create=create)
            if ensured["status"] == "completed":
                break
            if ensured["status"] != "queued" or time.monotonic() >= deadline:
                raise RPCError(f"company.ensure: {ensured}")
            time.sleep(0.03)
        actor = {"identity_id": identity or f"test-company-{company}", "run_id": uuid.uuid4().hex, "company_id": company, "mode": mode}
        self.completed(self.rpc("session.bind", actor=actor))
        return actor

    def observe(self, name, parameters=None, *, actor=None):
        return self.completed(self.rpc("observation.query", actor=actor or self.actor, query={"name": name, "parameters": parameters or {}}))

    def enqueue(self, name, parameters=None, *, actor=None, request_id=None):
        self.sequence += 1
        action = Action(self.sequence, request_id or uuid.uuid4().hex, copy.deepcopy(actor or self.actor), {"name": name, "parameters": copy.deepcopy(parameters or {})})
        self.accepted.append(action)
        return action, self.rpc("action.submit", **action.fields())

    def wait(self, action, initial=None, *, timeout=8):
        reply = initial
        deadline = time.monotonic() + timeout
        while reply is None or reply.get("status") not in TERMINAL:
            if time.monotonic() >= deadline:
                raise RPCError(f"Action {action.payload} never completed: {reply}\n{self.log_tail()}")
            time.sleep(0.02)
            reply = self.rpc("action.status", **action.fields())
        return reply

    def action(self, name, parameters=None, *, actor=None):
        action, initial = self.enqueue(name, parameters, actor=actor)
        return self.wait(action, initial)

    def checkpoint_now(self):
        requested = self.rpc("checkpoint.create")
        if requested["status"] not in {"queued", "completed"}:
            raise RPCError(f"Checkpoint rejected: {requested}")
        checkpoint_id = requested["result"]["checkpoint_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            info = self.completed(self.rpc("checkpoint.status"))
            if info.get("checkpoint_id") == checkpoint_id and info["status"] == "completed":
                self.checkpoint = info
                return info
            if info.get("status") == "failed":
                raise RPCError(f"Checkpoint failed: {info}")
            time.sleep(0.03)
        raise RPCError("Checkpoint did not complete")

    def restart_checkpoint(self):
        if not self.checkpoint:
            raise RuntimeError("A completed checkpoint is required")
        previous = self.instance
        self.stop()
        self.start(self.checkpoint["save_path"])
        if self.instance == previous:
            raise RPCError("Restart did not change the engine boot identity")
        self.finish_recovery(self.accepted)
