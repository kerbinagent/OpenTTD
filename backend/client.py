"""Dependency-free reference client for the public HTTP API.

Incoming SSE data is passed to an application callback. This module never
interprets message bodies as instructions or sends replies automatically.
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import socket
import stat
import struct
import time
import uuid
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .observations import ObservationImage, validate_request


MAX_IMAGE_BYTES = 16 * 1024 * 1024
_IMAGE_ID = re.compile(r"[0-9a-fA-F]{32}\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BackendError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        # urllib normally forwards Authorization to redirects, including other hosts.
        return None


def credential_from_env_or_file(path: str | None = None) -> str:
    """Load a bearer secret without putting it in command-line arguments."""
    if path is not None:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            details = os.fstat(file.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > 1025 or (
                os.name == "posix" and (details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077)
            ):
                raise ValueError("Credential file must be an owner-only regular file")
            token = file.read(1025).strip()
    else:
        token = os.environ.get("OPENTTD_BACKEND_TOKEN", "")
    if not token or len(token) > 1024 or any(char.isspace() for char in token):
        raise ValueError("Provide OPENTTD_BACKEND_TOKEN or a private credential file")
    return token


class BackendClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment or
            parsed.username is not None or parsed.password is not None):
            raise ValueError("Base URL must be an HTTP(S) origin")
        tailnet_http = False
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            try:
                tailnet = ipaddress.ip_address(parsed.hostname) in ipaddress.ip_network("100.64.0.0/10")
            except ValueError:
                tailnet = False
            if not tailnet or os.environ.get("OPENTTD_ALLOW_TAILSCALE_HTTP") != "1":
                raise ValueError("Remote credential transport requires HTTPS; Tailscale IPv4 HTTP requires OPENTTD_ALLOW_TAILSCALE_HTTP=1")
            tailnet_http = True
        if not token or any(char.isspace() for char in token):
            raise ValueError("Invalid bearer credential")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect(), *([ProxyHandler({})] if tailnet_http else []))
        self._observation_capability = None
        self.on_http_attempt = None

    def _exchange(self, request, read):
        """One HTTP attempt, with optional metadata-only measurement."""
        attempt_id = uuid.uuid4().hex
        request.add_header("X-OpenTTD-Attempt-Id", attempt_id)
        started = time.monotonic()
        status = None
        result = None
        failure = None
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                result = read(response)
                return result
        except HTTPError as exc:
            status = exc.code
            failure = "http_error"
            raise self._error(exc) from None
        except Exception as exc:
            failure = type(exc).__name__
            raise
        finally:
            if self.on_http_attempt is not None:
                record = {"attempt_id": attempt_id, "method": request.get_method(),
                          "path": urlsplit(request.full_url).path, "status": status,
                          "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
                if isinstance(result, bytes):
                    record["response_bytes"] = len(result)
                elif isinstance(result, ObservationImage):
                    record["response_bytes"] = len(result.data)
                if failure is not None:
                    record["error_type"] = failure
                try:
                    self.on_http_attempt(record)
                except Exception:
                    # Measurement must not change a known gameplay outcome.
                    pass

    def _url(self, path: str, query: dict | None = None) -> str:
        parsed = urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parsed.netloc or parsed.fragment:
            raise ValueError("Path must be relative to the API origin")
        if query:
            separator = "&" if "?" in path else "?"
            path += separator + urlencode(query)
        return self.base_url + path

    def _error(self, response: HTTPError) -> BackendError:
        try:
            body = json.load(response)
            details = body["error"]
            return BackendError(response.code, str(details["code"]), str(details["message"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            return BackendError(response.code, "http_error", "Backend request failed")

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        if body is not None and not isinstance(body, dict):
            raise ValueError("Body must be a JSON object")
        data = None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self._url(path, query), data=data, headers=headers, method=method.upper())
        return self._exchange(req, json.load)

    def new_observation_request(self, kind: str, parameters: dict) -> dict:
        """Create a new read intent; retain the returned body for exact retries."""
        if kind not in ("world", "native_info") or not isinstance(parameters, dict):
            raise ValueError("Expected world or native_info observation parameters")
        if {"observation_generation", "observation_request_id"} & parameters.keys():
            raise ValueError("New observation identities are generated by the client")
        if self._observation_capability is None:
            bootstrap = self.request("GET", "/v1/capabilities")
            capability = bootstrap.get("selected_observations") if isinstance(bootstrap, dict) else None
            if (not isinstance(capability, dict) or
                    not isinstance(capability.get("observation_generation"), str) or
                    not re.fullmatch(r"[0-9a-f]{32}", capability["observation_generation"])):
                raise BackendError(409, "observations_unavailable", "Selected observations are unavailable")
            self._observation_capability = capability
        request = {**parameters,
                   "observation_generation": self._observation_capability["observation_generation"],
                   "observation_request_id": f"obs1:{time.time_ns() // 1_000_000:013d}:{uuid.uuid4().hex}"}
        return validate_request(kind, request)

    def reset_observation_context(self) -> None:
        """Refresh bootstrap for a new read intent after an explicit expiry."""
        self._observation_capability = None

    def _observe(self, kind: str, body: dict) -> ObservationImage | dict:
        body = validate_request(kind, body)
        endpoint = "/v1/view/observe/" + ("world" if kind == "world" else "native-info")
        req = Request(self._url(endpoint), method="POST",
                      data=json.dumps(body, allow_nan=False, separators=(",", ":")).encode(),
                      headers={"Authorization": "Bearer " + self.token,
                               "Content-Type": "application/json", "Accept": "image/png"})

        def read(response):
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if response.status == 202 and content_type == "application/json":
                data = response.read(65_537)
                if len(data) > 65_536:
                    raise BackendError(202, "invalid_observation", "Pending response exceeds its limit")
                try:
                    pending = json.loads(data)
                except (ValueError, UnicodeError):
                    raise BackendError(202, "invalid_observation", "Invalid pending observation") from None
                allowed = {"status", "observation_request_id", "reason", "retry_after_ms"}
                if (not isinstance(pending, dict) or set(pending) != allowed or
                        pending["status"] != "pending" or
                        pending["observation_request_id"] != body["observation_request_id"] or
                        not isinstance(pending["reason"], str) or
                        pending["reason"] not in {"action_pending", "replica_pending", "renderer_busy", "rate_limited"} or
                        type(pending["retry_after_ms"]) is not int or
                        not 0 <= pending["retry_after_ms"] <= 2_000):
                    raise BackendError(202, "invalid_observation", "Invalid pending observation")
                return pending
            if response.status != 200 or content_type != "image/png":
                raise BackendError(response.status, "invalid_observation", "Expected an observation PNG")
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise BackendError(200, "image_too_large", "Image response exceeds 16 MiB")
            try:
                # The gateway checks the complete metadata-free PNG. Check the
                # public envelope and dimensions again at the client boundary.
                if (len(data) < 45 or not data.startswith(_PNG_SIGNATURE) or
                        data[8:16] != b"\x00\x00\x00\rIHDR" or
                        data[24:29] != b"\x08\x02\x00\x00\x00" or
                        data[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82"):
                    raise ValueError("Invalid PNG header")
                width, height = struct.unpack(">II", data[16:24])
                def header(name):
                    values = response.headers.get_all("X-OpenTTD-" + name, [])
                    if len(values) != 1:
                        raise ValueError("Missing or repeated observation header")
                    return values[0]
                def number(name, maximum):
                    value = header(name)
                    if not re.fullmatch(r"0|[1-9][0-9]{0,12}", value) or int(value) > maximum:
                        raise ValueError("Invalid numeric observation header")
                    return int(value)
                def identity(name):
                    value = header(name)
                    if not re.fullmatch(r"[0-9a-f]{32}", value):
                        raise ValueError("Invalid observation identity")
                    return value
                generation = identity("Observation-Generation")
                if (generation != body["observation_generation"] or
                        header("Capture-Kind") != kind or header("Image-Purpose") != "observation" or
                        number("Image-Width", 1_280) != width or number("Image-Height", 1_024) != height or
                        width < 1 or height < 1 or width * height > 1_048_576):
                    raise ValueError("Mismatched observation metadata")
                if kind == "world" and (width, height) != (body["output"]["width"], body["output"]["height"]):
                    raise ValueError("Observation dimensions differ from requested output")
                causal = header("Causal-State")
                if causal != ("satisfied" if "after_request_id" in body else "none"):
                    raise ValueError("Unexpected causal state")
                reused = header("Image-Reused")
                if reused not in ("true", "false"):
                    raise ValueError("Invalid reuse metadata")
                return ObservationImage(data, identity("Observation-Id"), identity("Image-Id"),
                                        generation, kind, width, height, causal,
                                        reused == "true", number("Image-Age-Ms", 65_000))
            except (ValueError, TypeError, struct.error):
                raise BackendError(200, "invalid_observation", "Invalid observation image or metadata") from None

        return self._exchange(req, read)

    def observe_world(self, request: dict) -> ObservationImage | dict:
        return self._observe("world", request)

    def observe_native_info(self, request: dict) -> ObservationImage | dict:
        return self._observe("native_info", request)

    def world_status(self) -> dict:
        """Read the current shared pause snapshot and its event cursor."""
        return self.request("GET", "/v1/world/status")

    def image(self, image_id: str) -> bytes:
        """Fetch one authenticated PNG without exposing a credential in its URL."""
        if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
            raise ValueError("Image ID must be 32 hexadecimal characters")
        req = Request(
            self._url(f"/v1/images/{image_id}"),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "image/png"},
            method="GET",
        )

        def read(response):
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "image/png":
                raise BackendError(response.status, "invalid_image", "Invalid PNG response")
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise BackendError(response.status, "image_too_large", "Image response exceeds 16 MiB")
            if not data.startswith(_PNG_SIGNATURE):
                raise BackendError(response.status, "invalid_image", "Invalid PNG response")
            return data

        return self._exchange(req, read)

    def iter_events(
        self, after: str | None = None, *, reconnect: bool = True,
        stop: Callable[[], bool] | None = None,
    ) -> Iterator[dict]:
        """Yield projected events, resuming with the last received event ID."""
        while stop is None or not stop():
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"}
            if after is not None:
                headers["Last-Event-ID"] = str(after)
            req = Request(self._url("/v1/events"), headers=headers)
            try:
                with self._opener.open(req, timeout=self.timeout) as response:
                    current: dict[str, str] = {}
                    data_lines: list[str] = []
                    while stop is None or not stop():
                        line = response.readline(65537)
                        if not line:
                            break
                        if len(line) > 65536:
                            raise BackendError(0, "invalid_stream", "Event line too large")
                        line = line.decode("utf-8").rstrip("\r\n")
                        if not line:
                            if data_lines:
                                try:
                                    payload = json.loads("\n".join(data_lines))
                                except ValueError:
                                    raise BackendError(0, "invalid_stream", "Malformed event data") from None
                                event_id = current.get("id")
                                if event_id is not None:
                                    after = event_id
                                yield {"id": event_id, "type": current.get("event", "message"), "data": payload}
                            current, data_lines = {}, []
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip(" "))
                            if sum(len(part) for part in data_lines) > 65536:
                                raise BackendError(0, "invalid_stream", "Event data too large")
                        elif line.startswith("id:"):
                            current["id"] = line[3:].lstrip(" ")
                        elif line.startswith("event:"):
                            current["event"] = line[6:].lstrip(" ")
            except HTTPError as exc:
                raise self._error(exc) from None
            except (URLError, OSError, socket.timeout):
                if not reconnect:
                    raise
            if not reconnect:
                return
            if stop is None or not stop():
                time.sleep(1.5)

    def events(
        self, on_event: Callable[[dict], None], after: str | None = None, *,
        reconnect: bool = True, stop: Callable[[], bool] | None = None,
    ) -> None:
        """Run the incoming event loop, invoking only the caller's callback."""
        for event in self.iter_events(after, reconnect=reconnect, stop=stop):
            on_event(event)
