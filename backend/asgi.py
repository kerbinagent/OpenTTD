"""Bounded ASGI transport for a single authenticated OpenTTD service.

The domain service remains synchronous and owns authorization and projection.
Uvicorn should run one worker per world behind a TLS-terminating proxy.
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import parse_qsl

import anyio
from anyio import to_thread
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from .agent_assets import build_agent_assets
from .errors import ServiceError
from .http import (
    CAPTURE_SLOTS, HEARTBEAT_INTERVAL, MAX_BODY, MAX_IMAGE, MAX_TARGET, STREAM_LIFETIME,
    _EVENT_FIELD, _IMAGE_PATH, _PNG_SIGNATURE, _STATIC, _WEB_ROOT,
    _capture_path, _observation_headers, read_static,
)
from .json_tools import dumps, loads
from .observations import ObservationImage


API_SLOTS = 64
IMAGE_SLOTS = 64
IMAGE_LOOKUP_THREADS = 8
CAPTURE_THREADS = 8
STREAM_SLOTS = 64
BODY_TIMEOUT = 10.0
SEND_TIMEOUT = 5.0
EVENT_WAIT = 2.0
_BEARER = re.compile(rb"Bearer ([^\s,]+)\Z", re.IGNORECASE)
_SENSITIVE_QUERY = {"token", "access_token", "authorization", "api_key", "bearer"}
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"]


class _Problem(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _Admission:
    """Immediate admission, independent of AnyIO's worker-thread limiter."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0
        self.lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self.lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self.lock:
            self.active -= 1


class _TimedResponse:
    """Hold an admission slot through body delivery, then release it."""

    def __init__(self, response: Response, slots: _Admission):
        self.response, self.slots = response, slots

    async def __call__(self, scope, receive, send) -> None:
        try:
            try:
                await asyncio.wait_for(self.response(scope, receive, send), SEND_TIMEOUT)
            except (TimeoutError, OSError, ConnectionError):
                # A slow or disconnected reader cannot retain an image/API slot.
                pass
        finally:
            await self.slots.release()


async def _capture_call(service: object, args: tuple, threads: anyio.CapacityLimiter):
    """Keep capture capacity reserved until a cancelled request's worker exits."""
    worker = asyncio.create_task(to_thread.run_sync(service.handle, *args, limiter=threads))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Raw asyncio cancellation can otherwise abandon an AnyIO worker and
        # release its limiter while the synchronous capture is still running.
        with anyio.CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        if not worker.cancelled():
            worker.exception()
        raise


def _json_response(status: int, payload: dict) -> Response:
    try:
        data = dumps(payload).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        status = 500
        data = b'{"error":{"code":"internal_error","message":"Internal server error"}}'
    return Response(data, status_code=status, headers={
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    })


def _error(status: int, code: str, message: str) -> Response:
    return _json_response(status, {"error": {"code": code, "message": message}})


def _path_and_query(request: Request) -> tuple[str, dict[str, str]]:
    raw_path = request.scope.get("raw_path")
    if not isinstance(raw_path, bytes):
        raw_path = request.scope["path"].encode("utf-8")
    raw_query = request.scope.get("query_string", b"")
    if len(raw_path) + len(raw_query) > MAX_TARGET:
        raise _Problem(414, "uri_too_long", "Request target is too long")
    try:
        path = raw_path.decode("ascii")
        query_string = raw_query.decode("ascii")
        pairs = parse_qsl(query_string, keep_blank_values=True, max_num_fields=64, errors="strict")
    except (UnicodeError, ValueError):
        raise _Problem(400, "invalid_query", "Invalid request target or query") from None
    if not path.startswith("/") or "#" in path or "?" in path:
        raise _Problem(400, "invalid_request", "Invalid request target")
    query = {}
    for key, value in pairs:
        if key in query:
            raise _Problem(400, "invalid_query", "Duplicate query parameter")
        if key.casefold() in _SENSITIVE_QUERY:
            raise _Problem(400, "invalid_query", "Credential query parameter is unsupported")
        query[key] = value
    return path, query


def _header_values(request: Request, name: bytes) -> list[bytes]:
    return [value for key, value in request.scope.get("headers", []) if key.lower() == name]


def _token(request: Request) -> str:
    values = _header_values(request, b"authorization")
    if len(values) != 1 or len(values[0]) > 520:
        raise _Problem(401, "unauthorized", "Bearer credential required")
    match = _BEARER.fullmatch(values[0].strip())
    if match is None or len(match.group(1)) > 512:
        raise _Problem(401, "unauthorized", "Bearer credential required")
    try:
        return match.group(1).decode("ascii")
    except UnicodeDecodeError:
        raise _Problem(401, "unauthorized", "Bearer credential required") from None


async def _body(request: Request) -> dict | None:
    values = _header_values(request, b"content-length")
    if len(values) > 1 or (values and (not values[0].isdigit() or len(values[0]) > 10)):
        raise _Problem(400, "invalid_request", "Invalid content length")
    if values and int(values[0]) > MAX_BODY:
        raise _Problem(413, "body_too_large", "Request body exceeds 64 KiB")
    if request.method in ("GET", "HEAD") and values and int(values[0]) > 0:
        raise _Problem(400, "invalid_request", "Request body is unsupported")
    data = bytearray()
    try:
        with anyio.fail_after(BODY_TIMEOUT):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_BODY:
                    raise _Problem(413, "body_too_large", "Request body exceeds 64 KiB")
    except TimeoutError:
        raise _Problem(408, "request_timeout", "Request body timed out") from None
    except ClientDisconnect:
        raise _Problem(400, "invalid_request", "Request body was interrupted") from None
    if not data:
        return None
    if request.method in ("GET", "HEAD"):
        raise _Problem(400, "invalid_request", "Request body is unsupported")
    content_types = _header_values(request, b"content-type")
    if len(content_types) != 1 or content_types[0].split(b";", 1)[0].strip().lower() != b"application/json":
        raise _Problem(415, "unsupported_media_type", "Expected application/json")
    try:
        body = loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise _Problem(400, "invalid_json", "Malformed JSON object") from None
    if not isinstance(body, dict):
        raise _Problem(400, "invalid_json", "Expected JSON object")
    return body


def _wants_events(request: Request) -> bool:
    return any(
        part.split(b";", 1)[0].strip().lower() == b"text/event-stream"
        for header in _header_values(request, b"accept") for part in header.split(b",")
    )


def _event_cursor(request: Request, query: dict[str, str]) -> str | None:
    values = _header_values(request, b"last-event-id")
    if len(values) > 1:
        raise _Problem(400, "invalid_cursor", "Invalid event cursor")
    try:
        after = values[0].decode("ascii") if values else query.get("after")
    except UnicodeDecodeError:
        raise _Problem(400, "invalid_cursor", "Invalid event cursor") from None
    if after is not None and (len(after) > 128 or not _EVENT_FIELD.fullmatch(after)):
        raise _Problem(400, "invalid_cursor", "Invalid event cursor")
    return after


class _SSEStream:
    def __init__(self, request: Request, service: object, token: str, after: str | None,
                 page: dict, slots: _Admission, threads: anyio.CapacityLimiter):
        self.request, self.service, self.token = request, service, token
        self.after, self.page, self.slots, self.threads = after, page, slots, threads

    async def __call__(self, scope, receive, send) -> None:
        async def write(body: bytes, more: bool = True) -> None:
            await asyncio.wait_for(send({"type": "http.response.body", "body": body, "more_body": more}), SEND_TIMEOUT)

        try:
            await asyncio.wait_for(send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache, no-store"),
                (b"x-accel-buffering", b"no"),
                (b"x-content-type-options", b"nosniff"),
            ]}), SEND_TIMEOUT)
            await write(b"retry: 1500\n\n")
            deadline = time.monotonic() + STREAM_LIFETIME
            last_write = time.monotonic()
            while time.monotonic() < deadline:
                if await self.request.is_disconnected():
                    break
                events = self.page.get("events", [])
                for event in events:
                    event_id = str(event["id"])
                    event_type = str(event.get("type", "message"))
                    if not _EVENT_FIELD.fullmatch(event_id) or not _EVENT_FIELD.fullmatch(event_type):
                        continue
                    data = dumps(event.get("data", {}))
                    frame = f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode("utf-8")
                    if len(frame) > MAX_BODY:
                        return
                    await write(frame)
                    self.after = event_id
                    last_write = time.monotonic()
                self.after = self.page.get("cursor", self.after)
                now = time.monotonic()
                if not events and now - last_write >= HEARTBEAT_INTERVAL:
                    await write(b": heartbeat\n\n")
                    last_write = time.monotonic()
                if now >= deadline:
                    break
                wait = min(EVENT_WAIT, max(0.0, deadline - now),
                           max(0.0, HEARTBEAT_INTERVAL - (now - last_write)))
                started = time.monotonic()
                self.page = await to_thread.run_sync(
                    self.service.wait_for_events, self.token, self.after, wait,
                    limiter=self.threads,
                )
                if (not self.page.get("events") and self.page.get("cursor") == self.after and
                    time.monotonic() - started < 0.01):
                    await asyncio.sleep(0.05)
            await write(b"", more=False)
        except (TimeoutError, OSError, ConnectionError, ServiceError, ValueError, KeyError, TypeError):
            # Once headers are sent, closing is the only safe error response.
            pass
        finally:
            await self.slots.release()


def create_app(service: object, web_root=None) -> Starlette:
    """Build a transport-only app; the caller owns Service and gateway lifecycle."""
    root = _WEB_ROOT if web_root is None else web_root
    assets = {path: read_static(path, root) for path in _STATIC}
    agent_assets = build_agent_assets()
    api_slots = _Admission(API_SLOTS)
    image_slots = _Admission(IMAGE_SLOTS)
    capture_slots = _Admission(CAPTURE_SLOTS)
    stream_slots = _Admission(STREAM_SLOTS)
    api_threads = anyio.CapacityLimiter(API_SLOTS)
    image_threads = anyio.CapacityLimiter(IMAGE_LOOKUP_THREADS)
    capture_threads = anyio.CapacityLimiter(CAPTURE_THREADS)
    stream_threads = anyio.CapacityLimiter(STREAM_SLOTS)

    async def dispatch(request: Request):
        slots = None
        try:
            raw_path = request.scope.get("raw_path", b"")
            agent_path = next((path for path in agent_assets if raw_path == path.encode("ascii")), None)
            if agent_path is not None:
                _, query = _path_and_query(request)
                if query or request.scope.get("query_string"):
                    raise _Problem(400, "invalid_query", "Agent asset query is unsupported")
                if request.method not in ("GET", "HEAD"):
                    raise _Problem(405, "method_not_allowed", "Agent assets support GET and HEAD")
                if _header_values(request, b"transfer-encoding"):
                    raise _Problem(400, "invalid_request", "Transfer encoding is unsupported")
                await _body(request)
                data, content_type = agent_assets[agent_path]
                return Response(b"" if request.method == "HEAD" else data, headers={
                    "Content-Type": content_type, "Cache-Control": "no-cache",
                    "Content-Length": str(len(data)),
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                })
            public_path = next((path for path in assets if raw_path == path.encode("ascii")), None)
            if request.method == "GET" and public_path is not None and not request.scope.get("query_string"):
                data, content_type = assets[public_path]
                return Response(data, headers={
                    "Content-Type": content_type, "Cache-Control": "no-cache",
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                })
            token = None if request.method == "GET" and raw_path == b"/v1/model-reports" else _token(request)
            path, query = _path_and_query(request)
            if path == "/v1/events" and request.method == "GET" and _wants_events(request):
                if not await stream_slots.acquire():
                    raise _Problem(503, "streams_busy", "Too many event streams")
                slots = stream_slots
                await _body(request)
                after = _event_cursor(request, query)
                page = await to_thread.run_sync(service.events, token, after, 100, limiter=stream_threads)
                response = _SSEStream(request, service, token, after, page, stream_slots, stream_threads)
                slots = None
                return response
            image_match = _IMAGE_PATH.fullmatch(path)
            if request.method == "GET" and image_match is not None:
                if not await image_slots.acquire():
                    raise _Problem(503, "images_busy", "Too many image requests")
                slots = image_slots
                await _body(request)
                if query:
                    raise _Problem(400, "invalid_query", "Image query is unsupported")
                try:
                    data = await to_thread.run_sync(service.image, token, image_match.group(1), limiter=image_threads)
                except ServiceError as exc:
                    raise _Problem(exc.status, exc.code, "Image unavailable") from None
                if not isinstance(data, bytes) or len(data) > MAX_IMAGE or not data.startswith(_PNG_SIGNATURE):
                    raise _Problem(500, "image_unavailable", "Image unavailable")
                response = Response(data, headers={
                    "Content-Type": "image/png", "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                })
                slots = None
                return _TimedResponse(response, image_slots)
            capture_path = _capture_path(path)
            capture = request.method == "POST" and capture_path is not None
            slots = capture_slots if capture else api_slots
            if not await slots.acquire():
                slots = None
                if capture:
                    raise _Problem(503, "captures_busy", "Too many capture requests")
                raise _Problem(503, "server_busy", "Too many requests")
            body = await _body(request)
            args = (request.method, path, token, body, query)
            if capture:
                status, payload = await _capture_call(service, args, capture_threads)
            else:
                status, payload = await to_thread.run_sync(service.handle, *args, limiter=api_threads)
            if isinstance(payload, ObservationImage):
                if status != 200 or request.method != "POST":
                    raise _Problem(500, "image_unavailable", "Image unavailable")
                try:
                    headers = _observation_headers(payload, capture_path or path)
                except (TypeError, ValueError):
                    raise _Problem(500, "image_unavailable", "Image unavailable") from None
                response = Response(payload.data, headers={
                    "Content-Type": "image/png", "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                    **headers,
                })
            else:
                response = _json_response(status, payload)
            response_slots = slots
            slots = None
            return _TimedResponse(response, response_slots)
        except _Problem as exc:
            response = _error(exc.status, exc.code, exc.message)
        except ServiceError as exc:
            response = _error(exc.status, exc.code, exc.message)
        except asyncio.CancelledError:
            if slots is not None:
                await slots.release()
            raise
        except Exception:
            response = _error(500, "internal_error", "Internal server error")
        if slots is not None:
            return _TimedResponse(response, slots)
        return response

    routes = [Route("/", dispatch, methods=_METHODS), Route("/{path:path}", dispatch, methods=_METHODS)]
    app = Starlette(debug=False, routes=routes)
    app.router.redirect_slashes = False
    app.state.api_slots = api_slots
    app.state.api_threads = api_threads
    app.state.image_slots = image_slots
    app.state.image_lookup_threads = image_threads
    app.state.capture_slots = capture_slots
    app.state.capture_threads = capture_threads
    app.state.stream_slots = stream_slots
    return app
