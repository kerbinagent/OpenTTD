"""Dependency-free reference client for the public HTTP API.

Incoming SSE data is passed to an application callback. This module never
interprets message bodies as instructions or sends replies automatically.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import time
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


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
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Remote credential transport requires HTTPS")
        if not token or any(char.isspace() for char in token):
            raise ValueError("Invalid bearer credential")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

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
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raise self._error(exc) from None

    def image(self, image_id: str) -> bytes:
        """Fetch one authenticated PNG without exposing a credential in its URL."""
        if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
            raise ValueError("Image ID must be 32 hexadecimal characters")
        req = Request(
            self._url(f"/v1/images/{image_id}"),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "image/png"},
            method="GET",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "image/png":
                    raise BackendError(response.status, "invalid_image", "Invalid PNG response")
                data = response.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise BackendError(response.status, "image_too_large", "Image response exceeds 16 MiB")
                if not data.startswith(_PNG_SIGNATURE):
                    raise BackendError(response.status, "invalid_image", "Invalid PNG response")
                return data
        except HTTPError as exc:
            raise self._error(exc) from None

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
