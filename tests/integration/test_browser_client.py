"""Isolated relay checks. Never connects to a running OpenTTD server."""
import asyncio
import importlib.util
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestServer

spec = importlib.util.spec_from_file_location('browser_client', Path(__file__).parents[2] / 'tools/browser_client.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in relay.ASSETS:
            (self.root / name).write_bytes(b'public build')
        self.connections = 0
        self.closed = 0
        self.writers = set()
        self.metadata_server = None

        async def echo(reader, writer):
            self.connections += 1
            self.writers.add(writer)
            try:
                while data := await reader.read(32768):
                    writer.write(data)
                    await writer.drain()
            finally:
                self.closed += 1
                self.writers.discard(writer)
                writer.close()
                await writer.wait_closed()

        self.tcp = await asyncio.start_server(echo, '127.0.0.1', 0)
        native_port = self.tcp.sockets[0].getsockname()[1]
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.server = TestServer(relay.create_app(self.root, self.port, native_port), port=self.port)
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.base = f'http://127.0.0.1:{self.port}'
        self.origin = {'Origin': self.base}

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        if self.metadata_server is not None:
            await self.metadata_server.close()
        for writer in tuple(self.writers):
            writer.close()
        self.tcp.close()
        await self.tcp.wait_closed()
        self.temp.cleanup()

    async def connect(self, **kwargs):
        return await self.session.ws_connect(self.base + '/native', protocols=('binary',), headers=self.origin, **kwargs)

    async def enable_metadata(self, handler, metadata_timeout=relay.METADATA_TIMEOUT):
        upstream = aiohttp.web.Application()
        upstream.router.add_get('/v1/participants', handler)
        self.metadata_server = TestServer(upstream)
        await self.metadata_server.start_server()
        self.token_file = self.root / 'metadata.token'
        self.token_file.write_text('test-secret-api-token\n')
        await self.server.close()
        with patch.object(relay, 'METADATA_TIMEOUT', metadata_timeout):
            app = relay.create_app(self.root, self.port, 1, self.metadata_server.port, self.token_file)
            self.server = TestServer(app, port=self.port)
            await self.server.start_server()

    async def wait_closed_count(self, count):
        async with asyncio.timeout(2):
            while self.closed < count:
                await asyncio.sleep(.01)

    async def test_static_bootstrap_and_rejections(self):
        async with self.session.get(self.base + '/browser-config.js') as response:
            self.assertEqual(response.status, 200)
            self.assertIn('"company": 1', await response.text())
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        for path in ['/secret', '/openttd.js?target=elsewhere', '/%2e%2e/secret', '/openttd.js/extra']:
            async with self.session.get(self.base + path) as response:
                self.assertEqual(response.status, 404)
        async with self.session.get(self.base + '/openttd.js', headers={'Host': 'evil.invalid'}) as response:
            self.assertEqual(response.status, 403)
        async with self.session.head(self.base + '/openttd.wasm') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'')
        async with self.session.post(self.base + '/openttd.js') as response:
            self.assertEqual(response.status, 405)
        (self.root / 'openttd.js').unlink()
        (self.root / 'openttd.js').symlink_to('/etc/passwd')
        async with self.session.get(self.base + '/openttd.js') as response:
            self.assertEqual(response.status, 404)
        self.assertEqual(self.connections, 0)

    async def test_metadata_is_opt_in_and_credential_options_are_paired(self):
        async with self.session.get(self.base + '/model-roster') as response:
            self.assertEqual(response.status, 404)
        for options in [{'metadata_port': 18086}, {'metadata_token_file': self.root / 'token'}]:
            with self.assertRaises(ValueError):
                relay.create_app(self.root, **options)
        token_file = self.root / 'token'
        for value in [b'', b'x\nInjected: bad', b'x\n\n', b'x' * 4097, b'non ascii\xff']:
            token_file.write_bytes(value)
            with self.assertRaises(ValueError):
                relay.create_app(self.root, metadata_port=18086, metadata_token_file=token_file)

    async def test_metadata_projection_keeps_multiple_actors_but_no_private_fields(self):
        calls = []
        report = {'model_name': '<img src=x onerror=alert(1)>', 'effort_level': 'custom high', 'self_reported': True}
        people = [
            {'company_id': 1, 'display_name': '<b>Agent A</b>', 'kind': 'agent',
             'model_report': {**report, 'token': 'nested-secret'}, 'identity_id': 'private-id',
             'run_id': 'private-run', 'native_handle': {'private': True}},
            {'company_id': 1, 'display_name': 'Agent B', 'kind': 'agent'},
            {'company_id': 0, 'display_name': 'Human', 'kind': 'human', 'model_report': report},
            {'company_id': None, 'display_name': 'Spectator', 'kind': 'human'},
        ]
        async def participants(request):
            calls.append((request.path_qs, request.headers.get('Authorization')))
            return aiohttp.web.json_response({'participants': people, 'token': 'top-secret'})
        await self.enable_metadata(participants)
        async with self.session.get(self.base + '/browser-config.js') as response:
            config = await response.text()
            self.assertIn('"modelRoster": true', config)
            self.assertNotIn('secret', config)
            self.assertNotIn(str(self.metadata_server.port), config)
        async with self.session.get(self.base + '/model-roster') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            payload = await response.json()
        self.assertEqual(payload, {'participants': [
            {'company_id': 1, 'display_name': '<b>Agent A</b>', 'kind': 'agent', 'model_report': report},
            {'company_id': 1, 'display_name': 'Agent B', 'kind': 'agent'},
            {'company_id': 0, 'display_name': 'Human', 'kind': 'human'},
            {'company_id': None, 'display_name': 'Spectator', 'kind': 'human'},
        ]})
        self.assertEqual(calls, [('/v1/participants', 'Bearer test-secret-api-token')])
        for path in ['/model-roster?port=1', '/model-roster/other', '/model-roster%2Fother']:
            async with self.session.get(self.base + path) as response:
                self.assertEqual(response.status, 404)
        async with self.session.put(self.base + '/model-roster') as response:
            self.assertEqual(response.status, 405)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.connections, 0)

    async def test_metadata_redirect_oversize_and_malformed_responses_are_unavailable(self):
        result = {'response': aiohttp.web.Response(status=302, headers={'Location': 'http://127.0.0.1:1/secret'})}
        calls = []
        async def participants(request):
            calls.append(request.path_qs)
            return result['response']
        await self.enable_metadata(participants)
        responses = [result['response'], aiohttp.web.Response(text='x' * (relay.METADATA_LIMIT + 1)),
                     aiohttp.web.Response(text='{not json'), aiohttp.web.json_response({'participants': [None]}),
                     aiohttp.web.json_response({'participants': [{'company_id': 1, 'display_name': 'A', 'kind': []}]}),
                     aiohttp.web.json_response({'participants': [{'company_id': 1, 'display_name': 'A', 'kind': 'agent',
                                                                 'model_report': {'model_name': 'bad\nlabel', 'effort_level': 'high', 'self_reported': True}}]})]
        for upstream_response in responses:
            result['response'] = upstream_response
            async with self.session.get(self.base + '/model-roster') as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(await response.text(), 'Model reports unavailable')
        self.assertEqual(len(calls), len(responses))

    async def test_metadata_timeout_is_bounded(self):
        async def participants(request):
            await asyncio.sleep(.2)
            return aiohttp.web.json_response({'participants': []})
        await self.enable_metadata(participants, metadata_timeout=.02)
        async with asyncio.timeout(.15):
            async with self.session.get(self.base + '/model-roster') as response:
                self.assertEqual(response.status, 503)

    async def test_handshake_rejected_before_tcp(self):
        for headers, protocols, path in [({'Origin': 'http://evil.invalid'}, ('binary',), '/native'), (self.origin, (), '/native'), (self.origin, ('binary',), '/native?port=80'), ({}, ('binary',), '/native')]:
            with self.assertRaises(aiohttp.WSServerHandshakeError):
                await self.session.ws_connect(self.base + path, protocols=protocols, headers=headers)
        self.assertEqual(self.connections, 0)

    async def test_binary_and_both_local_origins(self):
        for host in ['127.0.0.1', 'localhost']:
            origin = f'http://{host}:{self.port}'
            async with self.session.ws_connect(self.base + '/native', protocols=('binary',), headers={'Host': f'{host}:{self.port}', 'Origin': origin}, compress=15) as ws:
                self.assertEqual(ws.protocol, 'binary')
                self.assertEqual(ws.compress, 0)
                payload = bytes(range(256)) * 600
                await ws.send_bytes(payload)
                received = b''
                while len(received) < len(payload):
                    message = await ws.receive(timeout=2)
                    self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
                    received += message.data
                self.assertEqual(received, payload)
        await self.wait_closed_count(2)

    async def test_cap_and_slot_release(self):
        sockets = [await self.connect() for _ in range(relay.CLIENT_LIMIT)]
        try:
            with self.assertRaises(aiohttp.WSServerHandshakeError) as error:
                await self.connect()
            self.assertEqual(error.exception.status, 503)
            self.assertEqual(self.connections, relay.CLIENT_LIMIT)
            await sockets.pop().close()
            await self.wait_closed_count(1)
            sockets.append(await self.connect())
        finally:
            for ws in sockets:
                await ws.close()
        await self.wait_closed_count(relay.CLIENT_LIMIT + 1)

    async def test_text_and_oversize_close(self):
        async with await self.connect() as ws:
            await ws.send_str('not native data')
            await ws.receive(timeout=2)
            self.assertEqual(ws.close_code, 1003)
        async with await self.connect() as ws:
            await ws.send_bytes(b'x' * (relay.MESSAGE_LIMIT + 1))
            await ws.receive(timeout=2)
            self.assertEqual(ws.close_code, 1009)
        await self.wait_closed_count(2)

    async def test_tcp_eof_closes_browser(self):
        async with await self.connect() as ws:
            for writer in tuple(self.writers):
                writer.close()
            message = await ws.receive(timeout=2)
            self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)

    async def test_backpressure_times_out_and_cancels_peer(self):
        class BlockedWriter:
            def __init__(self):
                self.writes = 0
            def write(self, data):
                self.writes += 1
            async def drain(self):
                await asyncio.Future()
        class Browser:
            def __aiter__(self):
                return self.messages()
            async def messages(self):
                for _ in range(100):
                    yield type('Message', (), {'type': aiohttp.WSMsgType.BINARY, 'data': b'x'})()
            async def send_bytes(self, data):
                await asyncio.Future()
        writer = BlockedWriter()
        reader = asyncio.StreamReader()
        with patch.object(relay, 'WRITE_TIMEOUT', .02):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(relay.bridge(Browser(), reader, writer), 1)
        self.assertEqual(writer.writes, 1)


if __name__ == '__main__':
    unittest.main()
