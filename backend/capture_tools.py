"""Public capture-player tools over dependency-free MCP stdio.

Run ``python -m backend.capture_tools --origin ORIGIN --token-file FILE
--artifacts DIRECTORY`` and configure the model host to use its MCP transport.
Successful observation tool results contain MCP ImageContent, not file paths.
Optional audit records establish emission to that transport, not model receipt
or understanding. The adapter accesses only the authenticated public API.
"""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from .client import BackendClient, BackendError, MAX_IMAGE_BYTES, credential_from_env_or_file
from .json_tools import dumps, loads
from .errors import ServiceError
from .observations import validate_request
from .player_actions import SavedActions


PROTOCOL_VERSION = "2025-11-25"
MAX_MESSAGE_BYTES = 128 * 1024
MAX_RESULT_BYTES = 24 * 1024 * 1024
MAX_SAVED_BYTES = 64 * 1024
MAX_AUDIT_BYTES = 4 * 1024 * 1024
MAX_WAIT_MS = 20_000
TERMINAL = {"completed", "rejected", "uncertain", "retired", "resolved_rolled_back"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
ACTION_NAME = re.compile(r"[a-z][a-z0-9_.]{0,79}\Z")
IDENTITY_FIELDS = {"observation_generation", "observation_request_id"}
SCHEMA_PATH = Path(__file__).with_name("schemas") / "public-api-v1.json"


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("Invalid tool arguments")
    return value


def _identifier(value):
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid request ID")
    return value


def _wait_ms(value):
    if type(value) is not int or not 0 <= value <= MAX_WAIT_MS:
        raise ValueError("wait_ms must be an integer from 0 to 20000")
    return value


def _receipt(value, request_id):
    if (not isinstance(value, dict) or value.get("request_id") != request_id
            or not isinstance(value.get("status"), str)
            or value["status"] not in TERMINAL | {"accepted", "queued"}):
        raise BackendError(0, "invalid_response", "Invalid public action receipt")
    try:
        dumps(value).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise BackendError(0, "invalid_response", "Invalid public action receipt") from None
    return value


def _validate_observation(kind, parameters):
    if not isinstance(parameters, dict) or set(parameters) & IDENTITY_FIELDS:
        raise ValueError("Observation identity fields are managed by the adapter")
    try:
        normalized = validate_request(kind, {"observation_generation": "0" * 32,
                                             "observation_request_id": "obs1:1000000000000:" + "0" * 32,
                                             **parameters})
    except ServiceError:
        raise ValueError("Invalid observation parameters") from None
    return {key: value for key, value in normalized.items() if key not in IDENTITY_FIELDS}


def _schema_object(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _observation_schema(kind):
    """Reuse the public wire schema, keeping identity mechanics out of tools."""
    source = loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            if not reference.startswith("#/$defs/"):
                raise ValueError("Unsupported public schema reference")
            return expand({**source["$defs"][reference.removeprefix("#/$defs/")],
                           **{key: item for key, item in value.items() if key != "$ref"}})
        return {key: expand(item) for key, item in value.items()}

    name = "ObserveWorldRequest" if kind == "world" else "ObserveNativeInfoRequest"
    schema = expand(source["$defs"][name])
    # Observation request variants may be represented by oneOf/anyOf branches.
    def remove_identity(value):
        if isinstance(value, dict):
            if "properties" in value:
                for field in IDENTITY_FIELDS:
                    value["properties"].pop(field, None)
                if "required" in value:
                    value["required"] = [field for field in value["required"] if field not in IDENTITY_FIELDS]
                value["properties"]["observation_key"] = {
                    "type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$",
                    "description": "Optional caller-known key for exact recovery if this entire tool reply is lost. Reuse only for this same observation.",
                }
            for key in ("oneOf", "anyOf", "allOf"):
                for variant in value.get(key, []):
                    remove_identity(variant)
    remove_identity(schema)
    # MCP Tool.inputSchema requires an object root, including when the public
    # JSON schema expresses its object variants using only anyOf.
    schema["type"] = "object"
    return schema


def tool_definitions():
    identifier = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$"}
    wait = {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_MS}
    action = _schema_object({
        "request_id": identifier,
        "action": _schema_object({
            "name": {"type": "string", "pattern": "^[a-z][a-z0-9_.]{0,79}$"},
            "parameters": {"type": "object"},
        }, ("name", "parameters")),
        "preconditions": {"type": "object"},
    }, ("request_id", "action"))
    world, native = _observation_schema("world"), _observation_schema("native_info")
    observation = {"oneOf": [
        _schema_object({"kind": {"const": "world"}, "parameters": world}, ("kind", "parameters")),
        _schema_object({"kind": {"const": "native_info"}, "parameters": native}, ("kind", "parameters")),
    ]}
    descriptions = [
        ("observe_world", "Return the selected genuine native map image. Reuse observation_key for exact lost-reply recovery; omit it for a new read each time.", world),
        ("observe_native_info", "Return a crop of the selected real native panel. Reuse observation_key for exact lost-reply recovery.", native),
        ("retry_observation", "Retry the exact saved observation; copy its opaque observation_request_id.",
         _schema_object({"observation_request_id": identifier}, ("observation_request_id",))),
        ("submit_action", "Save and submit one exact typed gameplay request. Choose a new ID only for a new intent.",
         _schema_object({"request": action}, ("request",))),
        ("retry_action", "Resend only the original saved ID, parameters, references and version tokens.",
         _schema_object({"request_id": identifier}, ("request_id",))),
        ("wait_action", "Wait for a receipt using bounded public status polling. Pending is not gameplay success.",
         _schema_object({"request_id": identifier, "wait_ms": wait}, ("request_id",))),
        ("act_and_observe", "Submit an exact typed action, wait, then return its receipt and chosen subsequent image. Capture failure preserves the receipt.",
         _schema_object({"request": action, "observation": observation, "wait_ms": wait}, ("request", "observation"))),
    ]
    return [{"name": name, "description": description, "inputSchema": schema}
            for name, description, schema in descriptions]


def _result(value, data=None, *, error=False):
    content = [{"type": "text", "text": dumps(value)}]
    if data is not None:
        content.append({"type": "image", "mimeType": "image/png", "data": base64.b64encode(data).decode("ascii")})
    return {"content": content, "structuredContent": value, "isError": error}


class CaptureTools:
    def __init__(self, client, artifacts, *, audit=False):
        self.client = client
        self.artifacts = Path(artifacts).resolve()
        for directory in (self.artifacts, self.artifacts / "observations", self.artifacts / "observation_keys"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.actions = SavedActions(self.artifacts / "requests")
        self.audit = audit
        self.call_id = None
        self._active_attempt_id = None
        self._last_http_attempt_id = None
        if hasattr(client, "on_http_attempt"):
            self._previous_http_callback = client.on_http_attempt
            client.on_http_attempt = self._http_measured

    def record(self, value):
        if not self.audit:
            return
        try:
            path = self.artifacts / "capture-tools.jsonl"
            line = (dumps({"time": time.time(), "mcp_call_id": self.call_id, **value}) + "\n").encode("utf-8")
            if path.exists() and path.stat().st_size + len(line) > MAX_AUDIT_BYTES:
                path.replace(path.with_suffix(".jsonl.1"))
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags, 0o600), "ab") as target:
                target.write(line)
        except OSError:
            # Optional measurement must not replace a known action receipt.
            pass

    def _http_measured(self, measurement):
        self._last_http_attempt_id = measurement["attempt_id"]
        self.record({"event": "http_attempt", **measurement,
                     "http_attempt_id": measurement["attempt_id"], "adapter_attempt_id": self._active_attempt_id})
        if self._previous_http_callback is not None:
            self._previous_http_callback(measurement)

    def _path(self, category, request_id):
        _identifier(request_id)
        return self.artifacts / category / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")

    def _save(self, category, request_id, value):
        data = dumps(value).encode("utf-8")
        if len(data) > MAX_SAVED_BYTES:
            raise ValueError("Saved request exceeds 64 KiB")
        path = self._path(category, request_id)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            if dumps(self._load(category, request_id)).encode("utf-8") != data:
                raise ValueError("Request ID already has a different saved body") from None
            return
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if os.name == "posix":
            directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)

    def _load(self, category, request_id):
        descriptor = os.open(self._path(category, request_id), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Saved request must be a regular file")
            data = source.read(MAX_SAVED_BYTES + 1)
        if len(data) > MAX_SAVED_BYTES:
            raise ValueError("Saved request exceeds 64 KiB")
        value = loads(data.decode("utf-8"))
        if (not isinstance(value, dict) or set(value) - {"kind", "request", "observation_key"}
                or not {"kind", "request"} <= set(value)
                or value["kind"] not in ("world", "native_info") or not isinstance(value["request"], dict)
                or (category == "observations" and value["request"].get("observation_request_id") != request_id)
                or (category == "observation_keys" and value.get("observation_key") != request_id)):
            raise ValueError("Invalid saved request")
        try:
            validate_request(value["kind"], value["request"])
        except ServiceError:
            raise ValueError("Invalid saved observation") from None
        return value

    def _http(self, method, path, operation, *, request_id=None):
        attempt = uuid.uuid4().hex
        fields = {"adapter_attempt_id": attempt, "method": method, "path": path}
        if request_id is not None:
            fields["request_id"] = request_id
        self._active_attempt_id, self._last_http_attempt_id = attempt, None
        self.record({"event": "http_operation", **fields})
        try:
            reply = operation()
        except (BackendError, OSError, TimeoutError, ValueError) as error:
            details = {"code": error.code, "status": error.status} if isinstance(error, BackendError) else {"type": type(error).__name__}
            self.record({"event": "http_failure", **fields, "error": details})
            raise
        finally:
            self._active_attempt_id = None
        self.record({"event": "http_reply", **fields, "http_attempt_id": self._last_http_attempt_id})
        return reply, self._last_http_attempt_id or attempt

    def _submit(self, request):
        _object(request, {"request_id", "action", "preconditions"}, {"request_id", "action"})
        request_id = _identifier(request["request_id"])
        action = _object(request["action"], {"name", "parameters"}, {"name", "parameters"})
        if (not isinstance(action["name"], str) or ACTION_NAME.fullmatch(action["name"]) is None
                or action["name"] == "ui.apply" or not isinstance(action["parameters"], dict)
                or not isinstance(request.get("preconditions", {}), dict)):
            raise ValueError("Expected a canonical typed gameplay action")
        new = self.actions.save(request)
        reply, _ = self._http("POST", "/v1/actions", lambda: self.client.request("POST", "/v1/actions", request), request_id=request_id)
        return _receipt(reply, request_id), {"kind": "new_intent" if new else "exact_replay", "request_id": request_id}

    def _wait(self, request_id, wait_ms, receipt=None):
        request_id, wait_ms = _identifier(request_id), _wait_ms(wait_ms)
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            if receipt is None:
                path = "/v1/actions/" + request_id
                receipt, _ = self._http("GET", path, lambda: self.client.request("GET", path), request_id=request_id)
            receipt = _receipt(receipt, request_id)
            if receipt.get("status") in TERMINAL or time.monotonic() >= deadline:
                return receipt
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            receipt = None

    def _selection(self, kind, parameters):
        if not isinstance(parameters, dict):
            raise ValueError("Invalid observation parameters")
        parameters = deepcopy(parameters)
        has_key = "observation_key" in parameters
        observation_key = parameters.pop("observation_key", None)
        normalized = _validate_observation(kind, parameters)
        saved = None
        if has_key:
            _identifier(observation_key)
            try:
                saved = self._load("observation_keys", observation_key)
            except FileNotFoundError:
                pass
            if saved is not None and (saved["kind"] != kind or dumps(normalized) != dumps({
                    key: value for key, value in saved["request"].items() if key not in IDENTITY_FIELDS})):
                raise ValueError("Observation key already has a different saved selection")
        return parameters, observation_key, saved

    def _observe(self, kind, parameters=None, *, saved=None):
        if saved is None:
            parameters, observation_key, saved = self._selection(kind, parameters)
        if saved is None:
            # The public client caches capability bootstrap. This operation is
            # distinct from the explicit HTTP-attempt records below.
            request = self.client.new_observation_request(kind, deepcopy(parameters))
            self.record({"event": "observation_prepared", "observation_request_id": request["observation_request_id"], "kind": kind})
            saved = {"kind": kind, "request": request}
            if observation_key is not None:
                saved["observation_key"] = observation_key
                self._save("observation_keys", observation_key, saved)
        else:
            kind, request = saved["kind"], saved["request"]
        self._save("observations", request["observation_request_id"], saved)
        request_id = _identifier(request["observation_request_id"])
        if kind not in ("world", "native_info"):
            raise ValueError("Invalid observation kind")
        path = "/v1/view/observe/" + ("world" if kind == "world" else "native-info")
        method = self.client.observe_world if kind == "world" else self.client.observe_native_info
        try:
            reply, attempt = self._http("POST", path, lambda: method(request), request_id=request_id)
        except (BackendError, OSError, TimeoutError, ValueError) as error:
            if (isinstance(error, BackendError) and error.code == "observation_expired"
                    and hasattr(self.client, "reset_observation_context")):
                # The next new read may bootstrap a new generation. This
                # failed read and any exact retry keep their original body.
                self.client.reset_observation_context()
            return {"observation_request_id": request_id, **({"observation_key": saved["observation_key"]} if "observation_key" in saved else {}),
                    "kind": kind, "purpose": "observation",
                    "status": "failed", **self._error(error)}, None
        if isinstance(reply, dict):
            # The reference client bounds pending JSON. Only workflow fields
            # are carried into model content; native provenance stays private.
            pending = {key: reply[key] for key in ("status", "reason", "retry_after_ms") if key in reply}
            return {"observation_request_id": request_id, **({"observation_key": saved["observation_key"]} if "observation_key" in saved else {}),
                    "kind": kind, "purpose": "observation", **pending}, None
        data = reply.data
        if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Invalid observation image")
        metadata = {"observation_request_id": request_id, "observation_id": reply.observation_id,
                    "image_id": reply.image_id, "observation_generation": reply.generation,
                    "kind": reply.kind, "purpose": "observation", "width": reply.width, "height": reply.height,
                    "causal_state": reply.causal_state, "reused": reply.reused, "age_ms": reply.age_ms,
                    "image_sha256": hashlib.sha256(data).hexdigest(), "image_bytes": len(data),
                    "http_attempt_id": attempt, "delivery_id": uuid.uuid4().hex}
        if "observation_key" in saved:
            metadata["observation_key"] = saved["observation_key"]
        self.record({"event": "image_ready", **metadata})
        return metadata, data

    def call(self, name, arguments, *, call_id=None):
        self.call_id = call_id
        try:
            if name in ("observe_world", "observe_native_info"):
                kind = "world" if name == "observe_world" else "native_info"
                metadata, data = self._observe(kind, arguments)
                return _result(metadata, data, error=metadata.get("status") == "failed")
            if name == "retry_observation":
                _object(arguments, {"observation_request_id"}, {"observation_request_id"})
                metadata, data = self._observe(None, saved=self._load("observations", arguments["observation_request_id"]))
                return _result(metadata, data, error=metadata.get("status") == "failed")
            if name == "submit_action":
                _object(arguments, {"request"}, {"request"})
                receipt, submission = self._submit(arguments["request"])
                return _result({"receipt": receipt, "submission": submission})
            if name == "retry_action":
                _object(arguments, {"request_id"}, {"request_id"})
                receipt, submission = self._submit(self.actions.load(arguments["request_id"]))
                return _result({"receipt": receipt, "submission": submission})
            if name == "wait_action":
                _object(arguments, {"request_id", "wait_ms"}, {"request_id"})
                return _result({"receipt": self._wait(arguments["request_id"], arguments.get("wait_ms", MAX_WAIT_MS))})
            if name == "act_and_observe":
                _object(arguments, {"request", "observation", "wait_ms"}, {"request", "observation"})
                observation = _object(arguments["observation"], {"kind", "parameters"}, {"kind", "parameters"})
                if observation["kind"] not in ("world", "native_info") or not isinstance(observation["parameters"], dict):
                    raise ValueError("Invalid observation selection")
                request = arguments["request"]
                if not isinstance(request, dict):
                    raise ValueError("Expected a canonical typed gameplay action")
                if observation["parameters"].get("after_request_id", request.get("request_id")) != request.get("request_id"):
                    raise ValueError("Observation must follow the submitted action")
                parameters = {**observation["parameters"], "after_request_id": request.get("request_id")}
                self._selection(observation["kind"], parameters)
                wait_ms = _wait_ms(arguments.get("wait_ms", MAX_WAIT_MS))
                receipt, submission = self._submit(request)
                try:
                    receipt = self._wait(request["request_id"], wait_ms, receipt)
                except (BackendError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as error:
                    return _result({"receipt": receipt, "submission": submission, "wait": self._error(error),
                                    "observation": {"status": "not_requested"}}, error=True)
                if receipt.get("status") != "completed":
                    return _result({"receipt": receipt, "submission": submission, "observation": {"status": "not_requested"}})
                try:
                    parameters = deepcopy(observation["parameters"])
                    if parameters.get("after_request_id", request["request_id"]) != request["request_id"]:
                        raise ValueError("Observation must follow the submitted action")
                    parameters["after_request_id"] = request["request_id"]
                    metadata, data = self._observe(observation["kind"], parameters)
                    return _result({"receipt": receipt, "submission": submission, "observation": metadata}, data,
                                   error=metadata.get("status") == "failed")
                except (BackendError, OSError, TimeoutError, ValueError, AttributeError, TypeError) as error:
                    return _result({"receipt": receipt, "submission": submission, "observation": {"status": "failed", **self._error(error)}}, error=True)
            raise ValueError("Unknown tool")
        except (BackendError, OSError, TimeoutError, ValueError, KeyError, AttributeError, TypeError) as error:
            return _result(self._error(error), error=True)

    @staticmethod
    def _error(error):
        if isinstance(error, BackendError):
            return {"error": {"status": error.status, "code": error.code, "message": error.message}}
        if isinstance(error, (ValueError, KeyError)):
            return {"error": {"code": "invalid_arguments", "message": str(error) if isinstance(error, ValueError) else "Missing required field"}}
        if isinstance(error, (AttributeError, TypeError)):
            return {"error": {"code": "invalid_response", "message": "Invalid public backend response"}}
        return {"error": {"code": "transport_error", "message": type(error).__name__}}

    def emitted(self, result, *, success):
        if not any(item.get("type") == "image" for item in result.get("content", [])):
            return
        metadata = result["structuredContent"]
        if "observation" in metadata:
            metadata = metadata["observation"]
        self.record({"event": "image_emission", "state": "emitted" if success else "failed", **metadata})


def serve(adapter, source, target):
    """Serve bounded newline-delimited JSON-RPC; stdout contains only MCP."""
    initialized = ready = False
    names = {"observe_world", "observe_native_info", "retry_observation", "submit_action", "retry_action", "wait_action", "act_and_observe"}
    while True:
        line = source.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        call_id = None
        tool_result = None
        try:
            if len(line) > MAX_MESSAGE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = source.readline(MAX_MESSAGE_BYTES + 1)
                raise ValueError("Message exceeds 128 KiB")
            message = loads(line.decode("utf-8"))
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid JSON-RPC message")
            if "id" not in message:
                if message.get("method") == "notifications/initialized" and initialized:
                    ready = True
                continue
            candidate_id = message["id"]
            if type(candidate_id) not in (int, str):
                raise ValueError("Invalid JSON-RPC request ID")
            if isinstance(candidate_id, str):
                candidate_id.encode("utf-8")
            call_id = candidate_id
            method, params = message.get("method"), message.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("Invalid JSON-RPC request")
            if method == "initialize" and not initialized:
                if not isinstance(params.get("protocolVersion"), str):
                    raise ValueError("Missing MCP protocol version")
                initialized = True
                value = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                         "serverInfo": {"name": "openttd-capture-player", "version": "1"}}
            elif method == "ping":
                value = {}
            elif not ready:
                response = {"jsonrpc": "2.0", "id": call_id, "error": {"code": -32002, "message": "Server not initialized"}}
                value = None
            elif method == "tools/list":
                value = {"tools": tool_definitions()}
            elif method == "tools/call" and isinstance(params.get("name"), str) and params["name"] in names:
                _object(params, {"name", "arguments", "_meta"}, {"name"})
                value = tool_result = adapter.call(params["name"], params.get("arguments", {}), call_id=call_id)
            else:
                response = {"jsonrpc": "2.0", "id": call_id, "error": {"code": -32601, "message": "Unknown method or tool"}}
                value = None
            if value is not None:
                response = {"jsonrpc": "2.0", "id": call_id, "result": value}
        except (ValueError, UnicodeError, KeyError, RecursionError) as error:
            response = {"jsonrpc": "2.0", "id": call_id, "error": {"code": -32600, "message": "Invalid MCP message"}}
        encoded = (dumps(response) + "\n").encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            encoded = (dumps({"jsonrpc": "2.0", "id": call_id, "error": {"code": -32603, "message": "Tool result exceeds transport limit"}}) + "\n").encode()
            if tool_result is not None:
                adapter.emitted(tool_result, success=False)
            tool_result = None
        try:
            target.write(encoded)
            target.flush()
        except (BrokenPipeError, OSError):
            if tool_result is not None:
                adapter.emitted(tool_result, success=False)
            return 1
        if tool_result is not None:
            adapter.emitted(tool_result, success=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--audit", action="store_true", help="record bounded public request/image emission metadata")
    args = parser.parse_args(argv)
    client = BackendClient(args.origin, credential_from_env_or_file(args.token_file), timeout=5)
    return serve(CaptureTools(client, args.artifacts, audit=args.audit), sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
