"""Bounded local-development HTTP/SSE adapter for :mod:`backend.service`.

Release deployments should use a maintained ASGI server behind a TLS proxy.
This adapter never runs the simulation or keeps an event queue per connection.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .errors import ServiceError


MAX_BODY = 64 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MAX_STATIC = 256 * 1024
MAX_TARGET = 8192
MAX_HANDLERS = 96
MAX_STREAMS = 64
SOCKET_TIMEOUT = 10.0
STREAM_LIFETIME = 60.0
POLL_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 10.0
_BEARER = re.compile(r"Bearer ([^\s,]+)\Z", re.IGNORECASE)
_EVENT_FIELD = re.compile(r"[A-Za-z0-9_.-]+\Z")
_IMAGE_PATH = re.compile(r"/v1/images/([A-Za-z0-9_-]{1,128})\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
    "/viewer.css": ("viewer.css", "text/css; charset=utf-8"),
    "/events.mjs": ("events.mjs", "text/javascript; charset=utf-8"),
    "/world-status.mjs": ("world-status.mjs", "text/javascript; charset=utf-8"),
}


class _RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def read_static(path: str, root: Path | None = None) -> tuple[bytes, str]:
    """Read one exact viewer asset through a directory descriptor."""
    filename, content_type = _STATIC[path]
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    directory = os.open(_WEB_ROOT if root is None else root, directory_flags)
    try:
        descriptor = os.open(filename, flags, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as file:
            details = os.fstat(file.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_STATIC:
                raise ValueError("Invalid static asset")
            data = file.read(MAX_STATIC + 1)
            if len(data) > MAX_STATIC:
                raise ValueError("Static asset too large")
    finally:
        os.close(directory)
    return data, content_type


class BackendHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: object):
        self.service = service
        self._handler_slots = threading.BoundedSemaphore(MAX_HANDLERS)
        self.stream_slots = threading.BoundedSemaphore(MAX_STREAMS)
        super().__init__(address, BackendHandler)

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        if not self._handler_slots.acquire(blocking=False):
            try:
                request.settimeout(1.0)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


class BackendHandler(BaseHTTPRequestHandler):
    server: BackendHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT)

    def log_message(self, format: str, *args: object) -> None:
        # URLs, exception messages, and headers can contain credentials.
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def _json_response(self, status: int, payload: dict) -> None:
        try:
            data = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            data = b'{"error":{"code":"internal_error","message":"Internal server error"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _error(self, status: int, code: str, message: str) -> None:
        self._json_response(status, {"error": {"code": code, "message": message}})

    def _bytes_response(self, status: int, data: bytes, content_type: str, cache_control: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _static(self, path: str) -> None:
        try:
            data, content_type = read_static(path)
        except (OSError, ValueError):
            raise _RequestError(404, "not_found", "Resource not found") from None
        self._bytes_response(200, data, content_type, "no-cache")

    def _image(self, token: str, image_id: str) -> None:
        try:
            data = self.server.service.image(token, image_id)
        except ServiceError as exc:
            raise _RequestError(exc.status, exc.code, "Image unavailable") from None
        if not isinstance(data, bytes) or len(data) > MAX_IMAGE or not data.startswith(_PNG_SIGNATURE):
            raise _RequestError(500, "image_unavailable", "Image unavailable")
        self._bytes_response(200, data, "image/png", "no-store")

    def _token(self) -> str:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            raise _RequestError(401, "unauthorized", "Bearer credential required")
        match = _BEARER.fullmatch(values[0].strip())
        if match is None:
            raise _RequestError(401, "unauthorized", "Bearer credential required")
        return match.group(1)

    def _body(self, method: str) -> dict | None:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _RequestError(400, "invalid_request", "Transfer encoding is unsupported")
        values = self.headers.get_all("Content-Length", [])
        if len(values) > 1 or (values and (not values[0].isdigit() or len(values[0]) > 10)):
            raise _RequestError(400, "invalid_request", "Invalid content length")
        if not values:
            if method in ("POST", "PUT", "PATCH"):
                raise _RequestError(411, "length_required", "Content length required")
            return None
        length = int(values[0])
        if length > MAX_BODY:
            raise _RequestError(413, "body_too_large", "Request body exceeds 64 KiB")
        if not length:
            return None
        if method in ("GET", "HEAD"):
            raise _RequestError(400, "invalid_request", "Request body is unsupported")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise _RequestError(415, "unsupported_media_type", "Expected application/json")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise _RequestError(400, "invalid_json", "Incomplete JSON body")
            result = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError):
            raise _RequestError(400, "invalid_json", "Malformed JSON object") from None
        if not isinstance(result, dict):
            raise _RequestError(400, "invalid_json", "Expected JSON object")
        return result

    def _dispatch(self, method: str) -> None:
        try:
            if len(self.path) > MAX_TARGET:
                raise _RequestError(414, "uri_too_long", "Request target is too long")
            if method == "GET" and self.path in _STATIC:
                self._static(self.path)
                return
            token = self._token()
            try:
                split = urlsplit(self.path)
            except ValueError:
                raise _RequestError(400, "invalid_request", "Invalid request target") from None
            if split.scheme or split.netloc or split.fragment or not split.path.startswith("/"):
                raise _RequestError(400, "invalid_request", "Invalid request target")
            try:
                entries = parse_qsl(split.query, keep_blank_values=True, max_num_fields=64)
            except ValueError:
                raise _RequestError(400, "invalid_query", "Invalid query string") from None
            query = {}
            for key, value in entries:
                if key in query:
                    raise _RequestError(400, "invalid_query", "Duplicate query parameter")
                query[key] = value
            image_match = _IMAGE_PATH.fullmatch(split.path)
            if image_match is not None and method == "GET":
                self._body(method)
                if query:
                    raise _RequestError(400, "invalid_query", "Image query is unsupported")
                self._image(token, image_match.group(1))
                return
            if split.path == "/v1/events" and method == "GET" and self._wants_events():
                self._body(method)
                self._events(token, query)
                return
            body = self._body(method)
            status, payload = self.server.service.handle(method, split.path, token, body=body, query=query)
            self._json_response(status, payload)
        except _RequestError as exc:
            self._error(exc.status, exc.code, exc.message)
        except ServiceError as exc:
            self._error(exc.status, exc.code, exc.message)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._error(500, "internal_error", "Internal server error")

    def _events(self, token: str, query: dict[str, str]) -> None:
        if not self._wants_events():
            raise _RequestError(406, "not_acceptable", "Expected Accept: text/event-stream")
        ids = self.headers.get_all("Last-Event-ID", [])
        if len(ids) > 1:
            raise _RequestError(400, "invalid_cursor", "Invalid event cursor")
        after = ids[0] if ids else query.get("after")
        if after is not None and (len(after) > 128 or "\r" in after or "\n" in after):
            raise _RequestError(400, "invalid_cursor", "Invalid event cursor")
        if not self.server.stream_slots.acquire(blocking=False):
            raise _RequestError(503, "streams_busy", "Too many event streams")
        committed = False
        try:
            # Authenticate and validate the cursor before committing stream headers.
            page = self.server.service.events(token, after=after, limit=100)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            committed = True
            self.wfile.write(b"retry: 1500\n\n")
            deadline = time.monotonic() + STREAM_LIFETIME
            last_write = time.monotonic()
            while time.monotonic() < deadline:
                events = page.get("events", [])
                for event in events:
                    event_id = str(event["id"])
                    event_type = str(event.get("type", "message"))
                    if not _EVENT_FIELD.fullmatch(event_id) or not _EVENT_FIELD.fullmatch(event_type):
                        continue
                    data = json.dumps(event.get("data", {}), separators=(",", ":"), allow_nan=False)
                    frame = f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode("utf-8")
                    if len(frame) > MAX_BODY:
                        return
                    self.wfile.write(frame)
                    after = event_id
                    last_write = time.monotonic()
                after = page.get("cursor", after)
                self.wfile.flush()
                if not events and time.monotonic() - last_write >= HEARTBEAT_INTERVAL:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                if time.monotonic() >= deadline:
                    break
                remaining = max(0.0, min(deadline - time.monotonic(), HEARTBEAT_INTERVAL - (time.monotonic() - last_write)))
                waiter = getattr(self.server.service, "wait_for_events", None)
                if waiter is None:
                    time.sleep(min(POLL_INTERVAL, remaining))
                    page = self.server.service.events(token, after=after, limit=100)
                else:
                    started_wait = time.monotonic()
                    page = waiter(token, after, remaining)
                    if (not page.get("events") and page.get("cursor") == after and
                        time.monotonic() - started_wait < 0.01):
                        # Protect against spurious wakeups without adding a
                        # polling delay to a real persisted event.
                        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        except Exception:
            if not committed:
                raise
            # A stream already has HTTP headers; close it and let clients resume.
        finally:
            self.close_connection = True
            self.server.stream_slots.release()

    def _wants_events(self) -> bool:
        accepts = self.headers.get_all("Accept", [])
        return any(
            part.split(";", 1)[0].strip().lower() == "text/event-stream"
            for header in accepts for part in header.split(",")
        )


def create_server(service: object, host: str = "127.0.0.1", port: int = 8080) -> BackendHTTPServer:
    """Create a bounded HTTP server; the caller owns serve_forever and close."""
    return BackendHTTPServer((host, port), service)
