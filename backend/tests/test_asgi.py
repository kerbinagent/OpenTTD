"""ASGI transport tests; automatically skipped without optional ASGI deps."""

import asyncio
import json
import threading
import time
import unittest

try:
    from backend import asgi
except ImportError:
    asgi = None

from backend.errors import ServiceError
from backend.service import Service
from backend.store import Store


class StubService:
    def handle(self, method, path, token, body=None, query=None):
        if token != "secret":
            raise ServiceError("unauthorized", status=401)
        if path == "/v1/fail":
            raise RuntimeError("internal artifact path and tick")
        return 200, {"method": method, "path": path, "body": body, "query": query}

    def image(self, token, image_id):
        if token != "secret":
            raise ServiceError("image_unavailable", "secret path and tick", 404)
        return b"\x89PNG\r\n\x1a\nexample"

    def events(self, token, after=None, limit=100):
        if token != "secret":
            raise ServiceError("unauthorized", status=401)
        events = [] if after else [{"id": "cursor-1", "type": "chat.message", "data": {"body": "hello"}}]
        return {"events": events, "cursor": "cursor-1"}

    def wait_for_events(self, token, after=None, timeout=1):
        time.sleep(min(timeout, 0.02))
        return self.events(token, after)


async def call(app, path="/v1/me", *, method="GET", token="secret", headers=(), body=b"", raw_path=None,
               query=b"", slow_send=False, body_gate=None, on_body=None, receive_gate=None):
    raw_path = path.encode("ascii") if raw_path is None else raw_path
    all_headers = list(headers)
    if token is not None:
        all_headers.append((b"authorization", b"Bearer " + token.encode()))
    if body:
        all_headers.extend(((b"content-length", str(len(body)).encode()),
                            (b"content-type", b"application/json")))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": raw_path, "query_string": query, "root_path": "",
             "headers": all_headers, "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8080)}
    sent = False
    messages = []

    async def receive():
        nonlocal sent
        if not sent:
            if receive_gate is not None:
                await receive_gate.wait()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        if message["type"] == "http.response.body":
            if on_body is not None:
                on_body()
            if body_gate is not None:
                await body_gate.wait()
            if slow_send:
                await asyncio.sleep(10)
        messages.append(message)

    await app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    payload = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    response_headers = dict(next(message["headers"] for message in messages if message["type"] == "http.response.start"))
    return status, payload, response_headers


@unittest.skipIf(asgi is None, "ASGI dependencies not installed")
class ASGITransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = asgi.create_app(StubService())

    async def test_exact_static_and_auth(self):
        status, body, _ = await call(self.app, "/viewer.js", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"fetch(", body)
        status, body, _ = await call(self.app, "/world-status.mjs", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"parseWorldStatus", body)
        status, _, _ = await call(self.app, "/viewer.js", raw_path=b"/viewer%2Ejs", token=None)
        self.assertEqual(status, 401)
        status, _, _ = await call(self.app, "/extra.js", token=None)
        self.assertEqual(status, 401)
        status, _, _ = await call(self.app, "/v1/me", token=None)
        self.assertEqual(status, 401)
        status, _, _ = await call(self.app, headers=((b"authorization", b"Bearer other"),))
        self.assertEqual(status, 401)

    async def test_json_validation_query_and_error_redaction(self):
        for body in (b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b"{"):
            with self.subTest(body=body):
                status, _, _ = await call(self.app, "/v1/test", method="POST", body=body)
                self.assertEqual(status, 400)
        status, _, _ = await call(self.app, "/v1/test", method="POST", body=b"{" + b" " * 65536)
        self.assertEqual(status, 413)
        status, _, _ = await call(self.app, query=b"a=1&a=2")
        self.assertEqual(status, 400)
        status, _, _ = await call(self.app, query=b"token=secret")
        self.assertEqual(status, 400)
        status, body, _ = await call(self.app, "/v1/fail")
        self.assertEqual(status, 500)
        self.assertNotIn(b"artifact path", body)

    async def test_binary_image_and_sse_resume(self):
        status, body, headers = await call(self.app, "/v1/images/image_1")
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"image/png")
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        status, ranged, range_headers = await call(self.app, "/v1/images/image_1",
                                                    headers=((b"range", b"bytes=0-2"),))
        self.assertEqual(status, 200)
        self.assertEqual(ranged, body)
        self.assertNotIn(b"etag", range_headers)
        self.assertNotIn(b"accept-ranges", range_headers)
        status, body, _ = await call(self.app, "/v1/images/image_1", token="bad")
        self.assertEqual(status, 404)
        self.assertNotIn(b"secret path", body)
        previous = asgi.STREAM_LIFETIME
        asgi.STREAM_LIFETIME = 0.05
        try:
            status, body, _ = await call(self.app, "/v1/events", headers=((b"accept", b"text/event-stream"),))
            self.assertEqual(status, 200)
            self.assertIn(b"id: cursor-1\n", body)
            status, body, _ = await call(self.app, "/v1/events", headers=(
                (b"accept", b"text/event-stream"), (b"last-event-id", b"cursor-1")))
            self.assertEqual(status, 200)
            self.assertNotIn(b"event: chat.message", body)
        finally:
            asgi.STREAM_LIFETIME = previous

    async def test_cached_image_burst_admits_54_and_503_at_65(self):
        calls = []
        call_lock = threading.Lock()

        class CachedImage(StubService):
            def image(self, token, image_id):
                with call_lock:
                    calls.append(image_id)
                return super().image(token, image_id)

        app = asgi.create_app(CachedImage())
        gate = asyncio.Event()
        bodies_started = 0

        def on_body():
            nonlocal bodies_started
            bodies_started += 1

        tasks = [asyncio.create_task(call(app, "/v1/images/image_1", body_gate=gate, on_body=on_body))
                 for _ in range(54)]
        try:
            for _ in range(300):
                if bodies_started == 54:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(bodies_started, 54)
            self.assertEqual(app.state.image_slots.active, 54)
            self.assertEqual(len(calls), 54)
            self.assertEqual(app.state.image_lookup_threads.borrowed_tokens, 0)
            status, _, _ = await call(app, "/v1/me")
            self.assertEqual(status, 200)
            previous = asgi.STREAM_LIFETIME
            asgi.STREAM_LIFETIME = 0.05
            try:
                status, _, _ = await call(app, "/v1/events", headers=((b"accept", b"text/event-stream"),))
                self.assertEqual(status, 200)
            finally:
                asgi.STREAM_LIFETIME = previous
            tasks.extend(asyncio.create_task(call(app, "/v1/images/image_1", body_gate=gate, on_body=on_body))
                         for _ in range(10))
            for _ in range(300):
                if bodies_started == 64:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(bodies_started, 64)
            self.assertEqual(app.state.image_slots.active, 64)
            status, _, _ = await call(app, "/v1/images/image_1")
            self.assertEqual(status, 503)
            self.assertEqual(len(calls), 64)
        finally:
            gate.set()
            results = await asyncio.gather(*tasks)
        self.assertTrue(all(status == 200 for status, _, _ in results))
        self.assertEqual(app.state.image_slots.active, 0)
        self.assertEqual(app.state.image_lookup_threads.borrowed_tokens, 0)

    async def test_image_lookup_threads_cap_at_eight_without_blocking_api(self):
        started = 0
        lock = threading.Lock()
        release = threading.Event()

        class BlockingImage(StubService):
            def image(self, token, image_id):
                nonlocal started
                with lock:
                    started += 1
                release.wait(3)
                return super().image(token, image_id)

        app = asgi.create_app(BlockingImage())
        tasks = [asyncio.create_task(call(app, "/v1/images/image_1")) for _ in range(9)]
        try:
            for _ in range(200):
                with lock:
                    count = started
                if count == 8:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(count, 8)
            self.assertEqual(app.state.image_slots.active, 9)
            self.assertEqual(app.state.image_lookup_threads.borrowed_tokens, 8)
            status, _, _ = await call(app, "/v1/me")
            self.assertEqual(status, 200)
        finally:
            release.set()
            tasks[-1].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(app.state.image_slots.active, 0)
        self.assertEqual(app.state.image_lookup_threads.borrowed_tokens, 0)

    async def test_slow_image_send_and_cancellation_release_admission(self):
        app = asgi.create_app(StubService())
        previous = asgi.SEND_TIMEOUT
        asgi.SEND_TIMEOUT = 0.05
        try:
            status, _, _ = await call(app, "/v1/images/image_1", slow_send=True)
            self.assertEqual(status, 200)
            self.assertEqual(app.state.image_slots.active, 0)
        finally:
            asgi.SEND_TIMEOUT = previous
        gate = asyncio.Event()
        sending = asyncio.Event()
        task = asyncio.create_task(call(app, "/v1/images/image_1", body_gate=gate, on_body=sending.set))
        await asyncio.wait_for(sending.wait(), 2)
        self.assertEqual(app.state.image_slots.active, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(app.state.image_slots.active, 0)
        self.assertEqual(app.state.image_lookup_threads.borrowed_tokens, 0)

    async def test_cancel_before_image_lookup_releases_admission(self):
        app = asgi.create_app(StubService())
        receive_gate = asyncio.Event()
        task = asyncio.create_task(call(app, "/v1/images/image_1", receive_gate=receive_gate))
        for _ in range(100):
            if app.state.image_slots.active == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(app.state.image_slots.active, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(app.state.image_slots.active, 0)
        self.assertEqual(app.state.image_lookup_threads.borrowed_tokens, 0)

    async def test_sixty_four_api_slots_keep_acknowledgements_moving_during_capture_burst(self):
        started = 0
        lock = threading.Lock()
        release = threading.Semaphore(0)

        class BlockingCaptures(StubService):
            def handle(self, method, path, token, body=None, query=None):
                nonlocal started
                if method == "POST" and path == "/v1/view/images":
                    with lock:
                        started += 1
                    if not release.acquire(timeout=10):
                        raise RuntimeError("capture gate timed out")
                return super().handle(method, path, token, body, query)

        app = asgi.create_app(BlockingCaptures())

        async def wait_for_captures(count):
            for _ in range(500):
                with lock:
                    current = started
                if current == count:
                    return
                await asyncio.sleep(0.01)
            self.fail(f"only {current} capture workers entered; expected {count}")

        captures = [asyncio.create_task(call(app, "/v1/view/images", method="POST", body=b"{}"))
                    for _ in range(54)]
        try:
            await wait_for_captures(54)
            self.assertEqual(app.state.api_slots.active, 54)
            self.assertEqual(app.state.api_threads.borrowed_tokens, 54)
            for method, path, body in (
                ("POST", "/v1/actions", b'{"request_id":"a"}'),
                ("POST", "/v1/threads/t/messages", b'{"body":"hello"}'),
                ("GET", "/v1/me", b""),
            ):
                status, _, _ = await call(app, path, method=method, body=body)
                self.assertEqual(status, 200)
            image_status, _, _ = await call(app, "/v1/images/image_1")
            self.assertEqual(image_status, 200)
            previous = asgi.STREAM_LIFETIME
            asgi.STREAM_LIFETIME = 0.05
            try:
                stream_status, _, _ = await call(app, "/v1/events",
                                                 headers=((b"accept", b"text/event-stream"),))
                self.assertEqual(stream_status, 200)
            finally:
                asgi.STREAM_LIFETIME = previous
            captures.extend(asyncio.create_task(call(app, "/v1/view/images", method="POST", body=b"{}"))
                            for _ in range(10))
            await wait_for_captures(64)
            self.assertEqual(app.state.api_slots.active, 64)
            self.assertEqual(app.state.api_threads.borrowed_tokens, 64)
            status, body, _ = await call(app, "/v1/actions", method="POST", body=b"{}")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"]["code"], "server_busy")
            release.release()
            for _ in range(200):
                if app.state.api_slots.active == 63:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(app.state.api_slots.active, 63)
            status, _, _ = await call(app, "/v1/actions", method="POST", body=b"{}")
            self.assertEqual(status, 200)
        finally:
            for _ in range(64):
                release.release()
            results = await asyncio.gather(*captures)
        self.assertTrue(all(status == 200 for status, _, _ in results))
        self.assertEqual(app.state.api_slots.active, 0)
        self.assertEqual(app.state.api_threads.borrowed_tokens, 0)

    async def test_sixty_four_streams_wake_on_persisted_event(self):
        store = Store(":memory:")
        token = store.provision("world", "viewer", 0, mode="visual")
        principal = store.authenticate(token)
        service = Service(store, engine=object(), autostart=False, world_id="world")
        original_wait = service.wait_for_events
        waits = 0
        lock = threading.Lock()

        def counted_wait(*args):
            nonlocal waits
            with lock:
                waits += 1
            return original_wait(*args)

        service.wait_for_events = counted_wait
        app = asgi.create_app(service)
        old_lifetime, old_wait = asgi.STREAM_LIFETIME, asgi.EVENT_WAIT
        asgi.STREAM_LIFETIME, asgi.EVENT_WAIT = 2.0, 1.0
        try:
            requests = [asyncio.create_task(call(app, "/v1/events", token=token,
                                             headers=((b"accept", b"text/event-stream"),)))
                        for _ in range(64)]
            for _ in range(500):
                with lock:
                    started = waits
                if app.state.stream_slots.active == 64 and started >= 64:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(app.state.stream_slots.active, 64)
            self.assertGreaterEqual(started, 64)
            store.append_event(principal, "chat.message.tick", {
                "message_id": "m1", "tick": 1, "tick_authoritative": True,
            })
            results = await asyncio.wait_for(asyncio.gather(*requests), 5)
            self.assertEqual(len(results), 64)
            self.assertTrue(all(status == 200 and b"event: chat.message.tick" in body
                                for status, body, _ in results))
            self.assertEqual(app.state.stream_slots.active, 0)
        finally:
            asgi.STREAM_LIFETIME, asgi.EVENT_WAIT = old_lifetime, old_wait
            service.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
