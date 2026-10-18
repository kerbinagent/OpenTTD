#!/usr/bin/env python3
"""Run one private local mixed or four-player Luna world; preserve it on exit.

This launcher owns process lifetimes and operator company provisioning only.
The backend owns request sequencing, checkpoint scheduling and recovery.
"""

from __future__ import annotations

import argparse
import configparser
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.bridge import UnixEngine
from backend.errors import ServiceError
from backend.store import Store


KEY = re.compile(r"[0-9a-fA-F]{64}\Z")


def profile_bindings(profile):
    """Two fixed role layouts; each invocation receives its own maps."""
    if profile == "mixed":
        return {
            "human": {"identity_id": "local-human", "company_id": 0, "mode": "visual", "kind": "human", "display_name": "Human viewer"},
            "text-agent": {"identity_id": "local-text-agent", "company_id": 1, "mode": "text", "kind": "agent", "display_name": "Text agent"},
            "visual-agent": {"identity_id": "local-visual-agent", "company_id": 2, "mode": "visual", "kind": "agent", "display_name": "Visual agent"},
        }, {"human": "r0", "visual-agent": "r2"}
    if profile == "luna":
        return {
            "text-cooperator": {"identity_id": "luna-text-cooperator", "company_id": 0, "mode": "text", "kind": "agent", "display_name": "Luna text cooperator"},
            "visual-cooperator": {"identity_id": "luna-visual-cooperator", "company_id": 1, "mode": "visual", "kind": "agent", "display_name": "Luna visual cooperator"},
            "text-competitor": {"identity_id": "luna-text-competitor", "company_id": 2, "mode": "text", "kind": "agent", "display_name": "Luna text competitor"},
            "visual-competitor": {"identity_id": "luna-visual-competitor", "company_id": 3, "mode": "visual", "kind": "agent", "display_name": "Luna visual competitor"},
        }, {"visual-cooperator": "r1", "visual-competitor": "r3"}
    raise StartupError("Unknown fixed launcher profile")


class StartupError(Exception):
    pass


def private_directory(path: Path, *, create=False):
    if create:
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise StartupError(f"Private directory must be owned by you with mode 0700: {path}")


def check_private_file(info, path, maximum):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or not 0 < info.st_size <= maximum):
        raise StartupError(f"Private file must be owned by you with mode 0600: {path}")


def private_file(path: Path, *, maximum=1024 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        check_private_file(info, path, maximum)
        value = source.read(maximum + 1)
        if len(value) > maximum:
            raise StartupError(f"Private file is too large: {path}")
        return value


def write_new(path: Path, text: str):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())


def publish_manifest(state: Path, manifest):
    temporary = state / "manifest.json.new"
    write_new(temporary, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, state / "manifest.json")
    descriptor = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def process_paths(state: Path, name: str):
    directory = state / name
    return directory, directory / "config", directory / "data"


def prepare_process(state: Path, name: str, baseset: Path, size: int, *, label="", renderer=False, luna=False):
    directory, config, data = process_paths(state, name)
    for path in (directory, config, data, config / "openttd", data / "openttd"):
        private_directory(path, create=True)
    (data / "openttd" / "baseset").symlink_to(baseset, target_is_directory=True)
    # Version 8 keeps the separately written private/secrets settings authoritative.
    write_new(config / "openttd" / "openttd.cfg",
              "[version]\nini_version = 8\n"
              "[network]\nserver_game_type = local\npause_on_join = false\n"
              "min_active_clients = 0\nmax_companies = 15\n"
              + ("max_lag_time = 2000\n" if luna else "")
              + "[gui]\npause_on_newgame = false\nautosave = off\n"
              "[misc]\nlanguage = english.lng\nsavegame_format = lzma:2\n"
              f"[game_creation]\nmap_x = {int(math.log2(size))}\nmap_y = {int(math.log2(size))}\n"
              + ("starting_year = 1950\nlandscape = temperate\n" if luna else ""))
    write_new(config / "openttd" / "private.cfg",
              "[version]\nini_version = 8\n[network]\n"
              f"client_name = {label}\nserver_name = Local mixed world\n")
    if renderer:
        write_new(config / "openttd" / "secrets.cfg",
                  "[version]\nini_version = 8\n[network]\n"
                  f"client_secret_key = {secrets.token_hex(32)}\n")
        private_directory(directory / "images", create=True)


def provision(state: Path, args):
    for name in ("ipc", "logs", "credentials", "engine-state"):
        private_directory(state / name, create=True)
    prepare_process(state, "engine", args.baseset, args.map_size, luna=args.profile == "luna")
    for role, worker in args.workers.items():
        prepare_process(state, worker, args.baseset, args.map_size,
                        label=f"mixed-{worker}", renderer=True)
    manifest = {"version": 1, "profile": args.profile, "world_id": args.world, "world_epoch": uuid.uuid4().hex,
                "ready": False, "runs": {}, "renderer_keys": {}}
    if args.profile == "luna":
        manifest["generation"] = {"map_size": args.map_size, "seed": args.seed,
                                  "starting_year": 1950, "landscape": "temperate"}
    store = Store(state / "world.sqlite3", world_id=args.world)
    try:
        for role, binding in args.roles.items():
            token = store.provision(args.world, **binding, world_epoch=manifest["world_epoch"])
            principal = store.authenticate(token)
            write_new(state / "credentials" / f"{role}.token", token + "\n")
            manifest["runs"][role] = {"run_id": principal.run_id}
    finally:
        store.close()
    publish_manifest(state, manifest)
    return manifest


def load_existing(state: Path, args):
    try:
        manifest = json.loads(private_file(state / "manifest.json"))
        # Version-1 manifests written before profiles always describe mixed.
        if manifest.get("profile", "mixed") != args.profile:
            raise StartupError("Existing world uses a different launcher profile")
        if (not isinstance(manifest, dict) or manifest.get("version") != 1
                or manifest.get("world_id") != args.world or not manifest.get("world_epoch")
                or manifest.get("ready") is not True or set(manifest.get("runs", {})) != set(args.roles)
                or set(manifest.get("renderer_keys", {})) != set(args.workers)):
            raise StartupError("Existing launcher manifest is incomplete or belongs to another world")
        check_private_file((state / "world.sqlite3").lstat(), state / "world.sqlite3", 1024 * 1024 * 1024)
        for name in ("ipc", "logs", "credentials", "engine-state"):
            private_directory(state / name)
        for name in ("engine", *args.workers.values()):
            directory, config, data = process_paths(state, name)
            for path in (directory, config, data, config / "openttd", data / "openttd"):
                private_directory(path)
            for file in ("openttd.cfg", "private.cfg"):
                private_file(config / "openttd" / file)
            link = data / "openttd" / "baseset"
            if not link.is_symlink() or link.resolve() != args.baseset:
                raise StartupError("Existing process base-set binding differs from --baseset")
        for role, worker in args.workers.items():
            _, config, _ = process_paths(state, worker)
            settings = configparser.ConfigParser(interpolation=None)
            settings.read_string(private_file(config / "openttd" / "secrets.cfg").decode("utf-8"))
            if not KEY.fullmatch(settings.get("network", "client_secret_key", fallback="")):
                raise StartupError("Existing renderer secret key is invalid")
            private_directory(state / worker / "images")
            if not isinstance(manifest["renderer_keys"][role], str) or not KEY.fullmatch(manifest["renderer_keys"][role]):
                raise StartupError("Existing renderer public key is invalid")
        # The launcher selects the recorded save; native code validates its sidecar/checksum.
        ledger = json.loads(private_file(state / "engine-state" / "ledger.json", maximum=64 * 1024 * 1024))
        journal = ledger["journal"]
        if journal["world_id"] != args.world or journal["world_epoch"] != manifest["world_epoch"]:
            raise StartupError("Existing native checkpoint belongs to another world")
        save = Path(journal["checkpoint"]["save_path"])
        if (not save.is_absolute() or save.resolve() != save or save.parent != state / "engine-state"
                or not save.is_file() or not journal["checkpoint"]["checkpoint_id"]):
            raise StartupError("Existing checkpoint save is missing or outside this world's engine state")
        store = Store(state / "world.sqlite3", world_id=args.world)
        try:
            if store.world_epoch(args.world) != manifest["world_epoch"]:
                raise StartupError("Existing database epoch differs from the native world")
            for role, binding in args.roles.items():
                token = private_file(state / "credentials" / f"{role}.token", maximum=1024).decode("utf-8").strip()
                principal = store.authenticate(token)
                expected = manifest["runs"][role]
                if (any(getattr(principal, key) != value for key, value in binding.items() if key != "display_name")
                        or principal.run_id != expected["run_id"] or principal.world_id != args.world):
                    raise StartupError("Existing credential/run binding differs from the launcher manifest")
        finally:
            store.close()
        return manifest, save
    except StartupError:
        raise
    except Exception as exc:
        raise StartupError("Existing state is incomplete or corrupt; no world or credential was reset") from exc


class Stack:
    def __init__(self, state: Path, args, manifest):
        self.state, self.args, self.manifest = state, args, manifest
        self.roles = {role: dict(binding) for role, binding in args.roles.items()}
        self.workers = dict(args.workers)
        self.children = {}
        self.instance = None
        self.rpc_client = UnixEngine(state / "ipc" / "engine.sock", timeout=2)
        self.stop_requested = False

    def environment(self, name, *, renderer=False):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("OPENTTD_INTEGRATION_") and key != "OPENTTD_RENDERER_IMAGE_DIR"}
        directory, config, data = process_paths(self.state, name)
        environment.update({"XDG_CONFIG_HOME": str(config), "XDG_DATA_HOME": str(data),
                            "OPENTTD_INTEGRATION_WORLD_ID": self.args.world,
                            "OPENTTD_INTEGRATION_WORLD_EPOCH": self.manifest["world_epoch"],
                            "OPENTTD_INTEGRATION_STATE_DIR": str(self.state / "engine-state" if not renderer else directory),
                            "OPENTTD_INTEGRATION_SOCKET": str(self.state / "ipc" / ("engine.sock" if not renderer else name + ".sock"))})
        if renderer:
            environment["OPENTTD_INTEGRATION_ROLE"] = "renderer"
            environment["OPENTTD_RENDERER_IMAGE_DIR"] = str(directory / "images")
            if self.args.profile == "luna":
                environment["OPENTTD_RENDERER_PARK_MS"] = "40000"
        return environment

    def spawn(self, name, command, *, environment=None, cwd=None, console=False):
        log_path = self.state / "logs" / f"{name}.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(descriptor)
            raise StartupError(f"Unsafe process log: {log_path}")
        with os.fdopen(descriptor, "ab", buffering=0) as log:
            child = subprocess.Popen(command, cwd=cwd or ROOT, env=environment,
                                     stdin=subprocess.PIPE if console else subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.children[name] = child
        return child

    def check_critical(self):
        if self.stop_requested:
            raise KeyboardInterrupt
        for name in ("engine", "backend"):
            child = self.children.get(name)
            if child is not None and child.poll() is not None:
                raise StartupError(f"{name} exited ({child.returncode}); inspect {self.state / 'logs' / (name + '.log')}")

    def rpc(self, kind, **fields):
        message = {"protocol_version": 1, "type": kind, "correlation_id": uuid.uuid4().hex,
                   "world_id": self.args.world, "world_epoch": self.manifest["world_epoch"], **fields}
        if kind != "hello":
            message["engine_instance_id"] = self.instance
        reply = self.rpc_client.call(message)
        if (reply.get("world_id") != self.args.world or reply.get("world_epoch") != self.manifest["world_epoch"]
                or (kind != "hello" and reply.get("engine_instance_id") != self.instance)):
            raise StartupError("Private RPC reply violated the world/engine fence")
        return reply

    def wait(self, description, operation, *, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_critical()
            result = operation()
            if result:
                return result
            time.sleep(0.1)
        raise StartupError(f"Timed out waiting for {description}; inspect the private process logs")

    def start(self, save=None):
        print("Starting native world…", flush=True)
        command = [str(self.args.binary), "-D", f"127.0.0.1:{self.args.native_port}", "-g"]
        if save:
            command.append(str(save))
        elif self.args.initial_save:
            command.append(str(self.args.initial_save))
        elif self.args.seed is not None:
            command += ["-G", str(self.args.seed)]
        command += ["-c", str(process_paths(self.state, "engine")[1] / "openttd" / "openttd.cfg"),
                    "-X", "-x", "-I", self.args.graphics, "-d", "net=1,misc=3"]
        self.spawn("engine", command, environment=self.environment("engine"), cwd=self.state / "engine", console=True)

        def native_ready():
            try:
                hello = self.rpc("hello")
                if hello.get("status") != "completed" or hello.get("result", {}).get("role") != "world":
                    return False
                self.instance = hello["engine_instance_id"]
                state = self.rpc("checkpoint.status")
                return state.get("status") == "completed" and state.get("result", {}).get("load_verified")
            except (OSError, ValueError, KeyError, ServiceError):
                return False

        self.wait("native world and verified load", native_ready, timeout=60)
        # Loaded games can carry an ordinary pause flag; this is an operator console action.
        engine = self.children["engine"]
        engine.stdin.write(b"unpause\n")
        engine.stdin.flush()
        config = {"version": 1, "world_id": self.args.world, "runs": {
            self.manifest["runs"][role]["run_id"]: {
                "socket": str(self.state / "ipc" / f"{worker}.sock"),
                "artifacts": str(self.state / worker / "images")}
            for role, worker in self.workers.items()}}
        renderer_config = self.state / "renderer-config.json"
        if renderer_config.exists():
            if json.loads(private_file(renderer_config)) != config:
                raise StartupError("Existing renderer map does not match the stable run bindings")
        else:
            write_new(renderer_config, json.dumps(config, indent=2) + "\n")
        self.spawn("backend", [sys.executable, "-m", "backend", "serve", "--database", str(self.state / "world.sqlite3"),
                               "--world", self.args.world, "--engine-socket", str(self.state / "ipc" / "engine.sock"),
                               "--renderer-config", str(renderer_config), "--transport", self.args.transport,
                               "--host", "127.0.0.1", "--port", str(self.args.http_port)], environment=self.environment("engine"))
        self.wait("backend-owned recovery", lambda: not self.rpc("checkpoint.status").get("result", {}).get("recovery_required", True))
        for company in sorted(binding["company_id"] for binding in self.roles.values()):
            self.wait(f"company {company + 1}", lambda company=company: self.rpc("company.ensure", company_id=company, create=save is None).get("status") == "completed")
        # Native-created companies start closed. A fresh renderer must authenticate
        # before its persistent key can be granted; restarts keep that closed grant.
        initial_policies = {
            binding["company_id"]: {"allow_any": binding["kind"] == "human" or role in self.workers}
            for role, binding in self.roles.items()
            if save is None or role not in self.workers or binding["kind"] == "human"}
        for company, policy in initial_policies.items():
            self.wait(f"company {company + 1} initial join policy",
                      lambda company=company, policy=policy: self.rpc("company.access", company_id=company, **policy).get("status") == "completed")
        for role, worker in self.workers.items():
            company = self.roles[role]["company_id"]
            self.spawn(worker, [str(self.args.binary), "-v", "framebuffer", "-s", "null", "-m", "null", "-r", "1280x720",
                                "-n", f"127.0.0.1:{self.args.native_port}#{company + 1}",
                                "-c", str(process_paths(self.state, worker)[1] / "openttd" / "openttd.cfg"),
                                "-X", "-x", "-I", self.args.graphics, "-d", "net=1,misc=3"],
                       environment=self.environment(worker, renderer=True), cwd=self.state / worker)

            def worker_key(role=role, worker=worker, company=company):
                child = self.children[worker]
                if child.poll() is not None:
                    raise StartupError(f"Renderer {worker} exited during startup; inspect its private log")
                # The private worker socket proves which native key belongs to
                # our child. A nickname in the public native roster is no proof.
                hello_request = {"protocol_version": 1, "type": "hello", "correlation_id": uuid.uuid4().hex,
                                 "world_id": self.args.world, "world_epoch": self.manifest["world_epoch"]}
                try:
                    hello = UnixEngine(self.state / "ipc" / f"{worker}.sock", timeout=2).call(hello_request)
                except ServiceError:
                    return False
                if (hello.get("world_id") != self.args.world or hello.get("world_epoch") != self.manifest["world_epoch"]
                        or hello.get("result", {}).get("role") != "renderer"):
                    raise StartupError("Renderer private handshake violated its world/role binding")
                worker_public_key = hello.get("result", {}).get("native_public_key", "").lower()
                if not KEY.fullmatch(worker_public_key):
                    return False
                reply = self.rpc("roster")
                people = reply.get("result", [])
                matches = [person for person in people if person.get("public_key", "").lower() == worker_public_key and person.get("company_id") == company]
                if len(matches) > 1:
                    raise StartupError("Renderer native identity is ambiguous")
                if not matches:
                    return False
                key = worker_public_key
                prior = self.manifest["renderer_keys"].get(role)
                if prior and prior != key:
                    raise StartupError("Renderer key differs from the existing world binding")
                return key

            self.manifest["renderer_keys"][role] = self.wait(f"authenticated renderer {worker}", worker_key)
        policies = {}
        for role, binding in self.roles.items():
            policy = {"allow_any": binding["kind"] == "human"}
            if role in self.workers and binding["kind"] != "human":
                policy["add_keys"] = [self.manifest["renderer_keys"][role]]
            policies[binding["company_id"]] = policy
        for company, policy in policies.items():
            self.wait(f"company {company + 1} native access policy",
                      lambda company=company, policy=policy: self.rpc("company.access", company_id=company, **policy).get("status") == "completed")
        baseline = self.rpc("checkpoint.status").get("result", {}).get("checkpoint_id")
        print("Waiting for the backend's periodic durable checkpoint…", flush=True)
        def checkpoint_ready():
            state = self.rpc("checkpoint.status").get("result", {})
            return state.get("status") == "completed" and state.get("checkpoint_id") and state.get("checkpoint_id") != baseline
        self.wait("backend-owned checkpoint after provisioning", checkpoint_ready, timeout=50)
        self.manifest["ready"] = True
        publish_manifest(self.state, self.manifest)

    def serve(self):
        print(f"\nWorld ready: http://127.0.0.1:{self.args.http_port}/", flush=True)
        print("Session tokens are in private files; their contents are never printed:")
        for role in self.roles:
            print(f"  {role}: {self.state / 'credentials' / (role + '.token')}")
        if self.args.profile == "mixed":
            human = ["env", f"XDG_DATA_HOME={self.state / 'engine' / 'data'}"]
            if os.environ.get("LD_LIBRARY_PATH"):
                human.append(f"LD_LIBRARY_PATH={os.environ['LD_LIBRARY_PATH']}")
            human += [str(self.args.binary), "-n", f"127.0.0.1:{self.args.native_port}#1", "-I", self.args.graphics]
            print("Matching native human client: " + shlex.join(human))
            print("Company 1: human. Company 2: text agent (native joins closed). Company 3: visual agent.")
        else:
            print("Luna companies: text 0 / visual 1 cooperate; text 2 / visual 3 compete.")
            print("Native companies are closed to other clients; visual companies admit their authenticated workers.")
        print("Ctrl+C stops this group and preserves state. Restart with the same command.", flush=True)
        reported = set()
        while not self.stop_requested:
            self.check_critical()
            for worker in self.workers.values():
                child = self.children[worker]
                if child.poll() is not None and worker not in reported:
                    reported.add(worker)
                    print(f"Renderer {worker} is offline; world and chat remain running. Inspect {self.state / 'logs' / (worker + '.log')}", file=sys.stderr, flush=True)
            time.sleep(0.25)

    def close(self):
        # Stop admission first. The last completed backend checkpoint is the restart boundary.
        for name in ("backend", *self.workers.values(), "engine"):
            child = self.children.get(name)
            if child is None:
                continue
            if child.poll() is None:
                try:
                    if name == "engine" and child.stdin:
                        child.stdin.write(b"quit\n"); child.stdin.flush()
                    else:
                        os.killpg(child.pid, signal.SIGINT if name == "backend" else signal.SIGTERM)
                    child.wait(timeout=8 if name == "backend" else 5)
                except (BrokenPipeError, ProcessLookupError):
                    pass
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=3)
            if child.stdin:
                try:
                    child.stdin.close()
                except BrokenPipeError:
                    pass


def port(value):
    number = int(value)
    if not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return number


def generation_seed(value):
    number = int(value)
    if not 0 <= number < 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("seed must be between 0 and 4294967294")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True, help="matching integration-enabled OpenTTD binary")
    parser.add_argument("--baseset", type=Path, required=True, help="directory containing the installed base graphics")
    parser.add_argument("--state-dir", type=Path, required=True, help="new empty or existing private world directory")
    parser.add_argument("--world", required=True, help="stable local world ID")
    parser.add_argument("--profile", choices=("mixed", "luna"), default="mixed",
                        help="fixed human/text/visual layout (default) or two text and two visual Luna companies")
    parser.add_argument("--native-port", type=port, default=3979)
    parser.add_argument("--http-port", type=port, default=8080)
    parser.add_argument("--transport", choices=("asgi", "http"), default="asgi", help="ASGI (default) or explicit local-development HTTP fallback")
    parser.add_argument("--graphics", default="OpenGFX", help="installed base graphics name")
    parser.add_argument("--map-size", type=int, choices=(128, 256, 512, 1024),
                        help="new square map size; default 512 for mixed, 128 for luna")
    parser.add_argument("--seed", type=generation_seed, help="new generated world's seed; luna defaults to 20261018")
    parser.add_argument("--initial-save", type=Path, help="optional starting save for a new world; restart always uses the checkpoint")
    args = parser.parse_args(argv)
    args.roles, args.workers = profile_bindings(args.profile)
    if args.map_size is None:
        args.map_size = 128 if args.profile == "luna" else 512
    if args.profile == "luna" and args.seed is None:
        args.seed = 20261018
    args.binary, args.baseset = args.binary.resolve(), args.baseset.resolve()
    absolute_state = args.state_dir.absolute()
    if absolute_state.resolve() != absolute_state:
        parser.error("--state-dir must be a canonical path without symlink components")
    args.state_dir = absolute_state
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        parser.error("--binary must name an executable OpenTTD file")
    if not args.baseset.is_dir() or not any(args.baseset.rglob("*.obg")):
        parser.error("--baseset must contain an installed graphics .obg file")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", args.world):
        parser.error("--world must be a simple nonempty stable ID of at most 80 characters")
    if args.http_port == args.native_port:
        parser.error("native and HTTP ports must differ")
    if not args.graphics or any(char in args.graphics for char in "\r\n\0"):
        parser.error("invalid graphics set name")
    if len(os.fsencode(args.state_dir / "ipc" / "engine.sock")) >= 104:
        parser.error("--state-dir is too long for private Unix sockets; use a shorter canonical path")
    if args.initial_save:
        if args.profile == "luna" or args.seed is not None:
            parser.error("--initial-save is for the mixed profile and cannot be combined with --seed")
        args.initial_save = args.initial_save.resolve()
        if not args.initial_save.is_file():
            parser.error("--initial-save must name an existing save file")
    if args.transport == "asgi" and any(importlib.util.find_spec(name) is None for name in ("starlette", "uvicorn", "anyio", "h11")):
        parser.error("ASGI dependencies are missing; install backend/requirements.txt or explicitly use --transport http for local development")
    return args


def main(argv=None):
    os.umask(0o077)
    args = parse_args(argv)
    state = args.state_dir
    stack, lock = None, None
    try:
        private_directory(state, create=not state.exists())
        lock = os.open(state / "launcher.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_uid != os.getuid() or os.fstat(lock).st_mode & 0o077:
            raise StartupError("Unsafe launcher lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StartupError("This world directory already has an active launcher") from None
        fresh = set(path.name for path in state.iterdir()) == {"launcher.lock"}
        # Native OpenTTD uses SO_REUSEPORT, while ASGI uses SO_REUSEADDR.
        # Match their TIME_WAIT behavior, but reject an existing native listener
        # before a reuse-port bind could accidentally share it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", args.native_port)) == 0:
                raise StartupError("Native port already has an active listener")
        for address, kind in ((args.native_port, socket.SOCK_STREAM), (args.native_port, socket.SOCK_DGRAM), (args.http_port, socket.SOCK_STREAM)):
            with socket.socket(socket.AF_INET, kind) as probe:
                if kind == socket.SOCK_STREAM:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if address == args.native_port:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                probe.bind(("127.0.0.1", address))
        manifest, save = (provision(state, args), None) if fresh else load_existing(state, args)
        stack = Stack(state, args, manifest)
        def stop(*_):
            stack.stop_requested = True
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, stop)
        stack.start(save)
        stack.serve()
        return 0
    except KeyboardInterrupt:
        return 0
    except (StartupError, OSError, ValueError, ServiceError) as exc:
        print(f"Launcher stopped: {exc}", file=sys.stderr)
        print("Existing state and credentials were preserved. Inspect private logs before retrying.", file=sys.stderr)
        return 1
    finally:
        if stack:
            stack.close()
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
