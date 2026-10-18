"""One-world authenticated API and durable action broker.

Backend sequence identifies a permanent request. Native frame/tick determines
execution order. A completed action is durable in the world only when its
receipt is included in the loaded save generation.
"""

import hashlib
import re
import threading
import uuid
from types import SimpleNamespace
from urllib.parse import unquote

from .capabilities import matrix
from .catalog import CATALOG, action_spec, query_spec, validate_parameters, verify_engine_capabilities
from .checkpoint import CheckpointPolicy
from .errors import ServiceError
from .json_tools import dumps
from .observations import ObservationImage
from . import projection
from .social import Social

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ACTION = re.compile(r"^[a-z][a-z0-9_.]{0,79}$")
_NATIVE_KEY = re.compile(r"^[0-9a-fA-F]{64}$")
_TERMINAL = {"completed", "rejected", "resolved_rolled_back"}
_MAX_U64 = (1 << 64) - 1
_MAX_SQLITE_INT = (1 << 63) - 1


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ServiceError("invalid_request")
    return value


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ServiceError("invalid_request")
    return value


def _limit(query, name="limit", default=100, maximum=100):
    try:
        value = int(query.get(name, default))
    except (ValueError, TypeError, OverflowError):
        raise ServiceError("invalid_request") from None
    if value < 1 or value > maximum:
        raise ServiceError("invalid_request")
    return value


def _catalog(value):
    """Accept named ActionSpec dictionaries or the initial list of names."""
    if isinstance(value, dict):
        return {name: spec if isinstance(spec, dict) else {} for name, spec in value.items()
                if isinstance(name, str) and _ACTION.fullmatch(name)}
    if isinstance(value, list):
        result = {}
        for spec in value:
            name = spec if isinstance(spec, str) else spec.get("name") if isinstance(spec, dict) else None
            if isinstance(name, str) and _ACTION.fullmatch(name):
                result[name] = {} if isinstance(spec, str) else spec
        return result
    return {}


def _pause_from_hello(hello):
    """Require a current, ready native pause snapshot before reporting it."""
    if hello.get("recovery_required") is not False:
        raise ServiceError("world_recovering", status=503)
    return projection.world_status(hello.get("pause"))


def _native_offset(value):
    """SQLite INTEGER cannot hold the full unsigned native event range."""
    if type(value) is int and 0 <= value <= _MAX_SQLITE_INT:
        return value
    if isinstance(value, str) and value.startswith("u") and 1 <= len(value[1:]) <= 20 and value[1:].isascii() and value[1:].isdigit():
        number = int(value[1:])
        if _MAX_SQLITE_INT < number <= _MAX_U64:
            return number
    raise ServiceError("engine_unavailable", status=503)


def _stored_native_offset(value):
    return value if value <= _MAX_SQLITE_INT else "u" + str(value)


class Service:
    """One backend/database/socket per world; a proxy hosts independent stacks."""

    def __init__(self, store, engine, social=None, autostart=True, world_id=None, renderer=None, checkpoint_interval=30,
                 admission=None):
        self.store = store
        self.engine = engine
        self.renderer = renderer
        self.admission = admission
        self.social = social or Social(store)
        self.world_id = world_id
        if world_id is not None:
            self.store.enforce_single_world(world_id)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._hello_value = None
        self._bindings = set()
        self._thread = None
        self._instance = None
        self._actions = {}
        self._queries = {}
        self._chat_ready = False
        self.checkpoints = CheckpointPolicy(interval=checkpoint_interval)
        self._last_ack = 0
        self._visual_feedback_after = 0
        with self.store.lock:
            self.store.connection.executescript(
                "CREATE TABLE IF NOT EXISTS broker_offsets (world_id TEXT PRIMARY KEY, "
                "engine_instance_id TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS broker_quarantine (world_id TEXT NOT NULL, "
                "engine_instance_id TEXT NOT NULL, event_key TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY(world_id,engine_instance_id,event_key));"
            )
        if autostart:
            self.start()

    def start(self):
        if self.engine is None:
            raise ValueError("A game engine is required to start the world broker")
        if self._thread is None:
            self._thread = threading.Thread(target=self._dispatch_loop, name="world-broker", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _authorize(self, token):
        principal = self.store.authenticate(token)
        with self._lock:
            if self.world_id is None:
                self.world_id = self.store.enforce_single_world(principal.world_id)
            if principal.world_id != self.world_id:
                raise ServiceError("forbidden", status=403)
        return principal

    def _envelope(self, rpc_type, principal=None, **fields):
        world_id = principal.world_id if principal else self.world_id
        envelope = {"protocol_version": 1, "type": rpc_type, "correlation_id": str(uuid.uuid4()),
                    "world_id": world_id, "world_epoch": self.store.world_epoch(world_id)}
        if principal:
            envelope["actor"] = {"identity_id": principal.identity_id, "company_id": principal.company_id,
                                 "run_id": principal.run_id, "mode": principal.mode}
        envelope.update(fields)
        return envelope

    def _rpc(self, rpc_type, principal=None, **fields):
        envelope = self._envelope(rpc_type, principal, **fields)
        reply = self.engine.call(envelope)
        if not isinstance(reply, dict):
            raise ServiceError("engine_unavailable", status=503)
        if reply.get("world_id", envelope["world_id"]) != envelope["world_id"] or reply.get("world_epoch", envelope["world_epoch"]) != envelope["world_epoch"]:
            raise ServiceError("world_recovering", status=503)
        return reply

    def _hello(self, principal, refresh=False):
        with self._lock:
            cached = self._hello_value
        if cached and not refresh:
            return cached
        # This is the trusted backend's discovery channel. Public visual
        # capabilities are independently projected below; a visual actor must
        # not restrict the broker's recovery/chat control operations.
        try:
            reply = self._rpc("hello")
            value = reply.get("result", {})
            if reply.get("status") not in ("completed", "ok") or not isinstance(value, dict):
                raise ServiceError("engine_unavailable", status=503)
            value = dict(value)
            for key in ("engine_instance_id", "world_epoch"):
                if key not in value and key in reply:
                    value[key] = reply[key]
            if value.get("world_epoch", self.store.world_epoch(principal.world_id)) != self.store.world_epoch(principal.world_id):
                raise ServiceError("world_recovering", status=503)
            instance = value.get("engine_instance_id")
            if not isinstance(instance, str) or not 1 <= len(instance) <= 256:
                raise ServiceError("engine_unavailable", status=503)
            capabilities = value.get("capabilities", {})
            if value.get("role") != "world":
                raise ServiceError("engine_unavailable", status=503)
            verify_engine_capabilities(capabilities)
        except ServiceError:
            # Never retain an earlier compatible membership after observing an
            # failed or incompatible refresh. Historical receipt reads do not use
            # this discovery path and remain available during an upgrade.
            with self._lock:
                self._hello_value = None
                self._actions = {}
                self._queries = {}
            raise
        with self._lock:
            self._hello_value = value
            self._actions = _catalog(capabilities.get("actions", {}))
            self._queries = _catalog(capabilities.get("queries", {}))
        return value

    def _bind(self, principal, hello, *, estimate=False):
        key = (principal.run_id, hello["engine_instance_id"])
        if key in self._bindings:
            return
        reply = self._rpc("session.bind", principal, engine_instance_id=hello["engine_instance_id"],
                          display_name=principal.display_name, kind=principal.kind)
        if reply.get("status") not in ("completed", "ok"):
            if estimate:
                error = reply.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                if code == "checkpoint_busy":
                    raise ServiceError("world_checkpoint_unavailable", status=503)
                if code in ("world_recovering", "recovery_required", "engine_fence", "world_replaced"):
                    raise ServiceError("world_recovering", status=503)
                if code in ("world_unavailable", "ledger_unavailable", "authority_unavailable"):
                    raise ServiceError("engine_unavailable", status=503)
            raise ServiceError("company_unavailable", status=409)
        self._bindings.add(key)

    @staticmethod
    def _handle_identity(handle):
        if (not isinstance(handle, dict) or
                set(handle) != {"id", "generation", "engine_instance_id"} or
                type(handle["id"]) is not int or not 0 <= handle["id"] <= 0xFFFFFFFF or
                type(handle["generation"]) is not int or not 1 <= handle["generation"] <= 0xFFFFFFFFFFFFFFFF or
                not isinstance(handle["engine_instance_id"], str) or
                not 1 <= len(handle["engine_instance_id"]) <= 256):
            raise ServiceError("invalid_request")
        return (handle["id"], handle["generation"], handle["engine_instance_id"])

    def _fresh_native_humans(self, principal, hello):
        """Read a fenced roster, then classify every configured renderer worker.

        All connection handles remain ephemeral. The broker owns persistence
        of the existing key-derived social membership representation.
        """
        instance = hello["engine_instance_id"]
        roster = self._rpc("chat.roster", engine_instance_id=instance)
        if (roster.get("status") not in ("completed", "ok") or
                roster.get("engine_instance_id") != instance):
            raise ServiceError("native_roster_unavailable", status=503)
        value = roster.get("result")
        raw = value.get("clients") if isinstance(value, dict) else value
        if not isinstance(raw, list) or len(raw) > 256:
            raise ServiceError("native_roster_unavailable", status=503)
        try:
            # Normalization validates labels, company values and every
            # explicit remote handle before any worker filtering.
            self.social.normalize_native_roster(raw)
            for row in raw:
                if not _NATIVE_KEY.fullmatch(row["public_key"]):
                    raise ValueError("noncanonical native key")
                handle = row.get("native_handle")
                if handle is not None and self._handle_identity(handle)[2] != instance:
                    raise ValueError("foreign native handle")
        except (ServiceError, ValueError, KeyError, TypeError):
            raise ServiceError("native_roster_unavailable", status=503) from None

        managed = self.renderer.verified_native_worker_keys() if self.renderer else set()
        filtered = [row for row in raw if row["public_key"].lower() not in managed]
        normalized = self.social.normalize_native_roster(filtered)
        handles = {}
        for row in filtered:
            handle = row.get("native_handle")
            if handle is None:
                continue  # Local server has no active remote ClientID.
            identity = self._handle_identity(handle)
            key = row["public_key"].lower()
            if identity in handles and handles[identity] != key:
                raise ServiceError("native_roster_unavailable", status=503)
            handles[identity] = key
        return normalized, handles

    def _require_fresh_participant(self, principal, hello, participant):
        identity = self._handle_identity(participant)
        _, handles = self._fresh_native_humans(principal, hello)
        if identity not in handles:
            raise ServiceError("participant_unavailable", status=409)

    @staticmethod
    def _ui_actuation(record):
        return (record.get("mode") == "visual" and
                record.get("payload", {}).get("action", {}).get("name") == "ui.apply")

    def project_action(self, principal, record):
        result = {"request_id": record["request_id"], "status": record["status"]}
        name = record.get("payload", {}).get("action", {}).get("name")
        schema = action_spec(name)
        # Historical receipts retain their action kind even when today's
        # catalog no longer advertises its name or visual mode. The narrow
        # visual fallback projects direct command feedback without reopening
        # a structured observation channel.
        semantic_visual = (record.get("mode") == "visual" and
                           isinstance(name, str) and name != "ui.apply")
        if principal.mode == "text" or semantic_visual:
            result["checkpoint_state"] = record.get("checkpoint_state", "uncheckpointed")
            if principal.mode == "text" and record.get("tick") is not None:
                result["tick"] = record["tick"]
            if record["status"] in _TERMINAL:
                schema = schema or self._actions.get(name)
                if record.get("result") is not None:
                    result["result"] = projection.outcome(record["result"], schema,
                                                          visual=semantic_visual)
                if record.get("error") is not None:
                    result["error"] = projection.error(record["error"], visual=semantic_visual)
        return result

    def events(self, token, after=None, limit=100):
        principal = self._authorize(token)
        page = self.store.events(principal, after=after, limit=limit)
        events = []
        for event in page["events"]:
            kind, data = event.get("type"), event.get("data", {})
            if kind == "chat.message":
                data = projection.chat(data)
            elif kind == "chat.message.tick":
                data = projection.positive(data, {"message_id": str, "tick": int, "tick_authoritative": bool})
            elif kind == "action.status":
                # Re-project from durable action state, never trust an internal
                # event's extra fields. This also preserves mode on replay.
                try:
                    data = self.project_action(principal, self.store.get_request(principal, data["request_id"]))
                except (ServiceError, KeyError):
                    continue
            elif kind == "world.status":
                if not isinstance(data, dict) or data.get("world_id") != principal.world_id:
                    continue
                try:
                    if data.get("world_epoch") != self.store.world_epoch(principal.world_id):
                        continue
                    instance = data.get("engine_instance_id")
                    if not isinstance(instance, str) or not 1 <= len(instance) <= 256:
                        continue
                    projected = {"world_id": principal.world_id, "world_epoch": data["world_epoch"],
                                 "engine_instance_id": instance,
                                 "pause": projection.world_status(data.get("pause"))}
                    for marker in ("baseline", "history_gap"):
                        if marker in data:
                            if data[marker] is not True:
                                raise ServiceError("world_status_unavailable", status=503)
                            projected[marker] = True
                    data = projected
                except ServiceError:
                    continue
            else:
                continue
            events.append({"id": event["id"], "type": kind, "data": data})
        return {"events": events, "cursor": page.get("cursor")}

    def wait_for_events(self, token, after=None, timeout=1):
        principal = self._authorize(token)
        self.store.wait_for_events(principal, after, timeout)
        return self.events(token, after)

    def handle(self, method, path, token, body=None, query=None):
        if method == "POST" and path == "/v1/agent/join":
            if query:
                raise ServiceError("invalid_request")
            if self.admission is None:
                raise ServiceError("join_disabled", status=403)
            return 200, self.admission.join(token, body, self)
        principal = self._authorize(token)
        body, query = ({} if body is None else body), ({} if query is None else query)
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if any("/" in part or "\x00" in part for part in parts) or not parts or parts[0] != "v1":
            raise ServiceError("not_found", status=404)
        route = parts[1:]
        if method == "GET" and route == ["me"]:
            result = {"identity_id": principal.identity_id, "world_id": principal.world_id,
                      "company_id": principal.company_id, "run_id": principal.run_id,
                      "mode": principal.mode, "kind": principal.kind, "display_name": principal.display_name}
            report = self.store.model_reports_for_world(principal.world_id).get(principal.run_id)
            if report is not None:
                result["model_report"] = report
            return 200, result
        if route == ["me", "model"] and method in ("PUT", "DELETE"):
            if query:
                raise ServiceError("invalid_request")
            if method == "PUT":
                _object(body, {"model_name", "effort_level"}, {"model_name", "effort_level"})
            else:
                _object(body, {})
            return 200, {"model_report": self.store.set_model_report(principal, body if method == "PUT" else None)}
        if method == "GET" and route == ["world", "status"]:
            _object(body, {})
            if query:
                raise ServiceError("invalid_request")
            cursor = self.store.current_event_cursor(principal)
            hello = self._hello(principal, refresh=True)
            pause = _pause_from_hello(hello)
            return 200, {"world_id": principal.world_id, "world_epoch": hello["world_epoch"],
                         "engine_instance_id": hello["engine_instance_id"],
                         "pause": pause, "event_cursor": cursor}
        if method == "GET" and route == ["participants"]:
            visible_handles = {}
            if self.engine is not None and principal.mode in ("text", "visual"):
                try:
                    hello = self._hello(principal, refresh=True)
                    native_rows, _ = self._fresh_native_humans(principal, hello)
                    for row in native_rows:
                        if "native_handle" not in row:
                            continue
                        key = row["public_key"].lower()
                        company = row["company_id"]
                        visible_handles[(self.social.native_identity_id(principal.world_id, key),
                                         company)] = row["native_handle"]
                except ServiceError:
                    # Preserve stable participants during authority/renderer
                    # outages, but never reuse an earlier access handle.
                    pass
            people = {}
            model_reports = self.store.model_reports_for_world(principal.world_id)
            unclaimed = self.admission.unclaimed_runs() if self.admission else set()
            worker_memberships = self.social.native_memberships_for_keys(principal.world_id,
                self.renderer.managed_native_worker_keys()) if self.renderer and self.engine is not None else set()
            for member in self.store.principals_for_world(principal.world_id):
                if member.kind == "agent" and member.run_id in unclaimed:
                    continue
                if member.mode == "native" and (self.engine is None or
                        (member.identity_id, member.company_id) in worker_memberships):
                    continue
                person = {
                    "identity_id": member.identity_id, "display_name": member.display_name,
                    "kind": member.kind, "company_id": None if member.company_id == 255 else member.company_id,
                    "presence": "online" if member.mode == "native" else "unknown"}
                if member.run_id in model_reports:
                    person["model_report"] = model_reports[member.run_id]
                if principal.mode in ("text", "visual") and member.mode == "native":
                    handle = visible_handles.get((member.identity_id, member.company_id))
                    if handle is not None:
                        person["native_handle"] = handle
                people[(member.identity_id, member.company_id)] = person
            return 200, {"participants": list(people.values())}
        if method == "GET" and route == ["capabilities"]:
            try:
                hello = self._hello(principal)
                connected = True
            except ServiceError:
                connected = False
            actions = matrix(self._actions if connected else (), mode=principal.mode)
            visual_tools = ([{"name": "ui.apply", "target": "image", "gestures": ["click", "drag", "move", "text", "control"]}]
                            if connected and principal.mode == "visual" and self.renderer and self.renderer.available(principal)
                            and "ui.apply" in self._actions else [])
            result = {"api_version": "v1", "engine_connected": connected, "mode": principal.mode,
                      "actions": actions, "tools": visual_tools, "view_controls": [] if not self.renderer or not self.renderer.available(principal) else ["pan", "zoom", "inspect", "open_window", "close_windows", "capture"],
                      "observation": "screenshots" if principal.mode == "visual" else "structured",
                      "limits": {"request_bytes": 65536, "query_limit": 100},
                      "unimplemented_human_actions": CATALOG["human_only"]}
            reference = CATALOG.get("conventions", {}).get("control_reference") if connected else None
            if isinstance(reference, dict):
                result["control_reference"] = {
                    "endpoint": "/v1/control/reference", "parameters": reference["parameters"],
                    "result_fields": reference["result_fields"]}
            estimate = self._estimate_capability(principal) if connected else None
            if estimate is not None:
                result["action_estimate"] = estimate
            if connected:
                if self.renderer and self.renderer.available(principal):
                    try:
                        result["selected_observations"] = self.renderer.observation_capabilities(principal)
                    except (ServiceError, AttributeError):
                        # Legacy renderers retain their interaction snapshots.
                        pass
                try:
                    _pause_from_hello(hello)
                except ServiceError:
                    pass
                else:
                    result["world_status"] = {"endpoint": "/v1/world/status", "event": "world.status"}
            return 200, result
        if method == "POST" and route in (["view", "observe", "world"],
                                           ["view", "observe", "native-info"]):
            if query:
                raise ServiceError("invalid_request")
            if not self.renderer or not self.renderer.available(principal):
                raise ServiceError("renderer_unavailable", status=503)
            self._configure_renderer(principal)
            kind = "world" if route[-1] == "world" else "native_info"
            value = self.renderer.observe(
                principal, kind, body,
                lambda request_id: self._observation_barrier(principal, request_id))
            return (200 if isinstance(value, ObservationImage) else 202), value
        if method == "POST" and route == ["view", "images"]:
            _object(body, {})
            if not self.renderer:
                raise ServiceError("renderer_unavailable", status=503)
            self._configure_renderer(principal)
            return 201, self.renderer.capture(principal)
        if method == "POST" and route == ["view", "controls"]:
            if not self.renderer:
                raise ServiceError("renderer_unavailable", status=503)
            self._configure_renderer(principal)
            return 200, self.renderer.control(principal, body)
        if method == "POST" and route == ["actions"]:
            return self._accept_action(principal, body)
        if method == "POST" and route == ["actions", "estimate"]:
            return self._estimate_action(principal, body)
        if method == "POST" and route == ["control", "reference"]:
            reference = CATALOG.get("conventions", {}).get("control_reference")
            if not isinstance(reference, dict):
                raise ServiceError("control_reference_unavailable", status=409)
            validate_parameters(reference, body)
            hello = self._hello(principal, refresh=True)
            self._bind(principal, hello)
            reply = self._rpc("control.reference", principal, parameters=body,
                              engine_instance_id=hello["engine_instance_id"])
            if reply.get("status") not in ("completed", "ok"):
                raise ServiceError("control_reference_unavailable", status=409)
            return 200, projection.control_reference(
                reply.get("result"), body["kind"], hello["engine_instance_id"])
        if method == "GET" and len(route) == 2 and route[0] == "actions":
            return 200, self.project_action(principal, self.store.get_request(principal, _identifier(route[1])))
        if method == "GET" and route == ["events"]:
            return 200, self.events(token, query.get("after"), _limit(query))
        if route == ["observations"] and method in ("GET", "POST"):
            return self._observation(principal, method, body, query)
        if method == "GET" and route == ["threads"]:
            return 200, {"threads": self.social.list_threads(principal)}
        if method == "POST" and route == ["threads"]:
            _object(body, {"scope", "company_ids", "identity_ids"}, {"scope"})
            if "identity_ids" in body:
                raise ServiceError("unsupported_operation", status=400)
            return 201, self.social.create_thread(principal, body["scope"], body.get("company_ids"))
        if len(route) == 3 and route[0] == "threads" and route[2] == "messages":
            thread_id = _identifier(route[1])
            if method == "GET":
                return 200, self.social.history(principal, thread_id, query.get("after"), _limit(query))
            if method == "POST":
                _object(body, {"body", "request_id"}, {"body", "request_id"})
                if not self._chat_ready:
                    raise ServiceError("world_connecting", status=503)
                message = self.social.send(principal, thread_id, body["body"], _identifier(body["request_id"]))
                self._wake.set()
                return 201, message
        if method == "POST" and route == ["blocks"]:
            _object(body, {"identity_id", "blocked"}, {"identity_id"})
            if "blocked" in body and type(body["blocked"]) is not bool:
                raise ServiceError("invalid_request")
            return 200, self.social.block(principal, _identifier(body["identity_id"]), body.get("blocked", True))
        if method == "POST" and route == ["mutes"]:
            _object(body, {"thread_id", "muted"}, {"thread_id"})
            if "muted" in body and type(body["muted"]) is not bool:
                raise ServiceError("invalid_request")
            return 200, self.social.mute(principal, _identifier(body["thread_id"]), body.get("muted", True))
        if method == "POST" and route == ["reports"]:
            _object(body, {"message_id", "reason"}, {"message_id", "reason"})
            return 201, self.social.report(principal, _identifier(body["message_id"]), body["reason"])
        raise ServiceError("not_found", status=404)

    def _observation(self, principal, method, body, query):
        if principal.mode != "text":
            raise ServiceError("forbidden", status=403)
        if method == "POST":
            _object(body, {"query", "parameters"}, {"query"})
            name, params = body["query"], body.get("parameters", {})
        else:
            name = query.get("query", query.get("kind"))
            selector = "query" if "query" in query else "kind"
            params = {key: value for key, value in query.items() if key != selector}
            for key in ("x", "y", "width", "height", "limit", "after_id", "after_offset"):
                if key in params:
                    if key == "after_id" and name == "goal.questions":
                        raw = params[key]
                        if type(raw) is not int and (type(raw) is not str or not re.fullmatch(r"-1|[0-9]+", raw)):
                            raise ServiceError("invalid_request")
                    try:
                        params[key] = int(params[key])
                    except (ValueError, TypeError, OverflowError):
                        raise ServiceError("invalid_request") from None
                    if key == "after_id" and name == "goal.questions" and not -1 <= params[key] <= 65535:
                        raise ServiceError("invalid_request")
            for key in ("after_byte", "cargo_id", "engine_id", "after_generation"):
                if key in params:
                    raw = params[key]
                    if type(raw) is not int and (type(raw) is not str or not re.fullmatch(r"[0-9]+", raw)):
                        raise ServiceError("invalid_request")
                    try:
                        params[key] = int(raw)
                    except (ValueError, TypeError, OverflowError):
                        raise ServiceError("invalid_request") from None
                    if not 0 <= params[key] <= 0xFFFFFFFFFFFFFFFF:
                        raise ServiceError("invalid_request")
            if name in ("orders", "timetable") and "include_live_detail" in params:
                raw_detail = params["include_live_detail"]
                if type(raw_detail) is not str or raw_detail not in ("true", "false"):
                    raise ServiceError("invalid_request")
                params["include_live_detail"] = raw_detail == "true"
        if not isinstance(name, str) or not _ACTION.fullmatch(name) or not isinstance(params, dict):
            raise ServiceError("invalid_request")
        if name == "map.region":
            if any(type(params.get(key)) is not int for key in ("x", "y", "width", "height")):
                raise ServiceError("invalid_request")
            if min(params["x"], params["y"]) < 0 or not 1 <= params["width"] <= 64 or not 1 <= params["height"] <= 64:
                raise ServiceError("invalid_request")
        if "limit" in params:
            _limit(params, maximum=2048 if name == "display.text" else 100)
        targeted_access = name == "company.access" and isinstance(params, dict) and "participant" in params
        hello = self._hello(principal, refresh=targeted_access)
        if self._queries and name not in self._queries:
            raise ServiceError("unsupported_query")
        spec = query_spec(name)
        if spec is None:
            raise ServiceError("unsupported_query")
        validate_parameters(spec, params)
        if targeted_access:
            self._require_fresh_participant(principal, hello, params["participant"])
        self._bind(principal, hello)
        reply = self._rpc("observation.query", principal, query=name, parameters=params,
                          engine_instance_id=hello["engine_instance_id"])
        if reply.get("status") not in ("completed", "ok"):
            raise ServiceError("observation_unavailable", status=503)
        value = reply.get("result", {})
        if not isinstance(value, dict) or "error" in value:
            raise ServiceError("observation_unavailable", status=503)
        return 200, {"observation": projection.observation(
            value, query_spec(name) or self._queries.get(name), name, parameters=params)}

    def image(self, token, image_id):
        principal = self._authorize(token)
        if not self.renderer:
            raise ServiceError("image_unavailable", status=404)
        return self.renderer.image(principal, image_id)

    def _configure_renderer(self, principal):
        hello = self._hello(principal)
        if "native_max_lag_ticks" in hello:
            self.renderer.configure_authority(hello["engine_instance_id"], hello["native_max_lag_ticks"])

    def _observation_barrier(self, principal, request_id):
        """Resolve a private applied-frame fence without changing a saved action.

        The receipt's public tick is not a renderer replay watermark. A status
        lookup also handles actions completed through older event projections
        that did not retain the ledger's execution frame.
        """
        hello = self._hello(principal, refresh=True)
        instance = hello["engine_instance_id"]
        if hello.get("recovery_required") is not False:
            raise ServiceError("world_recovering", status=503)
        self.renderer.configure_authority(instance, hello["native_max_lag_ticks"])
        if request_id is None:
            return instance, None
        record = self.store.get_request(principal, request_id)
        if record["status"] in ("accepted", "queued"):
            self._wake.set()
            return None
        if record["status"] != "completed":
            raise ServiceError("action_not_applied", status=409)
        result = record.get("result") or {}
        if not isinstance(result, dict):
            raise ServiceError("causal_observation_unavailable", status=409)
        if "executed_frame" not in result or "executed_boot_id" not in result:
            self._bind(principal, hello)
            reply = self._rpc("action.status", principal, request_id=request_id,
                              request_sequence=record["request_sequence"],
                              engine_instance_id=instance)
            if (reply.get("status") != "completed" or
                    reply.get("engine_instance_id", instance) != instance):
                raise ServiceError("causal_observation_unavailable", status=409)
            result = reply.get("result") or {}
        if not isinstance(result, dict):
            raise ServiceError("causal_observation_unavailable", status=409)
        frame = result.get("executed_frame")
        if (result.get("executed_boot_id") != instance or type(frame) is not int or
                not 0 <= frame <= 2**32 - 1):
            raise ServiceError("causal_observation_unavailable", status=409)
        return instance, frame

    @staticmethod
    def _estimate_capability(principal):
        convention = CATALOG.get("conventions", {}).get("action_estimate")
        limits = CATALOG.get("limits", {})
        if (not isinstance(convention, dict) or
                convention.get("endpoint") != "/v1/actions/estimate" or
                principal.mode not in convention.get("modes", ()) or
                convention.get("result_fields") != ["success", "cost", "error", "advisory",
                                                    "test_and_execute_may_differ"] or
                limits.get("estimate_interval_ms") != 1000 or
                limits.get("estimate_result_bytes") != 4096):
            return None
        return {"endpoint": convention["endpoint"], "interval_ms": limits["estimate_interval_ms"],
                "result_bytes": limits["estimate_result_bytes"]}

    def _estimate_action(self, principal, body):
        _object(body, {"action", "preconditions"}, {"action"})
        action = _object(body["action"], {"name", "parameters"}, {"name", "parameters"})
        name, parameters = action["name"], action["parameters"]
        if not isinstance(name, str) or not _ACTION.fullmatch(name) or not isinstance(parameters, dict):
            raise ServiceError("invalid_request")
        reserved = {"world_id", "actor", "identity_id", "run_id", "credentials"}
        if name not in ("company.give_money", "company.buy"):
            reserved.add("company_id")
        if set(parameters) & reserved:
            raise ServiceError("invalid_request")
        _object(body.get("preconditions", {}), {})
        try:
            if len(dumps({"action": action, "preconditions": {}}).encode("utf-8")) > 60000:
                raise ServiceError("request_too_large", status=413)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ServiceError("invalid_request") from None
        if name == "ui.apply":
            raise ServiceError("estimate_unavailable", status=409)
        spec = action_spec(name)
        if spec is None:
            raise ServiceError("estimate_unavailable", status=409)
        if principal.mode not in spec.get("modes", ()):
            raise ServiceError("forbidden", status=403)
        validate_parameters(spec, parameters)
        if self._estimate_capability(principal) is None:
            raise ServiceError("estimate_unavailable", status=409)
        if self.checkpoints.admission_blocked:
            raise ServiceError("world_checkpoint_unavailable", status=503)
        hello = self._hello(principal, refresh=True)
        if name not in self._actions:
            raise ServiceError("estimate_unavailable", status=409)
        if name == "company.participant.authorize":
            self._require_fresh_participant(principal, hello, parameters["participant"])
        self._bind(principal, hello, estimate=True)
        reply = self._rpc("action.estimate", principal, action=action, preconditions={},
                          engine_instance_id=hello["engine_instance_id"])
        if reply.get("engine_instance_id", hello["engine_instance_id"]) != hello["engine_instance_id"]:
            raise ServiceError("world_recovering", status=503)
        if reply.get("status") in ("completed", "ok"):
            return 200, {"estimate": projection.estimate(reply.get("result"))}
        if reply.get("status") != "rejected":
            raise ServiceError("engine_unavailable", status=503)
        error = reply.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code == "estimate_rate_limited":
            raise ServiceError("estimate_rate_limited", status=429)
        if code == "mode_restricted":
            raise ServiceError("forbidden", status=403)
        if code in ("world_recovering", "recovery_required", "engine_fence", "world_replaced"):
            raise ServiceError("world_recovering", status=503)
        if code == "checkpoint_busy":
            raise ServiceError("world_checkpoint_unavailable", status=503)
        if code in ("world_unavailable", "ledger_unavailable", "authority_unavailable"):
            raise ServiceError("engine_unavailable", status=503)
        raise ServiceError("estimate_unavailable", status=409)

    def _accept_action(self, principal, body):
        _object(body, {"request_id", "action", "preconditions"}, {"request_id", "action"})
        request_id = _identifier(body["request_id"])
        action = _object(body["action"], {"name", "parameters"}, {"name", "parameters"})
        if not isinstance(action["name"], str) or not _ACTION.fullmatch(action["name"]) or not isinstance(action["parameters"], dict):
            raise ServiceError("invalid_request")
        reserved = {"world_id", "actor", "identity_id", "run_id", "credentials"}
        if action["name"] not in ("company.give_money", "company.buy"):
            reserved.add("company_id")
        if set(action["parameters"]) & reserved:
            raise ServiceError("invalid_request")
        preconditions = body.get("preconditions", {})
        if not isinstance(preconditions, dict):
            raise ServiceError("invalid_request")
        payload = {"action": action, "preconditions": preconditions}
        try:
            if len(dumps(payload).encode()) > 60000:
                raise ServiceError("request_too_large", status=413)
        except (TypeError, ValueError, RecursionError):
            raise ServiceError("invalid_request") from None
        # Historical retries use their original canonical payload, even if
        # today's schema requires stronger handles or the image has expired.
        # Store compares duplicates before allocating or dispatching any work.
        existing = None
        try:
            existing = self.store.get_request(principal, request_id)
        except ServiceError as error:
            if error.code != "not_found":
                raise
        if existing is None:
            # Current policy governs new admissions. A historical receipt is
            # still retryable after an action/schema/renderer has been retired.
            _object(preconditions, {})
            if action["name"] == "ui.apply":
                if principal.mode != "visual" or not self.renderer or not self.renderer.available(principal):
                    raise ServiceError("forbidden", status=403)
                self.renderer.validate_gesture(principal, action["parameters"])
            else:
                spec = action_spec(action["name"])
                if spec is None or principal.mode not in spec.get("modes", ()):
                    raise ServiceError("unsupported_action")
                validate_parameters(spec, action["parameters"])
            # Shape/mode errors remain local, while a fresh admission requires
            # a catalog-compatible authority. Exact duplicates bypass this
            # gate and retain their original immutable request bytes.
            hello = self._hello(principal, refresh=action["name"] == "company.participant.authorize")
            if action["name"] == "company.participant.authorize":
                self._require_fresh_participant(principal, hello, action["parameters"]["participant"])
        if self.checkpoints.admission_blocked:
            if existing is None:
                raise ServiceError("world_checkpoint_unavailable", status=503) from None
            # Store rechecks the exact canonical payload before returning the
            # existing receipt; no new sequence is allocated while overdue.
        record, new = self.store.accept_request(principal, request_id, payload)
        if new:
            self.store.append_event(principal, "action.status", self.project_action(principal, record))
        self._wake.set()
        return (200 if record["status"] in _TERMINAL else 202), self.project_action(principal, record)

    def _dispatch_loop(self):
        while not self._stop.is_set():
            self._wake.wait(0.2)
            self._wake.clear()
            try:
                if self.world_id is None:
                    worlds = self.store.list_worlds()
                    if len(worlds) != 1:
                        continue
                    self.world_id = self.store.enforce_single_world(worlds[0])
                self._process_world(self.world_id, self.store.pending_requests())
            except Exception:
                # Operator/transport faults must not destroy the broker thread.
                # Bounded admission remains active while it cannot dispatch.
                continue

    def _process_world(self, world_id, records):
        if world_id != self.world_id:
            raise ServiceError("forbidden", status=403)
        principals = [p for p in self.store.principals_for_world(world_id) if p.mode in ("text", "visual")]
        # Native-only worlds still need their trusted ingress router; private
        # broker RPCs do not borrow a public player's observation credentials.
        principal = principals[0] if principals else SimpleNamespace(world_id=world_id)
        hello = self._hello(principal, refresh=True)
        self._recover(principal, hello)
        self._checkpoint_sync(hello)
        engine_state = self.store.get_engine_state(world_id)
        self.checkpoints.due(max(0, self.store.max_request_sequence(world_id) - engine_state["retired_through"]))
        # Separate bounded channels: failed chat does not stop action draining.
        for operation in (self._native_chat, self._send_chat, self._poll_events):
            try:
                operation(principal, hello)
            except ServiceError:
                pass
        self._sync_visual_feedback()
        groups = {}
        for record in records:
            if record["world_id"] == world_id and (not self.checkpoints.quiescing or record["status"] == "queued"):
                groups.setdefault(record["company_id"], []).append(record)
        # One request per company per round; a large company queue cannot
        # consume all broker work before another company receives service.
        budget = 16
        for _ in range(16):
            for company in list(groups):
                if not groups[company]:
                    del groups[company]
                    continue
                record = groups[company].pop(0)
                budget -= 1
                actor = self.store.get_principal(record["run_id"])
                if actor:
                    try:
                        self._process_record(actor, record, hello)
                    except ServiceError:
                        pass
                if budget == 0:
                    break
            if not groups or budget == 0 or self._stop.is_set():
                break
        prefix = self.store.checkpoint_ack_prefix(world_id)
        if prefix > self._last_ack:
            acknowledged = self._rpc("action.ack", through_sequence=prefix, engine_instance_id=hello["engine_instance_id"])
            if acknowledged.get("status") == "completed":
                retired = acknowledged.get("result", {}).get("retired_through", 0)
                self.store.observe_engine(world_id, hello["engine_instance_id"], max(retired, engine_state["retired_through"]))
                self._last_ack = prefix
        queued_count = sum(record["status"] == "queued" for record in self.store.pending_requests())
        if self.checkpoints.should_create(queued_count):
            try:
                created = self._rpc("checkpoint.create", engine_instance_id=hello["engine_instance_id"])
                self.checkpoints.created(created.get("status"))
            except ServiceError:
                self.checkpoints.failed()

    def _verify_recovery_checkpoint(self, state, checkpoint, latest, maximum, *, unsent_initial=False):
        """Verify the loaded generation before rebinding a recovery fence.

        A native publisher may be one generation ahead of SQLite after a
        crash. Its private parent anchor must identify our exact manifest;
        a load-verified but unrelated save is insufficient.
        """
        def unsigned(value):
            return type(value) is int and 0 <= value <= (1 << 64) - 1

        def bounded(value, limit):
            return isinstance(value, str) and 0 < len(value) <= limit

        parent = checkpoint.get("parent_checkpoint")
        if parent is not None and (
                not isinstance(parent, dict)
                or set(parent) != {"checkpoint_id", "save_checksum", "saved_tick"}
                or not bounded(parent.get("checkpoint_id"), 512)
                or not bounded(parent.get("save_checksum"), 128)
                or not unsigned(parent.get("saved_tick"))):
            return False
        if state.get("load_verified") is not True:
            return False
        retired = state.get("retired_through", checkpoint.get("retired_through", 0))
        if not unsigned(retired) or retired > maximum:
            return False
        saved_retired = checkpoint.get("checkpoint_retired_through")
        if "checkpoint_retired_through" in checkpoint and (
                not unsigned(saved_retired) or saved_retired > retired):
            return False
        checkpoint_id = checkpoint.get("checkpoint_id")
        if not checkpoint_id and latest is None and (not maximum or unsent_initial):
            return True  # Initial world; accepted work is durably proven unsent.
        if (not bounded(checkpoint_id, 512)
                or not unsigned(checkpoint.get("saved_tick"))):
            return False
        if latest is None:
            return bounded(checkpoint.get("save_checksum"), 128)
        if checkpoint_id == latest["save_id"]:
            return (checkpoint["saved_tick"] == latest["tick"]
                    and checkpoint.get("save_checksum") == latest.get("save_checksum"))
        # Legacy sidecars have no parent anchor and cannot authorize adoption.
        return (parent is not None and saved_retired is not None
                and bounded(checkpoint.get("save_checksum"), 128)
                and bounded(latest.get("save_checksum"), 128)
                and parent["checkpoint_id"] == latest["save_id"]
                and parent["saved_tick"] == latest["tick"]
                and parent["save_checksum"] == latest["save_checksum"]
                and checkpoint_id != parent["checkpoint_id"]
                and checkpoint["saved_tick"] >= parent["saved_tick"])

    def _recover(self, principal, hello):
        instance = hello["engine_instance_id"]
        recovery_required = hello.get("recovery_required", hello.get("checkpoint", {}).get("recovery_required", False))
        if self._instance == instance and not recovery_required:
            return
        if not recovery_required:
            # Backend-only reconnect: the engine still owns its live dedupe
            # table. Poll queued requests normally; ReconcileAccepted is legal
            # only while the engine has opened a save-recovery fence.
            known = self.store.get_engine_state(principal.world_id)
            if known["engine_instance_id"] not in (None, instance) and self.store.max_request_sequence(principal.world_id):
                self.store.begin_recovery(principal.world_id, instance)
                raise ServiceError("loaded_checkpoint_unverified", status=503)
            self.store.observe_engine(principal.world_id, instance, known["retired_through"])
            self._instance = instance
            return
        reply = self._rpc("checkpoint.status", engine_instance_id=instance)
        if reply.get("status") not in ("completed", "ok"):
            raise ServiceError("world_recovering", status=503)
        state = reply.get("result", {})
        checkpoint = state.get("checkpoint", state)
        known = self.store.get_engine_state(principal.world_id)
        latest = self.store.latest_checkpoint(principal.world_id)
        maximum = self.store.max_request_sequence(principal.world_id)
        unsent_initial = (latest is None and not checkpoint.get("checkpoint_id")
                          and known["engine_instance_id"] in (None, instance)
                          and not self.store.has_dispatched_requests(principal.world_id))
        verified = self._verify_recovery_checkpoint(
            state, checkpoint, latest, maximum, unsent_initial=unsent_initial)
        if not verified:
            self.store.begin_recovery(principal.world_id, instance)
            raise ServiceError("loaded_checkpoint_unverified", status=503)
        retired = state.get("retired_through", checkpoint.get("retired_through", 0))
        if type(retired) is not int or not 0 <= retired <= maximum:
            raise ServiceError("recovery_state_missing", status=503)
        self.store.begin_recovery(principal.world_id, instance, verified_checkpoint=True)
        recovery_receipts = []
        after = 0
        while True:
            page = self.store.accepted_requests(principal.world_id, after_sequence=after, limit=128)
            if not page:
                break
            for record in page:
                after = record["request_sequence"]
                if after <= retired:
                    if record["status"] not in _TERMINAL:
                        raise ServiceError("recovery_state_missing", status=503)
                    continue
                # Accepted is definitely unsent: queued marker was committed
                # before sending. Keep it in the outbox for the new instance.
                if record["status"] == "accepted":
                    if self._ui_actuation(record) and record.get("intent_state") == "capturing":
                        actor = self.store.get_principal(record["run_id"])
                        self.store.abort_visual_capture(actor, record["request_id"],
                            event={"request_id": record["request_id"], "status": "rejected"})
                    continue
                if record.get("intent_state") == "aborted":
                    continue  # Proven local cancellation, never sent to authority.
                actor = self.store.get_principal(record["run_id"])
                payload = record["payload"]
                internal = payload.get("rpc_type") == "chat.publish"
                fields = {"message": payload["delivery"], "request_type": "chat.publish"} if internal else dict(payload)
                if self._ui_actuation(record):
                    if record.get("intent_state") != "persisted" or not record.get("visual_intent"):
                        self.store.mark_intent_uncertain(actor, record["request_id"])
                        continue
                    fields["visual_intent"] = record["visual_intent"]
                result = self._rpc("recovery.reconcile", None if internal else actor, request_sequence=after,
                                   request_id=record["request_id"], **fields, engine_instance_id=instance)
                status = result.get("status")
                if status in ("completed", "rejected", "resolved_rolled_back", "uncertain"):
                    receipt = result.get("result")
                    if (not isinstance(receipt, dict) or receipt.get("request_id") != record["request_id"]
                            or type(receipt.get("request_sequence")) is not int or receipt["request_sequence"] != after):
                        # A rejected RPC (mode/fence/binding/transport policy)
                        # is not an authoritative rejection of this action.
                        raise ServiceError("recovery_receipt_unverified", status=503)
                    corrected = self.store.resolve_recovery_request(actor, record["request_id"], status,
                                result=result.get("result"), error=result.get("error"), tick=result.get("tick"),
                                checkpointed=(result.get("result") or {}).get("checkpoint_state") == "checkpointed")
                    if corrected["checkpoint_state"] == "checkpointed":
                        recovery_receipts.append({"request_sequence": after, "status": status})
                    try:
                        self.store.append_event(actor, "action.status", self.project_action(actor, corrected))
                    except ServiceError:
                        pass  # revoked principals do not regain event access.
                else:
                    raise ServiceError("world_recovering", status=503)
            if len(page) < 128:
                break
        if checkpoint.get("checkpoint_id"):
            manifest = {
                "world_epoch": self.store.world_epoch(principal.world_id), "save_id": checkpoint["checkpoint_id"],
                "retired_through": checkpoint.get("checkpoint_retired_through", retired),
                "tick": checkpoint.get("saved_tick", 0),
                "save_checksum": checkpoint.get("save_checksum")}
            existing = self.store.latest_checkpoint(principal.world_id)
            if existing and existing["save_id"] == manifest["save_id"]:
                if existing["tick"] != manifest["tick"] or existing.get("save_checksum") != manifest["save_checksum"]:
                    raise ServiceError("checkpoint_conflict", status=503)
                self.store.merge_checkpoint_receipts(principal.world_id, manifest["save_id"], recovery_receipts)
            else:
                self.store.apply_checkpoint(principal.world_id, manifest, recovery_receipts)
        # Persist recognition of this engine before it can drop its fence. If
        # the finish reply is lost, a backend-only reconnect can safely use the
        # engine's live ledger; if it never finished, the next scan is idempotent.
        self.store.finish_recovery(principal.world_id, scan_completed=True,
                                   engine_instance_id=instance,
                                   retired_through=max(retired, known["retired_through"]))
        done = self._rpc("recovery.finish", engine_instance_id=instance)
        if done.get("status") not in ("completed", "ok"):
            raise ServiceError("world_recovering", status=503)
        self._last_ack = 0  # A restored save may predate a live-only retirement.
        self._instance = instance

    def _checkpoint_sync(self, hello):
        reply = self._rpc("checkpoint.status", engine_instance_id=hello["engine_instance_id"])
        if reply.get("status") not in ("completed", "ok"):
            raise ServiceError("checkpoint_unavailable", status=503)
        state = reply.get("result", {})
        checkpoint = state.get("checkpoint", state)
        if checkpoint.get("status") in ("idle", "saving", "completed", "failed"):
            self.checkpoints.observe(checkpoint)
        known = self.store.get_engine_state(self.world_id)
        retired = state.get("retired_through", checkpoint.get("retired_through", 0))
        self.store.observe_engine(self.world_id, hello["engine_instance_id"], max(retired, known["retired_through"]))
        if not checkpoint.get("checkpoint_id"):
            return
        latest = self.store.latest_checkpoint(self.world_id)
        if latest and latest.get("save_id") == checkpoint["checkpoint_id"]:
            return
        receipts = []
        after = 0
        while True:
            records = self.store.accepted_requests(self.world_id, after_sequence=after, limit=128)
            if not records:
                break
            for record in records:
                after = record["request_sequence"]
                if record["status"] not in _TERMINAL or record["checkpoint_state"] == "checkpointed":
                    continue
                actor = self.store.get_principal(record["run_id"])
                internal = record["payload"].get("rpc_type") == "chat.publish"
                fields = {"request_sequence": after, "request_id": record["request_id"],
                          "engine_instance_id": hello["engine_instance_id"]}
                if internal:
                    fields.update(request_type="chat.publish", message=record["payload"]["delivery"])
                else:
                    self._bind(actor, hello)
                    if self._ui_actuation(record):
                        if record.get("intent_state") != "persisted" or not record.get("visual_intent"):
                            continue
                        fields.update(record["payload"], visual_intent=record["visual_intent"])
                status = self._rpc("action.status", None if internal else actor, **fields)
                if status.get("status") == record["status"] and status.get("result", {}).get("checkpoint_state") == "checkpointed":
                    receipts.append({"request_sequence": after, "status": record["status"]})
            if len(records) < 128:
                break
        self.store.apply_checkpoint(self.world_id, {
            "world_epoch": self.store.world_epoch(self.world_id), "save_id": checkpoint["checkpoint_id"],
            "retired_through": checkpoint.get("checkpoint_retired_through", retired),
            "tick": checkpoint.get("saved_tick", 0), "save_checksum": checkpoint.get("save_checksum")},
            receipts)

    def _process_record(self, principal, record, hello=None):
        request_id = record["request_id"]
        try:
            self.store.validate_principal(principal)
        except ServiceError:
            if self._ui_actuation(record) and record["status"] == "accepted" and record.get("intent_state") == "capturing":
                return self.store.abort_visual_capture(principal, request_id)
            status = "rejected" if record["status"] == "accepted" else "uncertain"
            return self.store.update_request(principal, request_id, status, error={"code": "authorization_revoked"})
        hello = hello or self._hello(principal, refresh=True)
        internal = record["payload"].get("rpc_type") == "chat.publish"
        if not internal:
            self._bind(principal, hello)
        instance = hello["engine_instance_id"]
        ui_actuation = self._ui_actuation(record)
        if ui_actuation:
            if record.get("intent_state") == "capturing":
                return self.store.abort_visual_capture(principal, request_id,
                    event={"request_id": request_id, "status": "rejected"})
            if record.get("intent_state") == "uncertain":
                return self.store.mark_intent_uncertain(principal, request_id,
                    event={"request_id": request_id, "status": "uncertain"})
            if record.get("intent_state") == "none":
                if record["status"] != "accepted" or record["payload"]["action"]["name"] != "ui.apply" or not self.renderer:
                    return self.store.mark_intent_uncertain(principal, request_id,
                        event={"request_id": request_id, "status": "uncertain"})
                expected_worker = self.renderer.prepare_intent(principal, instance, hello["native_max_lag_ticks"])
                self.store.begin_intent_capture(principal, request_id, expected_worker)
                try:
                    intent = self.renderer.capture_intent(principal, request_id,
                        record["payload"]["action"]["parameters"], instance, hello["native_max_lag_ticks"],
                        expected_worker_instance_id=expected_worker)
                    record = self.store.persist_visual_intent(principal, request_id, intent)
                except (ServiceError, KeyError):
                    return self.store.abort_visual_capture(principal, request_id,
                        event={"request_id": request_id, "status": "rejected"})
            if record.get("intent_state") != "persisted" or not record.get("visual_intent"):
                return self.store.mark_intent_uncertain(principal, request_id)
        fields = {"request_id": request_id, "request_sequence": record["request_sequence"], "engine_instance_id": instance}
        if ui_actuation:
            fields["visual_intent"] = record["visual_intent"]
        if record["status"] == "accepted":
            if not internal and record["payload"]["action"]["name"] not in self._actions:
                return self._record_result(principal, record, {"status": "rejected", "error": {"code": "unsupported_action"}})
            self.store.update_request(principal, request_id, "queued", result={"engine_instance_id": instance})
            try:
                if internal:
                    reply = self._rpc("chat.publish", **fields, message=record["payload"]["delivery"])
                else:
                    reply = self._rpc("action.submit", principal, **fields, **record["payload"],
                                      payload_sha256=hashlib.sha256(dumps(record["payload"]).encode()).hexdigest())
            except ServiceError:
                return None
        else:
            old_instance = (record.get("result") or {}).get("engine_instance_id")
            if old_instance and old_instance != instance:
                return self._record_result(principal, record, {"status": "uncertain"})
            if internal:
                fields["request_type"] = "chat.publish"
                fields["message"] = record["payload"]["delivery"]
            elif ui_actuation:
                fields.update(record["payload"])
            reply = self._rpc("action.status", None if internal else principal, **fields)
        if reply.get("status") in ("not_found", "unknown", "uncertain"):
            return self._record_result(principal, record, {"status": "uncertain"})
        if reply.get("status") in _TERMINAL:
            return self._record_result(principal, record, reply)
        if reply.get("status") == "queued":
            self.store.update_request(principal, request_id, "queued",
                                      result={"engine_instance_id": instance, **({"authority_queued": True} if ui_actuation else {})})
        return None

    def _record_result(self, principal, record, reply):
        projected_record = dict(record, status=reply["status"], result=reply.get("result"),
                                error=reply.get("error"), tick=reply.get("tick"))
        private_result = reply.get("result")
        if self._ui_actuation(record):
            private_result = dict(private_result or {}, authority_queued=bool(
                (record.get("result") or {}).get("authority_queued")))
        value = self.store.update_request(principal, record["request_id"], reply["status"],
               result=private_result, error=reply.get("error"), tick=reply.get("tick"),
               event=None if principal.mode == "internal" else self.project_action(principal, projected_record))
        if reply["status"] in _TERMINAL and (reply.get("result") or {}).get("checkpoint_state") == "checkpointed":
            # Observe the current manifest before associating this verified
            # receipt: a newer save may have completed since the previous poll.
            if self._hello_value:
                self._checkpoint_sync(self._hello_value)
            checkpoint = self.store.latest_checkpoint(principal.world_id)
            if checkpoint:
                self.store.merge_checkpoint_receipts(principal.world_id, checkpoint["save_id"],
                    [{"request_sequence": record["request_sequence"], "status": reply["status"]}])
                value["checkpoint_state"] = "checkpointed"
                if principal.mode != "internal":
                    self.store.append_event(principal, "action.status", self.project_action(principal, value))
        if principal.mode == "internal":
            message_id = record["payload"]["message_id"]
            if reply["status"] == "completed":
                self.social.mark_delivered(message_id, tick=reply.get("tick"))
            elif reply["status"] in ("uncertain", "resolved_rolled_back", "rejected"):
                self.social.mark_uncertain(message_id)
        return value

    def _sync_visual_feedback(self):
        if not self.renderer:
            return
        records = self.store.unsynced_visual_requests(self.world_id, limit=16,
                                                     after_sequence=self._visual_feedback_after)
        if not records:
            self._visual_feedback_after = 0
            return
        self._visual_feedback_after = records[-1]["request_sequence"]
        for record in records:
            actor = self.store.get_principal(record["run_id"])
            if actor is None or not self.renderer.available(actor):
                continue
            try:
                self._configure_renderer(actor)
                expected_worker = record.get("expected_worker_instance_id") or (record.get("visual_intent") or {}).get("worker_instance_id")
                if not expected_worker:
                    continue
                fence = {"expected_worker_instance_id": expected_worker}
                result = record.get("result") or {}
                executed = record["status"] == "completed" or type(result.get("success")) is bool
                phase = "commit" if record["status"] != "resolved_rolled_back" and executed else "reject"
                status = self.renderer.intent_status(actor, record["request_id"], **fence)
                if record["ui_state"] == "pending":
                    if status.get("phase") == "unknown" and record.get("intent_state") != "aborted":
                        continue
                    if record.get("intent_state") == "persisted" and status.get("bundle") != record["visual_intent"]:
                        continue
                    if status.get("phase") != "unknown":
                        self.renderer.release_intent(actor, record["request_id"], phase,
                                                     authority_rejected=phase == "reject", **fence)
                    self.store.mark_ui_state(actor, record["request_id"], "released")
                    status = self.renderer.intent_status(actor, record["request_id"], **fence)
                if status.get("phase") == "unknown":
                    self.renderer.release_intent(actor, record["request_id"], "ack", **fence)
                    self.store.mark_ui_state(actor, record["request_id"], "acked")
                    continue
                self.renderer.clear_children(actor, status, **fence)
                if status.get("phase") in ("completed", "rejected"):
                    self.renderer.release_intent(actor, record["request_id"], "ack", **fence)
                    self.store.mark_ui_state(actor, record["request_id"], "acked")
            except ServiceError:
                continue  # Never turn a UI retry into another gesture.

    def _native_chat(self, principal, hello):
        if not hasattr(self.social, "admit_native"):
            return
        roster = self._rpc("chat.roster", engine_instance_id=hello["engine_instance_id"])
        if (roster.get("status") in ("completed", "ok") and
                roster.get("engine_instance_id") == hello["engine_instance_id"]):
            value = roster.get("result", [])
            rows = value.get("clients", []) if isinstance(value, dict) else value
            managed = self.renderer.managed_native_worker_keys() if self.renderer else set()
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ServiceError("native_roster_unavailable", status=503)
            self.social.update_native_roster(principal.world_id, [
                row for row in rows if not isinstance(row.get("public_key"), str) or
                row["public_key"].lower() not in managed])
            self._chat_ready = True
        pending = self._rpc("chat.pending", parameters={"limit": 64, "max_bytes": 48000}, engine_instance_id=hello["engine_instance_id"])
        if pending.get("status") not in ("completed", "ok"):
            return
        acknowledgements = []
        for wrapper in pending.get("result", {}).get("messages", [])[:64]:
            message = dict(wrapper.get("data", wrapper))
            message["event_id"] = wrapper.get("event_id", message.get("message_id"))
            if message.get("scope") == "direct":
                message["scope"] = "direct_identity"
            try:
                receipt = self.social.admit_native(principal.world_id, message)
                accepted = receipt["disposition"] == "accepted"
            except ServiceError:
                accepted = False
            if isinstance(message.get("message_id"), str):
                acknowledgements.append({"message_id": message["message_id"], "accepted": accepted})
        if acknowledgements:
            self._rpc("chat.ack", parameters={"ingress": acknowledgements}, engine_instance_id=hello["engine_instance_id"])

    def _send_chat(self, principal, hello):
        for entry in self.social.pending_deliveries(principal.world_id, limit=16):
            request_id = "chat:" + entry["message_id"]
            if self.store.get_internal(principal.world_id, request_id):
                continue
            message = self.social.native_delivery(entry["message_id"])
            exact = {key: message[key] for key in ("message_id", "delivery_request_id", "body", "sender", "recipient_public_keys")}
            self.store.reserve_internal(principal.world_id, request_id,
                {"rpc_type": "chat.publish", "message_id": entry["message_id"], "delivery": exact})

    def _poll_events(self, principal, hello):
        pause_supported = "pause" in hello
        if pause_supported:
            projection.world_status(hello["pause"])  # Present but malformed is not a legacy authority.
        with self.store.lock:
            row = self.store.connection.execute("SELECT * FROM broker_offsets WHERE world_id=?", (principal.world_id,)).fetchone()
        instance = hello["engine_instance_id"]
        first_instance_page = row is None or row["engine_instance_id"] != instance
        after = _native_offset(row["cursor"]) if not first_instance_page else 0
        reply = self._rpc("events.poll", after=after, limit=128, engine_instance_id=instance)
        if reply.get("status") not in ("completed", "ok"):
            return
        if reply.get("engine_instance_id") != instance:
            raise ServiceError("engine_unavailable", status=503)
        page = reply.get("result")
        if not isinstance(page, dict):
            raise ServiceError("engine_unavailable", status=503)
        events, cursor, gap, oldest = (page.get("events"), page.get("cursor"),
                                       page.get("gap"), page.get("oldest_event_id"))
        if (not isinstance(events, list) or len(events) > 128 or type(cursor) is not int
                or not after <= cursor <= _MAX_U64 or type(gap) is not bool
                or type(oldest) is not int or not 1 <= oldest <= _MAX_U64):
            raise ServiceError("engine_unavailable", status=503)
        if (gap and (not events or oldest <= after + 1)) or (events and not gap and oldest > after + 1):
            raise ServiceError("engine_unavailable", status=503)

        public_events, quarantined = [], []
        previous = after
        malformed_status = False
        for event in events:
            if (not isinstance(event, dict) or type(event.get("event_id")) is not int
                    or not previous < event["event_id"] <= cursor
                    or event.get("engine_instance_id") != instance):
                raise ServiceError("engine_unavailable", status=503)
            previous = event["event_id"]
            kind = event.get("type")
            if kind == "world.status" and pause_supported:
                data = event.get("data")
                try:
                    pause = projection.world_status(data.get("pause") if isinstance(data, dict) else None)
                except ServiceError:
                    malformed_status = True
                    quarantined.append(event)
                    continue
                public_events.append({"world_id": principal.world_id, "world_epoch": hello["world_epoch"],
                                      "engine_instance_id": instance, "pause": pause})
            elif kind not in ("action.completed", "checkpoint.complete"):
                quarantined.append(event)
        if cursor != previous or (events and events[0]["event_id"] != max(after + 1, oldest)):
            raise ServiceError("engine_unavailable", status=503)

        baseline = pause_supported and (first_instance_page or gap or malformed_status)
        baseline_event = None
        if baseline:
            fresh = self._hello(principal, refresh=True)  # Never hold SQLite across native RPC.
            if (fresh["engine_instance_id"] != instance or fresh["world_epoch"] != hello["world_epoch"]):
                raise ServiceError("world_recovering", status=503)
            baseline_event = {"world_id": principal.world_id, "world_epoch": fresh["world_epoch"],
                              "engine_instance_id": instance, "pause": _pause_from_hello(fresh),
                              "baseline": True}
            if gap or malformed_status or (row is not None and first_instance_page):
                baseline_event["history_gap"] = True

        # A cursor and all recipient fanout rows commit together. A retry sees
        # either neither effect or both; native tick/private fields never fan out.
        with self.store._transaction() as db:
            world = db.execute("SELECT epoch,recovery_state FROM worlds WHERE id=?",
                               (principal.world_id,)).fetchone()
            if world is None or world["epoch"] != hello["world_epoch"] or world["recovery_state"] != "ready":
                raise ServiceError("world_recovering", status=503)
            current = db.execute("SELECT engine_instance_id,cursor FROM broker_offsets WHERE world_id=?",
                                 (principal.world_id,)).fetchone()
            if ((current is None) != (row is None) or
                    (current is not None and (current["engine_instance_id"] != row["engine_instance_id"]
                                              or _native_offset(current["cursor"]) != _native_offset(row["cursor"])))):
                raise ServiceError("world_recovering", status=503)
            recipients = [p for p in self.store.active_principals_for_world(principal.world_id)
                          if p.mode in ("text", "visual")]
            if baseline_event is not None:
                for member in recipients:
                    self.store.append_event_in_transaction(db, member, "world.status", baseline_event)
            for data in public_events:
                for member in recipients:
                    self.store.append_event_in_transaction(db, member, "world.status", data)
            for event in quarantined:
                db.execute("INSERT OR IGNORE INTO broker_quarantine VALUES(?,?,?,?)",
                           (principal.world_id, instance, str(event["event_id"]), dumps(event)))
            if quarantined:
                db.execute("DELETE FROM broker_quarantine WHERE rowid NOT IN "
                           "(SELECT rowid FROM broker_quarantine ORDER BY rowid DESC LIMIT 128)")
            db.execute("INSERT INTO broker_offsets VALUES(?,?,?) ON CONFLICT(world_id) "
                       "DO UPDATE SET engine_instance_id=excluded.engine_instance_id,cursor=excluded.cursor",
                       (principal.world_id, instance, _stored_native_offset(cursor)))
