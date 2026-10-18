#!/usr/bin/env python3
"""Opt-in real-process load measurement; see LOAD.md for scope and interpretation."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.store import Store
from engine_harness import LiveWorld, RPCError

TERMINAL = {"completed", "rejected", "uncertain", "resolved_rolled_back"}
TICK_SECONDS = 0.027  # src/gfx_type.h MILLISECONDS_PER_TICK; no fast-forward.


class HTTPFailure(RPCError):
    def __init__(self, status, result):
        super().__init__(f"HTTP {status}: {result}")
        self.status = status


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def percentile(values, fraction):
    """Nearest-rank percentile, preserving the explicit empty denominator."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def copy_initial_fixture(args):
    """Copy one immutable completed save, never its ledger/DB/credentials."""
    provenance_fd = os.open(args.provenance, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(provenance_fd, "r", encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= 1024*1024):
            raise RPCError("Fixture provenance must be an owned private regular JSON file of at most1MiB")
        provenance = json.load(source)
    required = {"version", "save_sha256", "source_binary_sha256", "source_world_id", "source_world_epoch",
                "checkpoint_id", "saved_tick", "completed_checkpoint", "construction", "routes"}
    if (not isinstance(provenance, dict) or not required <= provenance.keys()
            or provenance["version"] != 1 or provenance["completed_checkpoint"] is not True
            or not isinstance(provenance["saved_tick"], int) or provenance["saved_tick"] < 0
            or not all(re.fullmatch("[0-9a-f]{64}", provenance[key]) for key in ("save_sha256", "source_binary_sha256"))):
        raise RPCError("Initial fixture provenance is incomplete")
    descriptor = os.open(args.initial_save, os.O_RDONLY | os.O_NOFOLLOW)
    destination = args.output / "initial-fixture.sav"
    copied = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    digest = hashlib.sha256()
    try:
        source_info = os.fstat(descriptor)
        if (not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.getuid() or source_info.st_mode & 0o077
                or not 0 < source_info.st_size <= 128*1024*1024):
            raise RPCError("Initial fixture must be an owned private save of at most128MiB")
        with os.fdopen(descriptor, "rb") as source, os.fdopen(copied, "wb") as target:
            while block := source.read(128*1024):
                digest.update(block); target.write(block)
            target.flush(); os.fsync(target.fileno())
            after = os.fstat(source.fileno())
            if (source_info.st_size, source_info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RPCError("Initial fixture changed while copying")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != provenance["save_sha256"]:
        raise RPCError("Initial fixture checksum does not match provenance")
    provenance["import_binary_sha256"] = hashlib.file_digest(args.binary.open("rb"), "sha256").hexdigest()
    directory_fd = os.open(args.output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    write_json(args.output / "fixture-provenance.json", provenance)
    return destination, provenance


class Metrics:
    def __init__(self, directory):
        self.lock = threading.RLock()
        self.phase = "bootstrap"
        self.file = (directory / "samples.jsonl").open("w", encoding="utf-8", buffering=1)
        self.latencies = defaultdict(list)
        self.counts = Counter()
        self.chat = {}
        self.delivered = set()
        self.clients = set()
        self.snapshots = []
        self.inflight_http = Counter()
        self.started_http = Counter()
        self.schedules = defaultdict(Counter)

    def emit(self, kind, **fields):
        with self.lock:
            row = {"kind": kind, "phase": self.phase, "monotonic": time.monotonic(),
                   "wall_time": time.time(), **fields}
            self.file.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
            if fields.get("measurement_phase", self.phase) == "steady" or (kind == "chat_delivery" and fields.get("sent_phase") == "steady"):
                self.counts[kind] += 1
                if "seconds" in fields and fields.get("ok", True):
                    self.latencies[kind].append(fields["seconds"])
                if kind == "world_sample":
                    self.snapshots.append(fields)
                if kind == "schedule_dispatch":
                    schedule = self.schedules[fields["schedule"]]
                    schedule["dispatches"] += 1
                    schedule["late_over_100ms"] += fields["late_seconds"] > .1
                    schedule["maximum_lateness_seconds"] = max(schedule["maximum_lateness_seconds"], fields["late_seconds"])
                if kind == "client_cadence_missed":
                    self.schedules[fields.get("schedule", "unlabelled")]["missed_slots"] += fields.get("missed_slots", 1)
            return row

    def receive_chat(self, viewer, body):
        now = time.monotonic()
        with self.lock:
            sent = self.chat.get(body)
            key = (viewer, body)
            if sent is None or key in self.delivered:
                return
            self.delivered.add(key)
            self.emit("chat_delivery", viewer=viewer, seconds=now-sent["start"],
                      sent_phase=sent["phase"], message_key=body, ok=True)

    def pending_http(self, phase):
        with self.lock:
            return {kind: count for (origin, kind), count in self.inflight_http.items() if origin == phase and count}

    def started_http_counts(self, phase):
        with self.lock:
            return {kind: count for (origin, kind), count in self.started_http.items() if origin == phase}

    def summarize(self):
        with self.lock:
            expected = [(viewer, body) for body, sent in self.chat.items()
                        if sent["phase"] == "steady" and sent.get("accepted")
                        for viewer in sent["viewers"]]
            missing = sum(pair not in self.delivered for pair in expected)
            gauges = ("backend_pending_actions", "backend_pending_chat", "owned_heads", "active_heads", "moving_now", "connected_sse")
            return {"event_counts": dict(self.counts), "latencies": {
                kind: {"samples": len(values), "p50_seconds": percentile(values, .5),
                       "p95_seconds": percentile(values, .95), "max_seconds": max(values)}
                for kind, values in self.latencies.items()},
                "sampled_ranges": {name: {"min": min(row[name] for row in self.snapshots),
                    "max": max(row[name] for row in self.snapshots)} for name in gauges} if self.snapshots else {},
                "schedules": {name: dict(counts) for name, counts in self.schedules.items()},
                "chat_expected_deliveries": len(expected), "chat_missing_deliveries": missing}


class HTTP:
    def __init__(self, port, token, metrics, *, label):
        self.port, self.token, self.metrics, self.label = port, token, metrics, label

    def request(self, method, path, body=None, *, metric=None, metric_fields=None):
        deadline = time.monotonic()+60
        while True:
            try:
                return self._request(method, path, body, metric=metric, metric_fields=metric_fields)
            except HTTPFailure as error:
                if (self.metrics.phase != "bootstrap" or metric is not None or error.status not in (429, 503)
                        or time.monotonic() >= deadline):
                    raise
                self.metrics.emit("bootstrap_backpressure", client=self.label, status=error.status)
                time.sleep(.2)

    def _request(self, method, path, body=None, *, metric=None, metric_fields=None):
        started = time.monotonic()
        measurement_phase = self.metrics.phase
        if metric:
            with self.metrics.lock:
                self.metrics.inflight_http[(measurement_phase, metric)] += 1
                self.metrics.started_http[(measurement_phase, metric)] += 1
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Authorization": "Bearer " + self.token}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        status, ok, size = 0, False, 0
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read(16 * 1024 * 1024 + 1)
            size = len(raw)
            if len(raw) > 16 * 1024 * 1024:
                raise RuntimeError("Oversized HTTP response")
            result = raw if response.getheader("Content-Type", "").startswith("image/png") else json.loads(raw)
            ok = 200 <= status < 300
            if not ok:
                raise HTTPFailure(status, result)
            return result
        finally:
            connection.close()
            if metric:
                self.metrics.emit(metric, seconds=time.monotonic()-started, ok=ok,
                                  status=status, bytes=size, client=self.label,
                                  measurement_phase=measurement_phase, **(metric_fields or {}))
                with self.metrics.lock:
                    self.metrics.inflight_http[(measurement_phase, metric)] -= 1

    def action(self, name, parameters, *, admission_only=False, metric=None):
        request_id = uuid.uuid4().hex
        result = self.request("POST", "/v1/actions", {
            "request_id": request_id, "action": {"name": name, "parameters": parameters}}, metric=metric)
        if admission_only:
            return result
        deadline = time.monotonic() + 30
        while result["status"] not in TERMINAL:
            if time.monotonic() >= deadline:
                raise RPCError(f"Action did not complete: {name}: {result}")
            time.sleep(.025)
            result = self.request("GET", "/v1/actions/" + request_id)
        if result["status"] != "completed" or not result.get("result", {}).get("success"):
            raise RPCError(f"Action failed: {name}: {result}")
        return result["result"]

    def observe(self, name, parameters=None):
        return self.request("POST", "/v1/observations", {
            "query": name, "parameters": parameters or {}})["observation"]


class FreshWorld(LiveWorld):
    """LiveWorld's private operator transport, with normal seeded newgame startup."""
    def __init__(self, binary, baseset, *, port, seed, fixture=None):
        super().__init__(binary, baseset, port=port)
        self.seed = seed
        self.initial_save = fixture
        self.epoch = uuid.uuid4().hex
        self.config_file.write_text(
            "[version]\nini_version = 8\n[network]\nserver_game_type = local\n"
            "pause_on_join = false\nmin_active_clients = 0\nmax_companies = 15\nframe_freq = 0\n"
            "[gui]\npause_on_newgame = false\nautosave = off\n"
            "[misc]\nlanguage = english.lng\nsavegame_format = lzma:2\n"
            "[game_creation]\nmap_x = 9\nmap_y = 9\nland_generator = 1\n"
            "[difficulty]\nterrain_type = 0\nquantity_sea_lakes = 0\nmax_loan = 2000000\n",
            encoding="utf-8")
        (self.config_file.parent / "private.cfg").write_text(
            "[version]\nini_version = 8\n[network]\nframe_freq = 0\n"
            "server_name = Load benchmark\nmax_lag_time = 500\n", encoding="utf-8")

    def start(self, save=None):
        if save or self.initial_save:
            return super().start(save or self.initial_save)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("OPENTTD_INTEGRATION_") and key != "OPENTTD_RENDERER_IMAGE_DIR"}
        environment.update({"XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.config),
            "OPENTTD_INTEGRATION_SOCKET": str(self.socket_path), "OPENTTD_INTEGRATION_STATE_DIR": str(self.state),
            "OPENTTD_INTEGRATION_WORLD_ID": self.world_id, "OPENTTD_INTEGRATION_WORLD_EPOCH": self.epoch})
        if os.environ.get("OPENTTD_TEST_LIBRARY_PATH"):
            environment["LD_LIBRARY_PATH"] = os.environ["OPENTTD_TEST_LIBRARY_PATH"] + ":" + environment.get("LD_LIBRARY_PATH", "")
        self.log = (self.directory / "engine.log").open("ab", buffering=0)
        self.process = subprocess.Popen([str(self.binary), "-D", f"127.0.0.1:{self.port}",
            "-G", str(self.seed), "-g", "-t", "1950", "-x", "-I", "OpenGFX", "-d", "net=1"],
            cwd=self.directory, env=environment, stdin=subprocess.PIPE, stdout=self.log,
            stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RPCError(f"Fresh engine exited: {self.log_tail()}")
            try:
                hello = self.rpc("hello")
                self.instance = hello["engine_instance_id"]
                if self.rpc("checkpoint.status").get("result", {}).get("load_verified"):
                    self.console("unpause")
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(.05)
        raise RPCError(f"Fresh world did not become ready: {self.log_tail()}")


class Group:
    def __init__(self, args, index, metrics):
        self.args, self.index, self.metrics = args, index, metrics
        self.world = FreshWorld(args.binary, args.baseset, port=args.native_port+20*index, seed=args.seed+index,
                                fixture=args.initial_save if index == 0 else None)
        self.api_port = args.api_port + 20*index
        self.backend = None
        self.renderers = []
        self.http = []
        self.visual = []
        self.process_logs = []
        self.database = self.world.directory / "world.sqlite3"
        self.routes = []
        self.handles = []

    def spawn(self, name, command, environment=None, cwd=None):
        log = (self.world.directory / (name + ".log")).open("ab", buffering=0)
        self.process_logs.append(log)
        return subprocess.Popen(command, cwd=cwd or ROOT, env=environment,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)

    def start(self):
        self.world.start()
        store = Store(self.database, world_id=self.world.world_id)
        runs = {}
        try:
            for company in range(8 if self.index == 0 else 1):
                kind = "human" if company >= 6 else "agent"
                token = store.provision(self.world.world_id, f"load-{self.index}-setup-{company}", company,
                                        mode="text", kind=kind, display_name=f"Load company {company+1}", world_epoch=self.world.epoch)
                self.http.append(HTTP(self.api_port, token, self.metrics, label=f"world{self.index}-company{company}"))
            for number in range(4 if self.index == 0 else 0):
                token = store.provision(self.world.world_id, f"load-visual-{number}", number,
                                        mode="visual", kind="agent", display_name=f"Visual agent {number+1}", world_epoch=self.world.epoch)
                principal = store.authenticate(token)
                directory = self.world.directory / f"renderer{number}"
                for path in (directory, directory / "images", directory / "config/openttd", directory / "data/openttd"):
                    path.mkdir(mode=0o700, parents=True, exist_ok=True)
                (directory / "data/openttd/baseset").symlink_to(self.world.baseset, target_is_directory=True)
                (directory / "config/openttd/openttd.cfg").write_text(
                    "[version]\nini_version = 8\n[gui]\nautosave = off\n[misc]\nlanguage = english.lng\n")
                (directory / "config/openttd/private.cfg").write_text(
                    f"[version]\nini_version = 8\n[network]\nclient_name = load-renderer-{number}\n")
                (directory / "config/openttd/secrets.cfg").write_text(
                    f"[version]\nini_version = 8\n[network]\nclient_secret_key = {secrets.token_hex(32)}\n")
                runs[principal.run_id] = {"socket": str(directory / "renderer.sock"), "artifacts": str(directory / "images")}
                self.visual.append(HTTP(self.api_port, token, self.metrics, label=f"visual{number}"))
            credentials = {"setup": [client.token for client in self.http], "visual": [client.token for client in self.visual]}
            write_json(self.world.directory / "credentials.json", credentials)
        finally:
            store.close()
        config = self.world.directory / "renderers.json"
        if runs:
            write_json(config, {"version": 1, "world_id": self.world.world_id, "runs": runs})
        command = [sys.executable, "-m", "backend", "serve", "--database", str(self.database),
                   "--world", self.world.world_id, "--engine-socket", str(self.world.socket_path),
                   "--transport", "asgi", "--host", "127.0.0.1", "--port", str(self.api_port)]
        if runs:
            command += ["--renderer-config", str(config)]
        self.backend = self.spawn("backend", command)
        self.wait(lambda: not self.world.rpc("checkpoint.status").get("result", {}).get("recovery_required", True))
        for company in range(len(self.http)):
            self.wait(lambda company=company: self.world.rpc("company.ensure", company_id=company,
                create=not(self.index == 0 and self.args.initial_save)).get("status") == "completed")
        self.wait(lambda: self.http[0].request("GET", "/v1/capabilities").get("engine_connected"))
        for number in range(len(self.visual)):
            directory = self.world.directory / f"renderer{number}"
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("OPENTTD_INTEGRATION_") and key != "OPENTTD_RENDERER_IMAGE_DIR"}
            environment.update({"XDG_DATA_HOME": str(directory / "data"), "XDG_CONFIG_HOME": str(directory / "config"),
                "OPENTTD_INTEGRATION_WORLD_ID": self.world.world_id, "OPENTTD_INTEGRATION_WORLD_EPOCH": self.world.epoch,
                "OPENTTD_INTEGRATION_ROLE": "renderer", "OPENTTD_INTEGRATION_STATE_DIR": str(directory),
                "OPENTTD_INTEGRATION_SOCKET": str(directory / "renderer.sock"),
                "OPENTTD_RENDERER_IMAGE_DIR": str(directory / "images")})
            if os.environ.get("OPENTTD_TEST_LIBRARY_PATH"):
                environment["LD_LIBRARY_PATH"] = os.environ["OPENTTD_TEST_LIBRARY_PATH"] + ":" + environment.get("LD_LIBRARY_PATH", "")
            child = self.spawn(f"renderer{number}", [str(self.args.binary), "-v", "framebuffer", "-s", "null",
                "-m", "null", "-r", "1280x720", "-n", f"127.0.0.1:{self.world.port}#{number+1}",
                "-x", "-I", "OpenGFX", "-d", "net=1"], environment, directory)
            self.renderers.append(child)
            self.wait(lambda number=number: self.visual[number].request("POST", "/v1/view/images", {}), timeout=60)
        dimensions = self.http[0].observe("map.region", {"x": 1, "y": 1, "width": 1, "height": 1})
        if (dimensions.get("map_width"), dimensions.get("map_height")) != (512, 512):
            raise RPCError("Load fixture is not actually 512 by 512")

    def wait(self, operation, timeout=45):
        deadline = time.monotonic()+timeout
        last = None
        while time.monotonic() < deadline:
            if self.world.process.poll() is not None or (self.backend and self.backend.poll() is not None):
                raise RPCError(f"World/backend exited; logs: {self.world.directory}")
            try:
                result = operation()
                if result:
                    return result
            except (OSError, ValueError, RPCError) as error:
                last = str(error)
            time.sleep(.1)
        raise RPCError(f"Startup deadline expired: {self.world.directory}: {last}")

    def seed_routes(self, count):
        if self.index == 0 and self.args.initial_save:
            return self.validate_initial_fixture(count)
        client = self.http[0]
        # Flat land is discovered through bounded normal observations. No map
        # writes or terrain/money shortcuts prepare the fixture.
        candidates = []
        route_length = 48
        needed = 32 if self.index == 0 else 1
        for y in range(8, 500, 5):
            for x in range(8, 450, 55):
                parameters = {"x": x, "y": y, "width": route_length, "height": 3}
                region = client.observe("map.region", parameters)
                tiles = list(region["tiles"])
                while not region["complete"]:
                    parameters["after_offset"] = region["next_after_offset"]
                    region = client.observe("map.region", parameters)
                    tiles.extend(region["tiles"])
                if all(t["type"] in (0, 4) and t["slope"] == 0 and t["height"] == tiles[0]["height"] > 0 for t in tiles):
                    candidates.append((x, y))
                    if len(candidates) == needed:
                        break
            if len(candidates) == needed:
                break
        if len(candidates) < needed:
            raise RPCError(f"Found only {len(candidates)}/{needed} flat road sites; do not claim the target")
        def provision_company(company, client):
            finance = client.observe("company")
            amount = finance["maximum_loan"] - finance["loan"]
            if amount > 0:
                client.action("company.loan.increase", {"amount": amount})
            engine = min((item for item in client.observe("catalog.engines", {"vehicle_type": "road", "limit": 100})["items"]
                          if item.get("default_cargo_id") == 0 and item.get("road_type") == 0), key=lambda item: item["price"])
            number = count//len(self.http) + (company < count % len(self.http))
            n_routes = 4 if self.index == 0 else 1
            for route in range(n_routes):
                x, y = candidates[company*n_routes+route]
                tile = lambda dx, dy=0: (y+dy)*512+x+dx
                client.action("road.build", {"start": tile(1), "end": tile(route_length-2)})
                client.action("road.build", {"start": tile(5), "end": tile(5, 1)})
                client.action("terrain.clear", {"tile": tile(5, 1)})
                client.action("road.depot.build", {"tile": tile(5, 1), "direction": 3})
                client.action("road.stop.build", {"tile": tile(0), "direction": 2, "stop_type": "bus"})
                client.action("road.stop.build", {"tile": tile(route_length-1), "direction": 0, "stop_type": "bus"})
                prototype = client.action("vehicle.build", {"tile": tile(5, 1), "engine_id": engine["engine_id"]})["vehicle_id"]
                for destination in (tile(0), tile(route_length-1)):
                    order_version = client.observe("orders", {"vehicle_id": prototype})["order_version"]
                    client.action("order.insert", {"vehicle_id": prototype, "order_version": order_version,
                                                   "destination": destination})
                handles = [prototype]
                route_count = number//n_routes + (route < number % n_routes)
                for _ in range(route_count-1):
                    handles.append(client.action("vehicle.clone", {"tile": tile(5, 1), "vehicle_id": prototype, "share_orders": True})["vehicle_id"])
                for handle in handles:
                    client.action("vehicle.start_stop", {"vehicle_id": handle})
                self.handles.extend((company, handle) for handle in handles)
                self.routes.append({"company": company, "x": x, "y": y, "length": route_length,
                                    "vehicles": len(handles), "engine_id": engine["engine_id"]})
                print(f"world {self.index}: seeded company {company+1} route {route+1}, {len(self.handles)}/{count} vehicles", flush=True)
        # Independent company setup can overlap normal HTTP admission/polling.
        # The backend and engine still own fairness, sequencing and real costs.
        with ThreadPoolExecutor(max_workers=min(4, len(self.http))) as executor:
            futures = [executor.submit(provision_company, company, client)
                       for company, client in enumerate(self.http)]
            for future in as_completed(futures):
                future.result()

    def validate_initial_fixture(self, count):
        vehicles = self.vehicles()
        owners = Counter(company for company, _ in vehicles)
        expected = {company: count//8+(company < count % 8) for company in range(8)}
        if (count != 500 or len(vehicles) != count or dict(owners) != expected
                or any(vehicle["stopped"] or vehicle["crashed"] or vehicle["order_count"] < 2 for _, vehicle in vehicles)):
            raise RPCError("Initial save must have exactly500 ordered running heads across the8 expected companies")
        provenance = self.args.fixture_provenance
        purchases = provenance["construction"].get("vehicle_purchases", [])
        if ({(company, vehicle["vehicle_id"]["id"]) for company, vehicle in vehicles}
                != {(purchase["company_id"], purchase["vehicle_id"]) for purchase in purchases}):
            raise RPCError("Loaded vehicle IDs/companies differ from normal-purchase provenance")
        if provenance["construction"].get("positive_gameplay_cost", 0) <= 0:
            raise RPCError("Missing normal construction/purchase cost provenance")
        tick = self.http[0].observe("company")["tick"]
        if tick < provenance["saved_tick"]:
            raise RPCError("Loaded tick predates the completed fixture checkpoint")
        self.world.console("getdate")
        self.wait(lambda: re.search(r"Date: (\d+-\d{2}-\d{2})", self.world.log_tail()))
        date = re.findall(r"Date: (\d+-\d{2}-\d{2})", self.world.log_tail())[-1]
        ages = [(tick-purchase["executed_tick"])/74 for purchase in purchases]
        self.routes = provenance["routes"]
        self.handles = [(company, vehicle["vehicle_id"]) for company, vehicle in vehicles]
        validation = {"world_id": self.world.world_id, "world_epoch": self.world.epoch, "tick": tick,
            "calendar_date_after_load": date, "owned_heads": len(vehicles), "company_counts": dict(owners),
            "age_days_lower_bound": max(0, math.floor(min(ages))-1), "age_days_upper_bound": math.ceil(max(ages))+1,
            "age_method": "Natural age bounds from normal purchase execution ticks,74ticks/calendar-day,one-day daily-update uncertainty; exact age DTO unavailable.",
            "source_checkpoint_tick": provenance["saved_tick"], "fresh_ledger_and_credentials": True}
        validation["import_binary_sha256"] = provenance["import_binary_sha256"]
        write_json(self.args.output / "fixture-validation.json", validation)
        print(f"Validated immutable fixture:500 active heads,8 companies,date={date},tick={tick}; fresh world/run identities", flush=True)

    def vehicles(self):
        result = []
        for company, client in enumerate(self.http):
            after = None
            while True:
                params = {"limit": 100}
                if after is not None:
                    params["after_id"] = after
                page = client.observe("vehicles", params)
                result.extend((company, vehicle) for vehicle in page["items"])
                after = page.get("next_after_id")
                if after is None:
                    break
        return result

    def processes(self):
        return [(f"world{self.index}.engine", self.world.process),
                (f"world{self.index}.backend", self.backend)] + [
                    (f"world{self.index}.renderer{i}", process) for i, process in enumerate(self.renderers)]

    def stop(self):
        for process in [self.backend, *self.renderers]:
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
        self.world.stop()  # Preserve the state/log directory; it is in manifest.json.
        for log in self.process_logs:
            log.close()


class Load:
    def __init__(self, args, metrics, groups):
        self.args, self.metrics, self.groups = args, metrics, groups
        self.stop = threading.Event()
        self.threads = []
        self.connections = set()
        self.lock = threading.Lock()
        self.sample_lock = threading.Lock()
        self.last_positions = {}
        self.moved = set()
        self.window_started = time.monotonic()
        self.previous_ticks = {}
        self.steady_ticks = {}
        self.speeds = defaultdict(list)
        self.minimum_active = args.vehicles
        self.last_active = None
        self.minimum_streams = args.viewers
        self.last_images = {}
        self.unique_images = defaultdict(set)
        self.burst_barrier = threading.Barrier(args.viewers+4) if args.burst_images else None
        self.burst_seen, self.burst_completed = set(), set()

    def thread(self, function, *args):
        thread = threading.Thread(target=function, args=args, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def periodic(self, interval, callback, phase=0, label="client", steady_only=False):
        self.stop.wait(phase)
        deadline = time.monotonic()
        while not self.stop.is_set():
            if steady_only and self.metrics.phase != "steady":
                return
            if steady_only:
                self.metrics.emit("schedule_dispatch", schedule=label,
                                  late_seconds=max(0, time.monotonic()-deadline))
            operation_phase = self.metrics.phase
            try:
                callback()
            except Exception as error:
                self.metrics.emit("client_error", error=str(error)[:500],
                                  measurement_phase=operation_phase, schedule=label)
            deadline += interval
            if deadline < time.monotonic():
                self.metrics.emit("client_cadence_missed", seconds=time.monotonic()-deadline,
                                  schedule=label, missed_slots=1+int((time.monotonic()-deadline)/interval))
                deadline = time.monotonic()
            self.stop.wait(max(0, deadline-time.monotonic()))

    def image(self, client, number, agent=False):
        kind = "visual" if agent else "browser"
        with self.lock:
            burst = self.burst_barrier is not None and client.label not in self.burst_seen
            self.burst_seen.add(client.label)
        image = client.request("POST", "/v1/view/images", {}, metric=kind+"_capture")
        if burst:
            # Each client obtained its own image through normal POST; synchronize
            # only its first PNG GET to stress image admission independently.
            self.burst_barrier.wait(timeout=10)
        data = client.request("GET", "/v1/images/"+image["image_id"], metric=kind+"_png")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RPCError("Invalid served PNG")
        # Operator-only filesystem measurement: the public visual DTO deliberately
        # omits simulation coordinates/ticks. Repeated parked frames retain mtime.
        artifact = self.groups[0].world.directory / f"renderer{number % 4}" / "images" / ("frame-"+image["image_id"]+".png")
        try:
            age = max(0, time.time()-artifact.stat().st_mtime)
            self.metrics.emit("image_frame_age", seconds=age, ok=True, view=number % 4)
        except FileNotFoundError:
            self.metrics.emit("image_frame_age_unavailable", view=number % 4)
        with self.lock:
            if burst:
                self.burst_completed.add(client.label)
                self.metrics.emit("burst_png_completed", client=client.label, bytes=len(data))
            self.last_images[number % 4] = image["image_id"]
            if self.metrics.phase == "steady":
                self.unique_images[number % 4].add(image["image_id"])

    def sse(self, client, number):
        cursor = None
        while not self.stop.is_set():
            connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=15)
            with self.lock:
                self.connections.add(connection)
            try:
                headers = {"Authorization": "Bearer "+client.token, "Accept": "text/event-stream"}
                if cursor:
                    headers["Last-Event-ID"] = cursor
                connection.request("GET", "/v1/events", headers=headers)
                response = connection.getresponse()
                if response.status != 200:
                    raise RPCError(f"SSE HTTP {response.status}")
                with self.metrics.lock:
                    self.metrics.clients.add(number)
                event = {}
                while not self.stop.is_set():
                    line = response.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536:
                        raise RPCError("Oversized SSE frame")
                    if line in (b"\n", b"\r\n"):
                        if event.get("id"):
                            cursor = event["id"]
                        if event.get("event") == "chat.message" and "data" in event:
                            self.metrics.receive_chat(number, json.loads(event["data"]).get("body"))
                        event = {}
                    else:
                        key, separator, value = line.decode("utf-8").partition(":")
                        if separator and key in ("id", "event", "data"):
                            event[key] = value.strip()
            except Exception as error:
                if not self.stop.is_set():
                    self.metrics.emit("sse_error", viewer=number, error=str(error)[:300])
            finally:
                with self.metrics.lock:
                    self.metrics.clients.discard(number)
                connection.close()
                with self.lock:
                    self.connections.discard(connection)
            self.stop.wait(.1)  # The normal 60-second stream lifetime reconnects with its cursor.

    def start(self):
        primary = self.groups[0]
        for i in range(self.args.viewers):
            client = HTTP(primary.api_port, primary.visual[i % 4].token, self.metrics, label=f"browser{i}")
            self.thread(self.sse, client, i)
            self.thread(self.periodic, 1.02, lambda client=client, i=i: self.image(client, i), i/self.args.viewers, f"browser{i}")
        for i, client in enumerate(primary.visual):
            self.thread(self.periodic, 1.02, lambda client=client, i=i: self.image(client, i, True), i*.25, f"visual{i}")
        self.thread(self.periodic, 5, self.sample, 0, "sample")

    def chat(self, thread_id, sequence):
        client = self.groups[0].http[sequence % 8]
        body = f"load-{self.args.run_id}-{sequence}"
        with self.metrics.lock:
            # Expect every configured client, including clients briefly reconnecting.
            self.metrics.chat[body] = {"start": time.monotonic(), "phase": self.metrics.phase,
                                       "viewers": list(range(self.args.viewers)), "accepted": False}
        client.request("POST", f"/v1/threads/{thread_id}/messages", {
            "request_id": uuid.uuid4().hex, "body": body}, metric="chat_ack", metric_fields={"message_key": body})
        with self.metrics.lock:
            self.metrics.chat[body]["accepted"] = True

    def admission(self, sequence):
        client = self.groups[0].http[sequence % 8]
        client.action("company.rename", {"name": f"Load-{sequence % 8}-{sequence:08d}"},
                      admission_only=True, metric="action_ack")

    def sample(self, freeze=False):
        with self.sample_lock:
            self._sample()
            return dict(self.previous_ticks) if freeze else None

    def begin_steady(self):
        with self.sample_lock:
            self.previous_ticks.clear(); self.steady_ticks.clear(); self.speeds.clear()
            for group in self.groups:
                tick = group.world.rpc("hello")["tick"]
                observation = (time.monotonic(), tick)
                self.previous_ticks[group.index] = observation
                self.steady_ticks[group.index] = observation
            self.window_started = time.monotonic(); self.moved.clear()

    def _sample(self):
        now = time.monotonic()
        tick_samples = {}
        for group in self.groups:
            reply = group.world.rpc("hello")
            tick = reply["tick"]
            observed = time.monotonic()
            prior = self.previous_ticks.get(group.index)
            ratio = None if prior is None or observed-prior[0] < 4 else (tick-prior[1])*TICK_SECONDS/(observed-prior[0])
            self.previous_ticks[group.index] = (observed, tick)
            if self.metrics.phase == "steady":
                self.steady_ticks.setdefault(group.index, (observed, tick))
                if ratio is not None:
                    self.speeds[group.index].append(ratio)
            group.http[0].request("GET", "/v1/me", metric=f"world{group.index}_api_probe")
            tick_samples[group.index] = {"tick": tick, "observed_monotonic": observed, "normal_speed_ratio": ratio}
        vehicles = self.groups[0].vehicles()
        active = sum(not v["stopped"] and not v["crashed"] and v["order_count"] >= 2 for _, v in vehicles)
        self.last_active = active
        moving = sum(v["speed"] > 0 for _, v in vehicles)
        for company, vehicle in vehicles:
            key = (company, vehicle["vehicle_id"]["id"])
            position = vehicle["location"]["tile"]
            if key in self.last_positions and self.last_positions[key] != position:
                self.moved.add(key)
            self.last_positions[key] = position
        if self.metrics.phase == "steady":
            self.minimum_active = min(self.minimum_active, active)
            # A stream reconnect is observed, not silently treated as permanently present.
            self.minimum_streams = min(self.minimum_streams, len(self.metrics.clients))
        movement = None
        if now-self.window_started >= 60:
            movement = {"distinct_heads_changed_tile": len(self.moved), "seconds": now-self.window_started}
            self.moved.clear(); self.window_started = now
        with sqlite3.connect(f"file:{self.groups[0].database}?mode=ro", uri=True, timeout=2) as database:
            pending = database.execute("SELECT COUNT(*) FROM action_requests WHERE status IN ('accepted','queued','uncertain')").fetchone()[0]
            chat_pending = database.execute("SELECT COUNT(*) FROM social_delivery_outbox WHERE status!='delivered'").fetchone()[0]
        processes = {}
        for group in self.groups:
            for name, process in group.processes():
                if process is None:
                    continue
                if process.poll() is not None:
                    processes[name] = {"exited": process.returncode}
                    continue
                stat = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
                status = dict(line.split(":", 1) for line in Path(f"/proc/{process.pid}/status").read_text().splitlines() if ":" in line)
                processes[name] = {"pid": process.pid, "cpu_seconds": (int(stat[11])+int(stat[12]))/os.sysconf("SC_CLK_TCK"),
                                   "rss_bytes": int(stat[21])*os.sysconf("SC_PAGE_SIZE"),
                                   "rss_peak_bytes": int(status.get("VmHWM", "0 kB").split()[0])*1024}
        self.metrics.emit("world_sample", worlds=tick_samples, owned_heads=len(vehicles), active_heads=active,
                          moving_now=moving, movement_window=movement, connected_sse=len(self.metrics.clients),
                          backend_pending_actions=pending, backend_pending_chat=chat_pending, processes=processes)
        print(f"{self.metrics.phase}: active={active}, moving={moving}, SSE={len(self.metrics.clients)}, pending={pending}, primary_speed={tick_samples[0]['normal_speed_ratio']}", flush=True)

    def faults(self):
        self.metrics.phase = "fault"
        primary = self.groups[0]
        primary.renderers[0].kill()
        primary.renderers[0].wait(timeout=5)
        # A real unread image response with a tiny receive buffer forces the
        # transport's bounded send path; an idle SSE socket would not do so.
        with self.lock:
            image_id = self.last_images.get(1)
        image_bytes = len(primary.visual[1].request("GET", "/v1/images/"+image_id))
        tcp_send_bound = Path("/proc/sys/net/ipv4/tcp_wmem").read_text().strip()
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        connection.settimeout(5)
        try:
            connection.connect(("127.0.0.1", primary.api_port))
            request = (f"GET /v1/images/{image_id} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {primary.visual[1].token}\r\nConnection: keep-alive\r\n\r\n").encode()
            # A single small PNG can fit entirely in a sender's kernel buffer.
            # An unread pipeline exceeds that buffer without changing production
            # socket settings or holding extra service stream identities.
            connection.sendall(request*64)
            before = {group.index: group.world.rpc("hello")["tick"] for group in self.groups}
            self.metrics.emit("fault_started", killed_renderer=0, unread_png=True, receive_buffer=1024,
                              pipelined_png_requests=64, initial_ticks=before, png_bytes=image_bytes,
                              offered_response_bytes=64*image_bytes, tcp_wmem=tcp_send_bound,
                              saturation_proof="Unread client offered; actual blocked send/slot timeout is not instrumented")
            self.stop.wait(min(2, self.args.fault_seconds))
            early_started = time.monotonic()
            for group in self.groups:
                group.http[0].action("company.rename", {"name": f"Early-fault-world-{group.index}"})
            early_seconds = time.monotonic()-early_started
            self.metrics.emit("fault_early_action_probes", completed=len(self.groups), seconds=early_seconds)
            self.stop.wait(max(0, self.args.fault_seconds-2-early_seconds))
            self.sample()
            after = {group.index: group.world.rpc("hello")["tick"] for group in self.groups}
            probe_started = time.monotonic()
            for group in self.groups:
                group.http[0].action("company.rename", {"name": f"Fault-probe-world-{group.index}"})
            result = {"tick_deltas": {index: after[index]-tick for index, tick in before.items()},
                "api_probes_ok": all(group.http[0].request("GET", "/v1/me").get("world_id") == group.world.world_id for group in self.groups),
                "ordinary_actions_completed": len(self.groups), "action_probe_seconds": time.monotonic()-probe_started,
                "early_ordinary_actions_completed": len(self.groups), "early_action_probe_seconds": early_seconds,
                "png_bytes": image_bytes, "offered_response_bytes": 64*image_bytes, "tcp_wmem": tcp_send_bound,
                "saturation_proof": "Unread client offered; actual blocked send/slot timeout is not instrumented"}
            self.metrics.emit("fault_completed", **result)
            return result
        finally:
            connection.close()

    def close(self):
        self.stop.set()
        with self.lock:
            for connection in list(self.connections):
                if connection.sock:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
        for thread in self.threads:
            thread.join(timeout=.25)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", type=Path, help="audit a completed run into a separate audited-summary.json")
    parser.add_argument("--initial-save", type=Path, help="immutable completed normal-gameplay fixture save")
    parser.add_argument("--provenance", type=Path, help="matching completed-checkpoint/purchase provenance JSON")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--baseset", type=Path)
    parser.add_argument("--output", type=Path, help="new owner-only results directory")
    parser.add_argument("--duration", type=float, default=1800)
    parser.add_argument("--warmup", type=float, default=15)
    parser.add_argument("--vehicles", type=int, default=500)
    parser.add_argument("--viewers", type=int, default=50)
    parser.add_argument("--worlds", type=int, default=4)
    parser.add_argument("--action-rate", type=float, default=1)
    parser.add_argument("--chat-rate", type=float, default=1)
    parser.add_argument("--fault-seconds", type=float, default=30)
    parser.add_argument("--burst-images", action="store_true", help="synchronize each client's first owned PNG GET; bootstrap fails unless all succeed")
    parser.add_argument("--native-port", type=int, default=16979)
    parser.add_argument("--api-port", type=int, default=16980)
    parser.add_argument("--seed", type=int, default=20261017)
    args = parser.parse_args(argv)
    if args.analyze:
        return args
    if not all((args.binary, args.baseset, args.output)):
        parser.error("--binary, --baseset and --output are required for a live run")
    if bool(args.initial_save) != bool(args.provenance) or (args.initial_save and args.vehicles != 500):
        parser.error("--initial-save and --provenance must be supplied together with --vehicles500")
    if (args.duration <= 0 or args.warmup < 0 or args.fault_seconds < 0 or not 32 <= args.vehicles <= 500
            or not 1 <= args.viewers <= 50 or not 1 <= args.worlds <= 4
            or not .1 <= args.action_rate <= 4 or not .1 <= args.chat_rate <= 4):
        parser.error("invalid load bounds; use at least32 vehicles and positive duration/rates")
    ports = [port+20*i for port in (args.native_port, args.api_port) for i in range(args.worlds)]
    if len(set(ports)) != len(ports) or min(ports) < 1024 or max(ports) > 65535:
        parser.error("native/API world port groups overlap or exceed1024..65535")
    args.binary, args.baseset, args.output = args.binary.resolve(), args.baseset.resolve(), args.output.absolute()
    if not args.binary.is_file() or not args.baseset.is_dir() or args.output.exists():
        parser.error("binary/baseset must exist and output directory must be new")
    args.run_id = uuid.uuid4().hex[:12]
    return args


def analyze_results(directory):
    """Audit all late records, retaining the original summary without edits.

    Early driver versions lack boundary tick timestamps. An interior observed
    tick delta divided by the entire steady interval is a conservative lower
    bound; no positive progress is credited to the unobserved edges.
    """
    directory = directory.resolve()
    config = json.loads((directory / "configuration.json").read_text())
    original = json.loads((directory / "summary.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    attempts, successes, failures = Counter(), Counter(), []
    clients = defaultdict(Counter)
    timings = defaultdict(list)
    samples, deliveries, completed = [], 0, False
    visual_clients = set()
    memory_peaks = Counter()
    accepted_messages, delivery_pairs = set(), set()
    deliveries_by_viewer = Counter()
    unkeyed_deliveries = duplicate_deliveries = 0
    viewers, world_ids, image_counts = set(), set(), Counter()
    cadence = Counter()
    measured_start = None
    measured_start_wall = None
    with (directory / "samples.jsonl").open(encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            kind = row["kind"]
            if kind == "steady_started":
                measured_start = row["monotonic"]
                measured_start_wall = row["wall_time"]
            if kind == "run_finished":
                completed = row.get("completed") is True
            if kind == "world_sample":
                for name, values in row.get("processes", {}).items():
                    memory_peaks[name] = max(memory_peaks[name], values.get("rss_peak_bytes", values.get("rss_bytes", 0)))
            if kind == "chat_delivery" and row.get("sent_phase") == "steady":
                deliveries += 1
                viewers.add(row["viewer"])
                deliveries_by_viewer[row["viewer"]] += 1
                if isinstance(row.get("message_key"), str):
                    pair = (row["viewer"], row["message_key"])
                    duplicate_deliveries += pair in delivery_pairs
                    delivery_pairs.add(pair)
                else:
                    unkeyed_deliveries += 1
                timings[kind].append(row["seconds"])
            origin = row.get("measurement_phase", row.get("phase"))
            if origin != "steady":
                continue
            if kind.endswith(("_ack", "_capture", "_png", "_api_probe")):
                attempts[kind] += 1
                if row.get("ok") is True:
                    successes[kind] += 1
                    clients[kind][row["client"]] += 1
                    timings[kind].append(row["seconds"])
                    if kind == "visual_capture":
                        visual_clients.add(row["client"])
                    if kind == "chat_ack" and isinstance(row.get("message_key"), str):
                        accepted_messages.add(row["message_key"])
                else:
                    failures.append({"kind": kind, "status": row.get("status"), "monotonic": row["monotonic"]})
            if kind.endswith("_error"):
                failures.append({"kind": kind, "monotonic": row["monotonic"]})
            if kind == "world_sample":
                samples.append(row)
                world_ids.update(row["worlds"])
            if kind == "image_frame_age":
                timings[kind].append(row["seconds"])
                image_counts[row["view"]] += 1
            if kind == "client_cadence_missed":
                cadence[row.get("schedule", "unlabelled")] += row.get("missed_slots", 1)
    elapsed = original["steady_seconds"]
    interior = samples[1:-1]
    speed_bound, window_minimum, sampling_gaps = {}, {}, []
    if len(interior) >= 2:
        for world in sorted(world_ids):
            speed_bound[world] = (interior[-1]["worlds"][world]["tick"]-interior[0]["worlds"][world]["tick"])*TICK_SECONDS/elapsed
            windows = []
            for index, row in enumerate(samples):
                ratio = row["worlds"][world].get("normal_speed_ratio")
                if ratio is not None:
                    windows.append(ratio)
                if index:
                    previous = samples[index-1]
                    timestamp = row["worlds"][world].get("observed_monotonic", row["monotonic"])
                    prior = previous["worlds"][world].get("observed_monotonic", previous["monotonic"])
                    if not 4 <= timestamp-prior <= 6 or ratio is None:
                        sampling_gaps.append({"world": world, "seconds": timestamp-prior, "ratio_available": ratio is not None})
            window_minimum[world] = min(windows) if windows else None
    expected_action = max(0, math.ceil((config["duration"]-.3)*config["action_rate"]))
    expected_chat = math.ceil(config["duration"]*config["chat_rate"])
    offered = (attempts["action_ack"] >= expected_action and attempts["chat_ack"] >= expected_chat
               and attempts["action_ack"] == original["action_posts_attempted"]
               and attempts["chat_ack"] == original["chat_posts_attempted"]
               and successes["action_ack"] == attempts["action_ack"]
               and successes["chat_ack"] == attempts["chat_ack"])
    expected_deliveries = successes["chat_ack"]*config["viewers"]
    expected_pairs = {(viewer, message) for viewer in range(config["viewers"]) for message in accepted_messages}
    pair_identity_proven = (not unkeyed_deliveries and not duplicate_deliveries
                            and len(accepted_messages) == successes["chat_ack"])
    missing_pairs = len(expected_pairs-delivery_pairs) if pair_identity_proven else None
    unexpected_pairs = len(delivery_pairs-expected_pairs) if pair_identity_proven else None
    # One-cycle tolerance covers the common interval cutting an already-running
    # capture/PNG pair; the driver offers each loop every1.02seconds.
    minimum_cycles = max(0, math.floor(config["duration"]/1.02)-1)
    image_load = all(set(clients[kind]) == {prefix+str(i) for i in range(count)}
                     and all(value >= minimum_cycles for value in clients[kind].values())
                     for kind, prefix, count in (("visual_capture", "visual", 4), ("visual_png", "visual", 4),
                        ("browser_capture", "browser", config["viewers"]), ("browser_png", "browser", config["viewers"])))
    ack95 = percentile(timings["action_ack"], .95)
    chat95 = percentile(timings["chat_delivery"], .95)
    full = (config["duration"] >= 1800 and elapsed >= 1800 and config["vehicles"] == 500
            and config["viewers"] == 50 and config["worlds"] == 4
            and config["action_rate"] == 1 and config["chat_rate"] == 1
            and len(manifest["worlds"]) == 4 and config.get("map") == [512, 512])
    minimum_active = min((row["active_heads"] for row in samples), default=0)
    if "steady_http_started" in original:
        # Reconcile even calls that were pending at the provisional summary.
        inflight_proven = (not original.get("inflight_generators")
                           and all(attempts[kind] == count for kind, count in original["steady_http_started"].items()))
    else:
        inflight_proven = ("unresolved_steady_http" in original and "inflight_generators" in original
                           and not original["unresolved_steady_http"] and not original["inflight_generators"])
    checks = {"full_target_configuration": full, "run_finished": completed,
        "timed_action_chat_offered": offered, "image_workload_offered": image_load,
        "steady_inflight_denominator_proven": inflight_proven, "no_http_or_client_failures": not failures,
        "five_second_sampling_coverage": not sampling_gaps, "500_active_heads": minimum_active >= 500,
        "chat_pair_identity_proven": pair_identity_proven,
        "chat_no_missing_or_unexpected_pairs": pair_identity_proven and not missing_pairs and not unexpected_pairs,
        "chat_delivery_total": deliveries == expected_deliveries, "50_chat_viewers": viewers == set(range(50)),
        "whole_interval_normal_speed_lower_bound": len(speed_bound) == 4 and all(ratio >= .95 for ratio in speed_bound.values()),
        "sampled_window_normal_speed": len(window_minimum) == 4 and all(ratio is not None and ratio >= .95 for ratio in window_minimum.values()),
        "action_ack_p95": ack95 is not None and ack95 < .5, "chat_delivery_p95": chat95 is not None and chat95 < 1,
        "no_action_chat_cadence_loss": not cadence["chat"] and not cadence["action"]}
    passed = all(checks.values())
    gauges = ("backend_pending_actions", "backend_pending_chat", "active_heads", "moving_now", "connected_sse")
    completion_counts = None
    database = Path(manifest["worlds"][0]["directory"]) / "world.sqlite3"
    if measured_start_wall is not None and database.is_file():
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2) as connection:
            completion_counts = dict(connection.execute(
                "SELECT status,COUNT(*) FROM action_requests WHERE created_at>=? AND created_at<=? "
                "AND json_extract(payload,'$.action.name')='company.rename' GROUP BY status",
                (measured_start_wall, measured_start_wall+elapsed)))
    result = {"analysis_version": 1, "postprocessed": True, "original_summary_preserved": True,
        "result_label": "TARGET_PASS" if passed else "TARGET_NOT_MET" if full else "NON_TARGET_SMOKE",
        "target_pass": passed, "full_target_configuration": full, "run_finished": completed,
        "checks": checks, "failed_checks": [name for name, valid in checks.items() if not valid],
        "steady_start_monotonic": measured_start, "steady_seconds": elapsed,
        "expected_action_posts": expected_action, "expected_chat_posts": expected_chat,
        "http_attempts": dict(attempts), "http_successes": dict(successes), "http_failures": failures,
        "successful_http_by_client": {kind: dict(counts) for kind, counts in clients.items()},
        "process_rss_peak_bytes": dict(memory_peaks),
        "memory_method": "Kernel VmHWM since process startup when available; legacy rows use maximum sampledRSS. Includes initial burst and separately labelled faults.",
        "offered_load_complete": offered, "chat_expected_deliveries": expected_deliveries,
        "steady_inflight_denominator_proven": inflight_proven,
        "inflight_limit": "Legacy drivers without start/inflight counters cannot establish a complete outstanding-request denominator and cannot receive TARGET_PASS.",
        "image_load_complete": image_load, "visual_capture_clients": len(visual_clients),
        "minimum_capture_cycles_per_client": minimum_cycles,
        "chat_observed_deliveries": deliveries, "chat_missing_deliveries": missing_pairs,
        "chat_unexpected_deliveries": unexpected_pairs, "chat_viewers_observed": len(viewers),
        "chat_pair_identity_proven": pair_identity_proven,
        "chat_deliveries_by_viewer": dict(deliveries_by_viewer),
        "chat_aggregate_delivery_deficit": max(0, expected_deliveries-deliveries),
        "chat_duplicate_pairs": duplicate_deliveries,
        "chat_identity_limit": "Legacy records without message_key support aggregate/per-viewer counts and driver dedupe by(viewer,body), not independent message-pair reconstruction.",
        "latencies": {name: {"samples": len(values), "p50_seconds": percentile(values, .5),
            "p95_seconds": percentile(values, .95), "max_seconds": max(values)} for name, values in timings.items() if values},
        "whole_interval_normal_speed_lower_bound": speed_bound,
        "observed_interior_minimum_five_second_speed_ratio": window_minimum,
        "sampling_gaps": sampling_gaps, "sampling_interval_bounds_seconds": [4, 6],
        "speed_method": "Total lower bound drops first/last steady samples and divides observed interior tick delta by full actual steady_seconds. Window minima include every completed steady sample and require4..6s spacing (per-world observed time when available, otherwise row-time proxy). Unobserved edges receive zero credit; first/final short-window minima are unproven.",
        "minimum_active_heads": minimum_active, "unique_frames_per_view": original.get("unique_frames_per_view"),
        "cadence_missed_by_producer": dict(cadence),
        "cadence_limit": "Earlier unlabelled misses may belong to images/sampler; exact action/chat attempt denominators independently gate offered load.",
        "timed_gameplay_backend_status_counts": completion_counts,
        "completion_status_method": "Read-only backend ledger company.rename records created during the timed wall interval; completion is excluded from ACK latency and this status view is separate.",
        "sampled_ranges": {name: {"min": min(row[name] for row in samples), "max": max(row[name] for row in samples)} for name in gauges} if samples else {},
        "scope": "50 browser-protocol clients/four view credentials; primary road load plus three one-bus independent worlds; POST-to-SSE chat only, no native recipient acknowledgement; fault proof separate.",
        "evidence_sha256": {name: hashlib.file_digest((directory / name).open("rb"), "sha256").hexdigest()
            for name in ("configuration.json", "manifest.json", "summary.json", "samples.jsonl")}}
    result["analysis_source_sha256"] = hashlib.file_digest(Path(__file__).open("rb"), "sha256").hexdigest()
    write_json(directory / "audited-summary.json", result)
    return result


def main(argv=None):
    os.umask(0o077)
    args = parse_args(argv)
    if args.analyze:
        result = analyze_results(args.analyze)
        print(json.dumps(result, indent=2), flush=True)
        return 0 if result["target_pass"] or not result["full_target_configuration"] else 2
    args.output.mkdir(mode=0o700, parents=True)
    fixture_provenance = None
    if args.initial_save:
        args.initial_save, fixture_provenance = copy_initial_fixture(args)
    args.fixture_provenance = fixture_provenance
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    metrics = Metrics(args.output)
    groups, load = [], None
    completed = False
    metadata = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    metadata.update({"platform": platform.platform(), "python": sys.version, "logical_cpus": os.cpu_count(),
        "cpuinfo": Path("/proc/cpuinfo").read_text(), "meminfo": Path("/proc/meminfo").read_text(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)), "cgroup_limits": {
            name: Path("/sys/fs/cgroup", name).read_text().strip()
            for name in ("cpu.max", "memory.max", "cpuset.cpus.effective")
            if Path("/sys/fs/cgroup", name).is_file()},
        "binary_sha256": hashlib.file_digest(args.binary.open("rb"), "sha256").hexdigest(),
        "driver_sha256": hashlib.file_digest(Path(__file__).open("rb"), "sha256").hexdigest(),
        "backend_runtime_sha256": {str(path.relative_to(ROOT)): hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
            for path in sorted([*ROOT.glob("backend/*.py"), *ROOT.glob("backend/schemas/*.json")])},
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
        "tick_seconds": TICK_SECONDS, "transport": "real loopback HTTP/1.1 ASGI", "map": [512, 512],
        "view_credentials": 4, "browser_connections": args.viewers, "visual_agents": 4,
        "loan_ceiling": 2000000, "native_queue_depth": "not instrumented; backend pending depth is separate",
        "world_loads": "primary road load; other worlds one normally ordered bus each",
        "mixed_control": "four visual-agent views, two text-agent companies, two human-labelled companies; synthetic input, no model inference"})
    write_json(args.output / "configuration.json", metadata)
    try:
        for i in range(args.worlds):
            group = Group(args, i, metrics)
            groups.append(group)
            print(f"Starting world {i}: {group.world.directory}", flush=True)
            group.start()
            group.seed_routes(args.vehicles if i == 0 else 1)
        write_json(args.output / "manifest.json", {"worlds": [{"id": g.world.world_id,
            "directory": str(g.world.directory), "native_port": g.world.port, "api_port": g.api_port,
            "processes": {name: process.pid for name, process in g.processes()}, "routes": g.routes,
            "config": g.world.config_file.read_text()} for g in groups]})
        load = Load(args, metrics, groups)
        load.start()
        deadline = time.monotonic()+60
        while len(metrics.clients) < args.viewers:
            if time.monotonic() >= deadline:
                raise RPCError(f"Only {len(metrics.clients)}/{args.viewers} SSE clients became ready")
            time.sleep(.1)
        metrics.phase = "warmup"
        time.sleep(args.warmup)
        load.sample()
        if args.burst_images and len(load.burst_completed) != args.viewers+4:
            raise RPCError(f"Synchronized PNG burst completed only{len(load.burst_completed)}/{args.viewers+4} clients")
        if load.last_active != args.vehicles:
            raise RPCError("Target vehicles were not active before measurement")
        thread = groups[0].http[0].request("POST", "/v1/threads", {"scope": "world"})
        thread_id = thread.get("id", thread.get("thread_id"))
        if not thread_id:
            raise RPCError(f"Missing world chat thread: {thread}")
        metrics.phase = "steady"
        load.begin_steady()
        started = time.monotonic()
        print(f"STEADY_START wall={time.time()} monotonic={started} duration={args.duration} output={args.output}", flush=True)
        metrics.emit("steady_started", duration=args.duration)
        sequence = Counter()
        def send_chat():
            if metrics.phase != "steady":
                return
            sequence["chat"] += 1
            load.chat(thread_id, sequence["chat"])
        def send_action():
            if metrics.phase != "steady":
                return
            sequence["action"] += 1
            load.admission(sequence["action"])
        chat_worker = load.thread(load.periodic, 1/args.chat_rate, send_chat, 0, "chat", True)
        action_worker = load.thread(load.periodic, 1/args.action_rate, send_action, .3, "action", True)
        while time.monotonic()-started < args.duration:
            time.sleep(min(.5, max(.01, args.duration-(time.monotonic()-started))))
        elapsed = time.monotonic()-started
        metrics.phase = "drain"
        ending_ticks = load.sample(freeze=True)
        load.minimum_active = min(load.minimum_active, load.last_active)
        # Keep SSE alive while bounded in-flight admission calls drain. A missing
        # final timeout must remain in the denominator, including its error.
        chat_worker.join(timeout=17)
        action_worker.join(timeout=17)
        if chat_worker.is_alive() or action_worker.is_alive():
            metrics.emit("inflight_request_error", generators_still_running=True)
        drain_deadline = time.monotonic()+20
        while metrics.pending_http("steady") and time.monotonic() < drain_deadline:
            time.sleep(.05)
        time.sleep(3)
        summary = metrics.summarize()
        total_speed = {}
        for group in groups:
            first = load.steady_ticks.get(group.index)
            last = ending_ticks.get(group.index)
            total_speed[group.index] = None if first is None or last is None or last[0] <= first[0] else (last[1]-first[1])*TICK_SECONDS/(last[0]-first[0])
        summary.update({"steady_seconds": elapsed, "requested_seconds": args.duration,
            "minimum_active_heads": load.minimum_active, "minimum_sampled_sse_connections": load.minimum_streams,
            "total_normal_speed_ratio": total_speed, "minimum_five_second_speed_ratio": {
                index: min(values) if values else None for index, values in load.speeds.items()},
            "unique_frames_per_view": {index: len(values) for index, values in load.unique_images.items()},
            "chat_posts_attempted": sequence["chat"], "action_posts_attempted": sequence["action"],
            "expected_chat_posts": math.ceil(args.duration*args.chat_rate),
            "expected_action_posts": max(0, math.ceil((args.duration-.3)*args.action_rate)),
            "inflight_generators": int(chat_worker.is_alive())+int(action_worker.is_alive()),
            "unresolved_steady_http": metrics.pending_http("steady"),
            "steady_http_started": metrics.started_http_counts("steady"),
            "full_target_configuration": args.duration >= 1800 and args.vehicles == 500 and args.viewers == 50 and args.worlds == 4 and args.action_rate == 1 and args.chat_rate == 1})
        summary["result_label"] = "AWAITING_AUDIT" if summary["full_target_configuration"] else "NON_TARGET_SMOKE"
        summary["authoritative_verdict"] = "audited-summary.json after all raw records close"
        write_json(args.output / "summary.json", summary)
        print(json.dumps(summary, indent=2), flush=True)
        if args.fault_seconds:
            # Existing viewers remain connected; a stop event is never cleared
            # while an old in-flight request might still hold it.
            write_json(args.output / "fault-summary.json", load.faults())
        load.close()
        completed = True
    except (Exception, KeyboardInterrupt) as error:
        metrics.emit("run_failed", error=str(error)[:2000])
        write_json(args.output / "failure.json", {"error": str(error), "full_target_completed": False})
        print(f"Benchmark failed: {error}; artifacts: {args.output}", file=sys.stderr, flush=True)
        return 1
    finally:
        if load:
            load.close()
        for group in reversed(groups):
            group.stop()
        metrics.emit("run_finished", completed=completed)
        metrics.file.close()
    result = analyze_results(args.output)
    print(json.dumps({"result_label": result["result_label"], "target_pass": result["target_pass"],
                      "authoritative_report": str(args.output / "audited-summary.json")}), flush=True)
    return 0 if result["target_pass"] or not result["full_target_configuration"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
