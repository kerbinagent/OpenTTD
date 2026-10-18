"""Portable public player CLI; requires only Python's standard library.

Run ``python -m backend.agent_client --state PRIVATE_DIR new --origin ORIGIN
--mode text --name NAME`` once, then ``--state PRIVATE_DIR resume`` in a later
session. Keep that private state for every command. Capture
commands save PNGs: the model host must open them before acting on their content.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from urllib.parse import unquote, urlsplit
import uuid

from .capture_tools import CaptureTools
from .client import BackendClient, BackendError
from .json_tools import dumps, loads
from .player_actions import IDENTIFIER, SavedActions
from .player_mechanics import prepare_order, rail_track, road_bits


MAX_STATE_BYTES = 16 * 1024
MAX_LOG_BYTES = 4 * 1024 * 1024
TERMINAL = {"completed", "rejected", "uncertain", "retired", "resolved_rolled_back"}

def _identifier(value):
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid request ID")
    return value


def _origin(value):
    # Reuse the public transport's credential and redirect policy.
    checked = BackendClient(value, "origin-validation").base_url
    parsed = urlsplit(checked)
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    suffix = "" if port in (None, 80 if parsed.scheme == "http" else 443) else ":" + str(port)
    return parsed.scheme + "://" + host + suffix


def _private_directory(path, *, create=True):
    path = Path(path).expanduser().absolute()
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077)):
        raise ValueError("State directory must be an owner-only directory, not a symlink")
    return path


def _sync_directory(directory):
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_private(path, data, *, create=False):
    temporary = path if create else path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if not create:
            os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if not create:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _read_private(path, maximum=MAX_STATE_BYTES):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077)):
            raise ValueError("Private state must be an owner-only regular file")
        data = source.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Private state exceeds its size limit")
    return data


def _company_name(value):
    if (not isinstance(value, str) or not value or len(value) > 31
            or "\x00" in value or len(value.encode("utf-8")) > 128):
        raise ValueError("Choose a nonempty company name of at most 31 Unicode characters and 128 UTF-8 bytes")
    return value


def _registration(value, binding):
    fields = ("world_epoch", "registration_id")
    if (not isinstance(value, dict)
            or any(not isinstance(value.get(field), str) or not value[field]
                   or len(value[field]) > 256 for field in fields)
            or value["registration_id"] != binding["run_id"]):
        raise ValueError("Invalid dynamic company registration")
    return {field: value[field] for field in fields}


def _session(directory):
    value = loads(_read_private(directory / "session.json").decode("utf-8"))
    if (not isinstance(value, dict) or set(value) - {"version", "origin", "mode", "claim", "binding", "token", "verified",
                                                                 "create_intent", "registration", "naming"}
            or value.get("version") not in (1, 2) or value.get("mode") not in ("text", "visual")
            or not isinstance(value.get("claim"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value["claim"])
            or not isinstance(value.get("origin"), str)
            or _origin(value.get("origin")) != value["origin"]):
        raise ValueError("Invalid saved session; retain it for recovery and use another private directory for a new player")
    if base64.urlsafe_b64encode(base64.urlsafe_b64decode(value["claim"] + "=")).decode().rstrip("=") != value["claim"]:
        raise ValueError("Invalid saved admission claim")
    if value["version"] == 1:
        if set(value) & {"create_intent", "registration", "naming"}:
            raise ValueError("Invalid legacy session")
    else:
        intent = value.get("create_intent")
        if not isinstance(intent, dict) or set(intent) != {"name", "request_id"}:
            raise ValueError("Invalid saved company creation intent")
        _company_name(intent["name"])
        _identifier(intent["request_id"])
        if "token" in value and (not isinstance(value["token"], str) or value["token"] != value["claim"]):
            raise ValueError("Saved dynamic credential does not match its registration claim")
        if "binding" in value:
            _registration(value.get("registration"), _binding(value["binding"], value["mode"]))
    return value


def _binding(value, mode):
    fields = ("world_id", "company_id", "identity_id", "run_id", "mode")
    if (not isinstance(value, dict) or value.get("mode") != mode
            or type(value.get("company_id")) is not int or not 0 <= value["company_id"] <= 14
            or any(not isinstance(value.get(field), str) or not value[field] for field in fields if field != "company_id")):
        raise ValueError("Invalid company binding")
    return {field: value[field] for field in fields}




class AgentClient:
    """An independent public player, with durable credentials and exact intents.

    Own state is private and may contain chat, chosen action parameters and their
    public replies. It is not a filesystem sandbox. Use one state directory per
    player and avoid concurrent writers to the same directory.
    """

    def __init__(self, directory, session, *, token=None):
        self.state = _private_directory(directory)
        self.session = session
        self.client = BackendClient(session["origin"], token or session["token"])
        self.client.on_http_attempt = self._http_attempt
        self.actions = SavedActions(self.state / "requests")
        self.images = _private_directory(self.state / "images")
        self.capture_tools = CaptureTools(self.client, self.state)
        self._setup = None

    @classmethod
    def join(cls, origin, state, mode):
        origin = _origin(origin)
        if mode not in ("text", "visual"):
            raise ValueError("Mode must be text or visual")
        state = _private_directory(state)
        try:
            saved = _session(state)
        except FileNotFoundError:
            saved = {"version": 1, "origin": origin, "mode": mode, "claim": secrets.token_urlsafe(32)}
            # Creation is exclusive and durable before even attempting HTTP.
            try:
                _write_private(state / "session.json", dumps(saved).encode(), create=True)
            except FileExistsError:
                saved = _session(state)
        if saved["version"] != 1:
            raise ValueError("This state has an explicit new-company intent; recover with the same new command or resume")
        if saved["origin"] != origin or saved["mode"] != mode:
            raise ValueError("Saved player belongs to a different origin or mode; choose a new private directory")
        player = cls(state, saved, token=saved.get("token", saved["claim"]))
        if "token" not in saved:
            # The claim is only an admission capability. Do not log its body or
            # reply, and do not expose either secret to the command output.
            reply = player.client.request("POST", "/v1/agent/join", {"mode": mode})
            binding = _binding(reply, mode)
            token = reply.get("token")
            if (reply.get("api_version") != "v1" or not isinstance(token, str)
                    or not token or len(token) > 1024 or any(char.isspace() for char in token)):
                raise ValueError("Invalid private join response")
            saved = {**saved, "binding": binding, "token": token, "verified": False}
            _write_private(state / "session.json", dumps(saved).encode())
            player = cls(state, saved)
        elif not isinstance(saved.get("token"), str):
            raise ValueError("Invalid saved credential")
        me = player.request("GET", "/v1/me")
        if _binding(me, mode) != saved.get("binding") or me.get("kind") != "agent":
            raise ValueError("Authenticated company does not match the saved join binding")
        saved["verified"] = True
        _write_private(state / "session.json", dumps(saved).encode())
        player.session = saved
        return player

    @classmethod
    def new(cls, origin, state, mode, name):
        """Create one company, or recover this exact durably saved creation."""
        origin = _origin(origin)
        if mode not in ("text", "visual"):
            raise ValueError("Mode must be text or visual")
        name = _company_name(name)
        state = _private_directory(state)
        fresh = False
        try:
            saved = _session(state)
        except FileNotFoundError:
            # Refuse unrelated state before the constructor creates journals.
            if any(state.iterdir()):
                raise ValueError("New company needs an empty private directory")
            saved = {"version": 2, "origin": origin, "mode": mode,
                     "claim": secrets.token_urlsafe(32),
                     "create_intent": {"name": name, "request_id": uuid.uuid4().hex}}
            try:
                _write_private(state / "session.json", dumps(saved).encode(), create=True)
                fresh = True
            except FileExistsError:
                saved = _session(state)
        if (saved["version"] != 2 or saved["origin"] != origin or saved["mode"] != mode
                or saved["create_intent"]["name"] != name):
            raise ValueError("Saved state has a different creation intent; retain it and choose a new private directory")
        player = cls(state, saved, token=saved.get("token", saved["claim"]))
        if "token" not in saved:
            # Admission secrets/replies never pass through the public audit.
            reply = player.client.request("POST", "/v1/agent/join", {"mode": mode, "intent": "new"})
            binding = _binding(reply, mode)
            registration = _registration(reply, binding)
            if reply.get("api_version") != "v1" or reply.get("token") != saved["claim"]:
                raise ValueError("Invalid private dynamic registration response")
            saved = {**saved, "binding": binding, "registration": registration,
                     "token": saved["claim"], "verified": False}
            _write_private(state / "session.json", dumps(saved).encode())
            player = cls(state, saved)
        player._verify()
        intent = saved["create_intent"]
        body = {"request_id": intent["request_id"], "action": {
            "name": "company.rename", "parameters": {"name": intent["name"]}}}
        player.actions.save(body)
        try:
            # Existing exact-body retries are already idempotent, including a
            # lost naming reply. Never allocate another setup request ID.
            receipt = player.retry(intent["request_id"])
            naming = {"request_id": intent["request_id"], "status": receipt["status"]}
            result = receipt.get("result")
            if isinstance(result, dict):
                if type(result.get("success")) is bool:
                    naming["success"] = result["success"]
                if isinstance(result.get("error"), str):
                    naming["error"] = result["error"][:512]
        except BackendError as error:
            naming = {"request_id": intent["request_id"], "status": "unavailable",
                      "error": {"status": error.status, "code": error.code}}
        except (OSError, TimeoutError) as error:
            naming = {"request_id": intent["request_id"], "status": "transport_unknown",
                      "error": type(error).__name__}
        player.session["naming"] = naming
        _write_private(state / "session.json", dumps(player.session).encode())
        player._setup = player._setup_result("new" if fresh else "recovered_new")
        return player

    @classmethod
    def resume(cls, state):
        """Verify existing company authority, without admission or naming."""
        state = _private_directory(state, create=False)
        saved = _session(state)
        if not isinstance(saved.get("token"), str) or "binding" not in saved:
            raise ValueError("Creation is incomplete; repeat the exact saved new or legacy join command")
        _binding(saved["binding"], saved["mode"])
        player = cls(state, saved)
        player._verify()
        player._setup = player._setup_result("resumed")
        return player

    def _verify(self):
        saved = self.session
        me = self.request("GET", "/v1/me")
        if _binding(me, saved["mode"]) != saved.get("binding") or me.get("kind") != "agent":
            raise ValueError("Authenticated company does not match the saved binding")
        if saved["version"] == 2 and _registration(me, saved["binding"]) != saved["registration"]:
            raise ValueError("Authenticated registration does not match the saved world")
        saved["verified"] = True
        _write_private(self.state / "session.json", dumps(saved).encode())

    def _setup_result(self, operation):
        result = {"operation": operation, "binding": self.binding}
        if "registration" in self.session:
            result.update(self.session["registration"])
        if "naming" in self.session:
            result["naming"] = self.session["naming"]
        elif "create_intent" in self.session:
            result["naming"] = {"request_id": self.session["create_intent"]["request_id"], "status": "not_started"}
        return deepcopy(result)

    @property
    def setup(self):
        """Public setup summary; naming state is advisory, not a new journal."""
        return self.safe_output(deepcopy(self._setup or self._setup_result("opened")))

    @classmethod
    def open(cls, state):
        state = _private_directory(state)
        saved = _session(state)
        if saved.get("verified") is not True or not isinstance(saved.get("token"), str):
            raise ValueError("Setup is incomplete; run resume if a token/binding was saved, otherwise repeat the exact new or legacy join command")
        _binding(saved.get("binding"), saved["mode"])
        return cls(state, saved)

    @property
    def binding(self):
        return deepcopy(self.session["binding"])

    def safe_output(self, value):
        """Remove this player's two known secrets from displayed/audited JSON."""
        encoded = dumps(value)
        for field in ("claim", "token"):
            secret = self.session.get(field)
            if isinstance(secret, str) and secret:
                encoded = encoded.replace(secret, "[redacted]")
        return loads(encoded)

    def record(self, value):
        """Bounded own-player audit. Admission secrets are never passed here."""
        try:
            line = (dumps(self.safe_output({"time": time.time(), **value})) + "\n").encode()
            if len(line) > MAX_LOG_BYTES:
                return
            path = self.state / "public-io.jsonl"
            if path.exists() and path.stat().st_size + len(line) > MAX_LOG_BYTES:
                os.replace(path, path.with_suffix(".jsonl.1"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "ab") as target:
                target.write(line)
        except (OSError, TypeError, ValueError):
            # Optional measurement must not replace a known gameplay result.
            pass

    def _http_attempt(self, measurement):
        self.record({"event": "http_attempt", **measurement})
        try:
            path = self.state / "http-counts.json"
            try:
                counts = loads(_read_private(path).decode())
            except FileNotFoundError:
                counts = {"attempts": 0, "by_method_path": {}, "by_status": {}}
            counts["attempts"] += 1
            counted_path = re.sub(r"^(/v1/(?:actions|images))/[^/]+$", r"\1/:id", measurement["path"])
            counted_path = re.sub(r"^(/v1/chat/threads)/[^/]+", r"\1/:id", counted_path)
            key = measurement["method"] + " " + counted_path
            if key not in counts["by_method_path"] and len(counts["by_method_path"]) >= 100:
                key = "other"
            counts["by_method_path"][key] = counts["by_method_path"].get(key, 0) + 1
            key = str(measurement["status"])
            counts["by_status"][key] = counts["by_status"].get(key, 0) + 1
            _write_private(path, dumps(counts).encode())
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def request(self, method, path, body=None):
        if not isinstance(path, str):
            raise ValueError("Use a public /v1 API path")
        method = method.upper()
        route = urlsplit(path)
        decoded = unquote(route.path).rstrip("/")
        if (not isinstance(path, str) or not path.startswith("/v1/") or route.netloc or route.fragment
                or decoded == "/v1/agent/join"):
            raise ValueError("Use a public /v1 API path; admission is handled only by join")
        if method == "POST" and decoded == "/v1/actions":
            if route.query:
                raise ValueError("Action submission does not accept a query")
            return self.submit(body, wait_ms=0)
        self.record({"request": {"method": method, "path": path, "body": body}})
        try:
            reply = self.client.request(method, path, body)
        except BackendError as error:
            self.record({"error": {"status": error.status, "code": error.code}})
            raise
        except (OSError, TimeoutError) as error:
            self.record({"transport_error": type(error).__name__})
            raise
        self.record({"response": reply})
        return reply

    def observe(self, query, parameters=None):
        return self.request("POST", "/v1/observations", {"query": query, "parameters": parameters or {}})

    @staticmethod
    def _wait_budget(wait_ms):
        if type(wait_ms) is not int or not 0 <= wait_ms <= 20_000:
            raise ValueError("wait_ms must be an integer from 0 to 20000")
        return wait_ms

    def status(self, request_id):
        return self.request("GET", "/v1/actions/" + _identifier(request_id))

    def wait(self, request_id, wait_ms=20_000, *, receipt=None):
        _identifier(request_id)
        deadline = time.monotonic() + self._wait_budget(wait_ms) / 1000
        while True:
            if receipt is None:
                receipt = self.status(request_id)
            if (not isinstance(receipt, dict) or receipt.get("request_id") != request_id
                    or receipt.get("status") not in TERMINAL | {"accepted", "queued"}):
                raise ValueError("Invalid action receipt")
            if receipt.get("status") in TERMINAL or time.monotonic() >= deadline:
                return receipt
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            receipt = None

    def submit(self, body, wait_ms=20_000):
        self._wait_budget(wait_ms)
        fresh = self.actions.save(body)
        body = self.actions.load(body["request_id"])
        self.record({"submission": {"request_id": body["request_id"], "kind": "new_intent" if fresh else "exact_replay"}})
        self.record({"request": {"method": "POST", "path": "/v1/actions", "body": body}})
        try:
            reply = self.client.request("POST", "/v1/actions", body)
        except BackendError as error:
            self.record({"error": {"status": error.status, "code": error.code}})
            raise
        except (OSError, TimeoutError) as error:
            self.record({"transport_error": type(error).__name__})
            raise
        self.record({"response": reply})
        return self.wait(body["request_id"], wait_ms, receipt=reply)

    def action(self, name, parameters, *, request_id=None, preconditions=None, wait_ms=20_000):
        body = {"request_id": request_id if request_id is not None else uuid.uuid4().hex,
                "action": {"name": name, "parameters": parameters}}
        if preconditions is not None:
            body["preconditions"] = preconditions
        return self.submit(body, wait_ms)

    def retry(self, request_id, wait_ms=20_000):
        """Resend the original saved body. Never resolve new targets/tokens."""
        return self.submit(self.actions.load(request_id), wait_ms)

    def prepare_order(self, name, vehicle_ref, parameters, *, source_ref=None):
        """Save a new explicit order intent; retry never refreshes its tokens."""
        return prepare_order(self, self.actions, name, vehicle_ref, parameters, source_ref=source_ref)

    def _image(self, metadata, data):
        image_id = metadata.get("image_id")
        if not isinstance(image_id, str) or not re.fullmatch(r"[0-9a-f]{32}", image_id):
            raise ValueError("Invalid image identity")
        path = self.images / (image_id + ".png")
        _write_private(path, data)
        result = {**metadata, "image_path": str(path)}
        self.record({"image": {"image_id": image_id, "bytes": len(data), "path": str(path),
                               "sha256": hashlib.sha256(data).hexdigest()}})
        return result

    def _selected_result(self, name, arguments):
        self.record({"capture_request": {"tool": name, "arguments": arguments}})
        result = self.capture_tools.call(name, arguments)
        metadata = self.safe_output(result["structuredContent"])
        image = next((part for part in result["content"] if part["type"] == "image"), None)
        if image is not None:
            metadata = self._image(metadata, base64.b64decode(image["data"], validate=True))
        self.record({"capture_response": metadata})
        return metadata

    def selected(self, kind, parameters, *, key=None):
        if kind not in ("world", "native_info") or not isinstance(parameters, dict):
            raise ValueError("Choose world or native_info and a parameter object")
        if "observation_key" in parameters:
            raise ValueError("Supply key once, as the separate argument")
        return self._selected_result("observe_" + kind, {**parameters, **({"observation_key": key} if key is not None else {})})

    def retry_observation(self, observation_request_id):
        return self._selected_result("retry_observation", {"observation_request_id": observation_request_id})

    def capture(self, label="view"):
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label):
            raise ValueError("Image label must be a short plain filename")
        metadata = self.request("POST", "/v1/view/images", {})
        data = self.client.image(metadata["image_id"])
        result = self._image(metadata, data)
        _write_private(self.images / ("capture-" + label + ".json"), dumps(result).encode())
        return result

    def control(self, image_id, parameters):
        if not isinstance(parameters, dict) or "image_id" in parameters:
            raise ValueError("Supply image_id once as the separate argument")
        return self.request("POST", "/v1/view/controls", {**parameters, "image_id": image_id})

    def ui(self, image_id, parameters, *, request_id=None, wait_ms=20_000):
        if not isinstance(parameters, dict) or "image_id" in parameters:
            raise ValueError("Supply image_id once as the separate argument")
        return self.action("ui.apply", {**parameters, "image_id": image_id}, request_id=request_id, wait_ms=wait_ms)


def _json_object(value):
    try:
        result = loads(value)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("Expected a JSON object") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    join = commands.add_parser("join")
    join.add_argument("--origin", required=True)
    join.add_argument("--mode", required=True, choices=("text", "visual"))
    command = commands.add_parser("new")
    command.add_argument("--origin", required=True)
    command.add_argument("--mode", required=True, choices=("text", "visual"))
    command.add_argument("--name", required=True)
    commands.add_parser("resume")
    for name in ("get", "post", "put", "delete"):
        command = commands.add_parser(name)
        command.add_argument("path")
        if name in ("post", "put"):
            command.add_argument("body", type=_json_object)
    command = commands.add_parser("observe")
    command.add_argument("query")
    command.add_argument("parameters", nargs="?", type=_json_object, default={})
    command = commands.add_parser("action")
    command.add_argument("name")
    command.add_argument("parameters", type=_json_object)
    command.add_argument("--id")
    command.add_argument("--preconditions", type=_json_object)
    command.add_argument("--wait-ms", type=int, default=20_000)
    for name in ("status", "retry", "wait"):
        command = commands.add_parser(name)
        command.add_argument("request_id")
        if name != "status":
            command.add_argument("--wait-ms", type=int, default=20_000)
    command = commands.add_parser("prepare-order")
    command.add_argument("name")
    command.add_argument("vehicle", type=_json_object)
    command.add_argument("parameters", type=_json_object)
    command.add_argument("--source", type=_json_object)
    command = commands.add_parser("selected")
    command.add_argument("kind", choices=("world", "native_info"))
    command.add_argument("parameters", type=_json_object)
    command.add_argument("--key")
    commands.add_parser("retry-observation").add_argument("observation_request_id")
    commands.add_parser("capture").add_argument("label", nargs="?", default="view")
    for name in ("control", "ui"):
        command = commands.add_parser(name)
        command.add_argument("image_id")
        command.add_argument("parameters", type=_json_object)
        if name == "ui":
            command.add_argument("--id")
    args = parser.parse_args(argv)
    try:
        _run(args)
    except BackendError as error:
        # The server controls its code as well as its message. Admission can
        # fail before a player exists, so load only already saved private
        # state here; do not construct a client or create recovery state.
        code = str(error.code)
        try:
            saved = _session(_private_directory(args.state, create=False))
            for field in ("claim", "token"):
                secret = saved.get(field)
                if isinstance(secret, str) and secret and secret in code:
                    code = "backend_error"
                    break
        except (ValueError, OSError):
            code = "backend_error"
        if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) is None:
            code = "backend_error"
        raise BackendError(error.status, code, "Client request failed") from None


def _run(args):
    if args.command == "new":
        player = AgentClient.new(args.origin, args.state, args.mode, args.name)
        result = player.setup
    elif args.command == "resume":
        player = AgentClient.resume(args.state)
        result = player.setup
    elif args.command == "join":
        player = AgentClient.join(args.origin, args.state, args.mode)
        result = player.binding
    else:
        player = AgentClient.open(args.state)
        if args.command in ("get", "post", "put", "delete"):
            result = player.request(args.command, args.path, getattr(args, "body", None))
        elif args.command == "observe":
            result = player.observe(args.query, args.parameters)
        elif args.command == "action":
            result = player.action(args.name, args.parameters, request_id=args.id,
                                   preconditions=args.preconditions, wait_ms=args.wait_ms)
        elif args.command in ("status", "retry", "wait"):
            result = (player.status(args.request_id) if args.command == "status" else
                      getattr(player, args.command)(args.request_id, args.wait_ms))
        elif args.command == "prepare-order":
            result = player.prepare_order(args.name, args.vehicle, args.parameters, source_ref=args.source)
        elif args.command == "selected":
            result = player.selected(args.kind, args.parameters, key=args.key)
        elif args.command == "retry-observation":
            result = player.retry_observation(args.observation_request_id)
        elif args.command == "capture":
            result = player.capture(args.label)
        elif args.command == "control":
            result = player.control(args.image_id, args.parameters)
        elif args.command == "ui":
            result = player.ui(args.image_id, args.parameters, request_id=args.id)
    print(json.dumps(player.safe_output(result), allow_nan=False, indent=2), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BackendError as error:
        # Error text is server-controlled; admission errors must never echo a
        # credential or response body through the helper's output.
        print(json.dumps({"error": {"status": error.status, "code": error.code}}), file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, OSError, TimeoutError) as error:
        print(json.dumps({"error": {"code": type(error).__name__, "message": "Client operation failed; retain the private state for exact recovery"}}), file=sys.stderr)
        raise SystemExit(1)
