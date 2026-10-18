#!/usr/bin/env python3
"""Serve a public WASM build and a fixed loopback native binary relay."""
import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import re
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector, WSMsgType, web
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.agent_assets import build_agent_assets

ASSETS = {'openttd.html', 'openttd.js', 'openttd.wasm', 'openttd.data'}
CLIENT_LIMIT = 4
MESSAGE_LIMIT = 256 * 1024
CHUNK_SIZE = 32 * 1024
WRITE_TIMEOUT = 10
METADATA_LIMIT = 64 * 1024
METADATA_TIMEOUT = 3
METADATA_SESSION = web.AppKey('metadata_session', ClientSession)
API_SESSION = web.AppKey('api_session', ClientSession)
API_LIMIT = 16
API_BODY_LIMIT = 64 * 1024
API_RESPONSE_LIMIT = 16 * 1024 * 1024
API_REQUEST_HEADERS = ('Authorization', 'Content-Type', 'Accept', 'Last-Event-ID', 'X-OpenTTD-Attempt-Id')
API_RESPONSE_HEADERS = ('Content-Type',) + tuple('X-OpenTTD-' + name for name in (
    'Observation-Id', 'Image-Id', 'Observation-Generation', 'Capture-Kind', 'Image-Width',
    'Image-Height', 'Causal-State', 'Image-Reused', 'Image-Age-Ms', 'Image-Purpose'))
ENTRY_ROOT = Path(__file__).resolve().parents[1] / 'os' / 'emscripten'
ENTRY_ASSETS = {'browser-entry.js': 'application/javascript', 'browser-entry.css': 'text/css'}


def bind_address(value):
    address = ipaddress.ip_address(value)
    if str(address) != value or address.version != 4 or not (value == '127.0.0.1' or address in ipaddress.ip_network('100.64.0.0/10')):
        raise ValueError('bind an exact loopback or Tailscale IPv4 address')
    return value


def https_origin(value, allow_http_host=None):
    parsed = urlsplit(value)
    if ((parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname == allow_http_host))
            or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or any(c.isspace() for c in value) or parsed.hostname != parsed.hostname.lower()
            or len(parsed.hostname) > 253
            or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in parsed.hostname.split('.'))
            or (parsed.port is not None and (not 1 <= parsed.port <= 65535 or parsed.port == (443 if parsed.scheme == 'https' else 80)))
            or parsed.netloc != parsed.hostname + (f':{parsed.port}' if parsed.port is not None else '')):
        raise ValueError('public origin must be exact HTTPS or HTTP on the configured bind address')
    return value


def port_number(value):
    value = int(value)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError('port must be between 1 and 65535')
    return value


async def bounded_write(writer, data):
    writer.write(data)
    await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)


async def bridge(ws, reader, writer):
    async def from_browser():
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await bounded_write(writer, message.data)
            elif message.type == WSMsgType.TEXT:
                await ws.close(code=1003, message=b'binary required')
                return
            elif message.type == WSMsgType.ERROR:
                return

    async def to_browser():
        while data := await reader.read(CHUNK_SIZE):
            await asyncio.wait_for(ws.send_bytes(data), WRITE_TIMEOUT)

    tasks = [asyncio.create_task(from_browser()), asyncio.create_task(to_browser())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def read_metadata_token(path):
    with Path(path).open('rb') as source:
        value = source.read(4097)
    token = value[:-1] if value.endswith(b'\n') else value
    token = token[:-1] if token.endswith(b'\r') else token
    if len(value) > 4096 or not token or any(byte <= 32 or byte >= 127 for byte in token):
        raise ValueError('metadata credential must be a bounded single-line token')
    return token.decode('ascii')


def model_roster(payload):
    """Public display labels only: no identities, run handles or credentials."""
    people = payload.get('participants') if isinstance(payload, dict) else None
    if not isinstance(people, list) or len(people) > 100:
        raise ValueError('invalid participant list')
    result = []
    for person in people:
        if not isinstance(person, dict):
            raise ValueError('invalid participant')
        company = person.get('company_id')
        name, kind = person.get('display_name'), person.get('kind')
        valid_company = company is None or (type(company) is int and company in (*range(15), 255))
        if not valid_company or not isinstance(name, str) or not 1 <= len(name) <= 200 or not isinstance(kind, str) or kind not in {'agent', 'human'}:
            raise ValueError('invalid participant labels')
        row = {'company_id': company, 'display_name': name, 'kind': kind}
        report = person.get('model_report')
        if kind == 'agent' and report is not None:
            if not isinstance(report, dict) or report.get('self_reported') is not True:
                raise ValueError('invalid model report')
            clean = {}
            for field, maximum in [('model_name', 100), ('effort_level', 32)]:
                label = report.get(field)
                if not isinstance(label, str) or not 1 <= len(label) <= maximum or label != label.strip() or not label.isprintable():
                    raise ValueError('invalid model report label')
                clean[field] = label
            row['model_report'] = {**clean, 'self_reported': True}
        result.append(row)
    return {'participants': result}


def create_app(asset_dir, port=18087, native_port=3979, metadata_port=None, metadata_token_file=None,
               *, world_id=None, api_port=None, public_origin=None, public_api_origin=None, bind_host='127.0.0.1'):
    if (metadata_port is None) != (metadata_token_file is None):
        raise ValueError('metadata port and token file must be provided together')
    metadata_token = read_metadata_token(metadata_token_file) if metadata_port is not None else None
    if world_id is not None and (not isinstance(world_id, str) or not 1 <= len(world_id) <= 128
                                 or not all(c.isalnum() or c in '-_.' for c in world_id)):
        raise ValueError('world identity must be a bounded identifier')
    entry_assets = {name: (ENTRY_ROOT / name).read_bytes() for name in ENTRY_ASSETS}
    bind_address(bind_host)
    if public_origin is None and not ipaddress.ip_address(bind_host).is_loopback:
        public_origin = f'http://{bind_host}:{port}'
    if public_origin is not None:
        https_origin(public_origin, bind_host)
    if public_api_origin is not None:
        https_origin(public_api_origin, bind_host)
    agent_assets = build_agent_assets() if api_port is not None else {}
    root = Path(asset_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('asset directory required')
    # Validate only this finite build output; never expose directory contents.
    for name in ASSETS:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'missing or symlinked public build asset: {name}')
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    hosts.add(f'{bind_host}:{port}')
    origins = {'http://' + host for host in hosts}
    if public_origin is not None:
        hosts.add(urlsplit(public_origin).netloc)
        origins.add(public_origin)
    active = set()
    api_active = set()

    @web.middleware
    async def guard(request, handler):
        if request.headers.getall('Host', []) not in ([host] for host in hosts):
            raise web.HTTPForbidden()
        if api_port is not None and request.raw_path.startswith('/v1/'):
            if (len(request.raw_path) > 8192 or '#' in request.raw_path or '%' in request.raw_path.split('?', 1)[0]
                    or any(ord(c) < 32 or ord(c) == 127 for c in request.raw_path)):
                raise web.HTTPBadRequest()
            if request.headers.getall('Origin', []) and request.headers.getall('Origin', []) not in ([origin] for origin in origins):
                raise web.HTTPForbidden()
            return await handler(request)
        routes = {'/', '/browser-config.js', '/native'} | {'/' + name for name in ASSETS | ENTRY_ASSETS.keys()}
        routes.update(agent_assets)
        if metadata_port is not None:
            routes.add('/model-roster')
        if request.query_string or '%' in request.raw_path or request.path not in routes:
            raise web.HTTPNotFound()
        if request.method not in {'GET', 'HEAD'}:
            raise web.HTTPMethodNotAllowed(request.method, ['GET', 'HEAD'])
        response = await handler(request)
        return response

    async def serve(request):
        if request.path == '/':
            raise web.HTTPFound('/openttd.html')
        if request.path == '/browser-config.js':
            host = request.host.split(':')[0]
            local_api = 'http://' + request.host if api_port is not None and request.host in {f'127.0.0.1:{port}', f'localhost:{port}', f'{bind_host}:{port}'} else None
            config = {'nativePort': native_port, 'websocketPath': '/native', 'modelRoster': metadata_port is not None,
                      'worldId': world_id, 'publicOrigin': public_origin,
                      'apiOrigin': public_api_origin or local_api}
            return web.Response(text='window.OPENTTD_BROWSER_RELAY=' + json.dumps(config) + ';\n', content_type='application/javascript')
        name = request.path[1:]
        if request.path in agent_assets:
            body, content_type = agent_assets[request.path]
            return web.Response(body=body, headers={'Content-Type': content_type})
        if name in entry_assets:
            return web.Response(body=entry_assets[name], content_type=ENTRY_ASSETS[name])
        # Open without following a replacement symlink; retain the descriptor
        # while streaming so directory changes cannot change the served file.
        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
        except OSError:
            raise web.HTTPNotFound()
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise web.HTTPNotFound()
            types = {'.html': 'text/html', '.js': 'application/javascript', '.wasm': 'application/wasm', '.data': 'application/octet-stream'}
            response = web.StreamResponse(headers={'Content-Type': types[Path(name).suffix], 'Content-Length': str(info.st_size)})
            await response.prepare(request)
            if request.method != 'HEAD':
                with os.fdopen(fd, 'rb', closefd=False) as source:
                    while data := source.read(64 * 1024):
                        await asyncio.wait_for(response.write(data), WRITE_TIMEOUT)
            await response.write_eof()
            return response
        finally:
            os.close(fd)

    async def native(request):
        if request.method != 'GET' or request.headers.getall('Origin', []) not in ([origin] for origin in origins):
            raise web.HTTPForbidden()
        protocols = [p.strip() for p in request.headers.get('Sec-WebSocket-Protocol', '').split(',')]
        ws = web.WebSocketResponse(protocols=('binary',), compress=False, max_msg_size=MESSAGE_LIMIT, heartbeat=30, writer_limit=CHUNK_SIZE)
        if 'binary' not in protocols or not ws.can_prepare(request).ok:
            raise web.HTTPBadRequest(text='binary WebSocket required')
        if len(active) >= CLIENT_LIMIT:
            raise web.HTTPServiceUnavailable()
        # No await between capacity check and reservation.
        active.add(ws)
        writer = None
        try:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', native_port, limit=CHUNK_SIZE), 3)
            except (OSError, TimeoutError):
                raise web.HTTPBadGateway()
            writer.transport.set_write_buffer_limits(high=CHUNK_SIZE, low=CHUNK_SIZE // 2)
            await ws.prepare(request)
            await bridge(ws, reader, writer)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 3)
            try:
                await ws.close()
            finally:
                active.discard(ws)
        return ws

    async def metadata(request):
        try:
            async with app[METADATA_SESSION].get(
                f'http://127.0.0.1:{metadata_port}/v1/participants',
                headers={'Authorization': 'Bearer ' + metadata_token}, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ValueError('metadata unavailable')
                body = bytearray()
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    body.extend(chunk)
                    if len(body) > METADATA_LIMIT:
                        raise ValueError('metadata too large')
                payload = model_roster(json.loads(body))
        except (ClientError, TimeoutError, ValueError, UnicodeError):
            raise web.HTTPServiceUnavailable(text='Model reports unavailable')
        return web.json_response(payload)

    async def metadata_session(app):
        async with ClientSession(timeout=ClientTimeout(total=METADATA_TIMEOUT),
                                 connector=TCPConnector(limit=2), trust_env=False) as session:
            app[METADATA_SESSION] = session
            yield

    async def api_session(app):
        async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=3, sock_read=60),
                                 connector=TCPConnector(limit=API_LIMIT), cookie_jar=DummyCookieJar(), trust_env=False) as session:
            app[API_SESSION] = session
            yield

    async def api(request):
        if len(api_active) >= API_LIMIT:
            raise web.HTTPServiceUnavailable()
        ticket = object()
        api_active.add(ticket)
        response = None
        try:
            headers = {}
            for name in API_REQUEST_HEADERS:
                values = request.headers.getall(name, [])
                if len(values) > 1:
                    raise web.HTTPBadRequest()
                if values:
                    headers[name] = values[0]
            body = await asyncio.wait_for(request.read(), 10)
            if len(body) > API_BODY_LIMIT:
                raise web.HTTPRequestEntityTooLarge(max_size=API_BODY_LIMIT, actual_size=len(body))
            target = URL(f'http://127.0.0.1:{api_port}' + request.raw_path, encoded=True)
            async with app[API_SESSION].request(request.method, target, data=body, headers=headers, allow_redirects=False) as upstream:
                public_headers = {}
                for name in API_RESPONSE_HEADERS:
                    values = upstream.headers.getall(name, [])
                    if len(values) > 1:
                        raise web.HTTPBadGateway()
                    if values:
                        public_headers[name] = values[0]
                response = web.StreamResponse(status=upstream.status, headers=public_headers)
                await response.prepare(request)
                size = 0
                events = upstream.content_type == 'text/event-stream'
                async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                    size += len(chunk)
                    if not events and size > API_RESPONSE_LIMIT:
                        raise ValueError('API response exceeds its bound')
                    await asyncio.wait_for(response.write(chunk), WRITE_TIMEOUT)
                await asyncio.wait_for(response.write_eof(), WRITE_TIMEOUT)
                return response
        except (ClientError, OSError, TimeoutError, ValueError):
            if response is not None and response.prepared:
                if request.transport is not None:
                    request.transport.close()
                return response
            raise web.HTTPBadGateway()
        finally:
            api_active.discard(ticket)

    async def headers(request, response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})

    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001, message=b'relay stopping') for ws in tuple(active)))

    app = web.Application(middlewares=[guard], client_max_size=API_BODY_LIMIT + 1)
    app.on_response_prepare.append(headers)
    app.on_shutdown.append(shutdown)
    if metadata_port is not None:
        app.cleanup_ctx.append(metadata_session)
        app.router.add_route('*', '/model-roster', metadata)
    if api_port is not None:
        app.cleanup_ctx.append(api_session)
        app.router.add_route('*', '/v1/{tail:.*}', api)
    app.router.add_route('*', '/native', native)
    app.router.add_route('*', '/{path:.*}', serve)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('asset_dir', type=Path, help='public isolated WASM build directory')
    parser.add_argument('--port', type=port_number, default=18087)
    parser.add_argument('--host', type=bind_address, default='127.0.0.1', help='exact loopback or Tailscale IPv4 bind address')
    parser.add_argument('--native-port', type=port_number, default=3979)
    parser.add_argument('--metadata-port', type=port_number, help='fixed loopback API port for public model labels')
    parser.add_argument('--metadata-token-file', type=Path, help='server-held API credential; requires --metadata-port')
    parser.add_argument('--world-id', help='stable world epoch for remembered company selection; change on a new world')
    parser.add_argument('--api-port', type=port_number, help='fixed loopback API; expose its authenticated routes and current public agent kit')
    parser.add_argument('--public-origin', help='exact browser origin; HTTP is allowed on the configured bind address')
    parser.add_argument('--public-api-origin', help='exact public guide/API origin for agent prompts; defaults to this listener')
    args = parser.parse_args()
    if (args.metadata_port is None) != (args.metadata_token_file is None):
        parser.error('--metadata-port and --metadata-token-file must be provided together')
    web.run_app(create_app(args.asset_dir, args.port, args.native_port, args.metadata_port, args.metadata_token_file,
                           world_id=args.world_id, api_port=args.api_port, public_origin=args.public_origin,
                           public_api_origin=args.public_api_origin, bind_host=args.host), host=args.host, port=args.port,
                access_log=None, shutdown_timeout=5, max_line_size=4096, max_field_size=4096, handler_cancellation=True)


if __name__ == '__main__':
    main()
