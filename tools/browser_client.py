#!/usr/bin/env python3
"""Serve a public WASM build and a fixed loopback native binary relay."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import stat

from aiohttp import WSMsgType, web

ASSETS = {'openttd.html', 'openttd.js', 'openttd.wasm', 'openttd.data'}
CLIENT_LIMIT = 4
MESSAGE_LIMIT = 256 * 1024
CHUNK_SIZE = 32 * 1024
WRITE_TIMEOUT = 10


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


def create_app(asset_dir, port=18087, native_port=3979):
    root = Path(asset_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('asset directory required')
    # Validate only this finite build output; never expose directory contents.
    for name in ASSETS:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'missing or symlinked public build asset: {name}')
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    origins = {'http://' + host for host in hosts}
    active = set()

    @web.middleware
    async def guard(request, handler):
        if request.headers.getall('Host', []) not in ([host] for host in hosts):
            raise web.HTTPForbidden()
        if request.query_string or '%' in request.raw_path or request.path not in {'/', '/browser-config.js', '/native'} | {'/' + name for name in ASSETS}:
            raise web.HTTPNotFound()
        if request.method not in {'GET', 'HEAD'}:
            raise web.HTTPMethodNotAllowed(request.method, ['GET', 'HEAD'])
        response = await handler(request)
        return response

    async def serve(request):
        if request.path == '/':
            raise web.HTTPFound('/openttd.html')
        if request.path == '/browser-config.js':
            config = {'nativePort': native_port, 'company': 1, 'websocketPath': '/native'}
            return web.Response(text='window.OPENTTD_BROWSER_RELAY=' + json.dumps(config) + ';\n', content_type='application/javascript')
        name = request.path[1:]
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

    async def headers(request, response):
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})

    async def shutdown(app):
        await asyncio.gather(*(ws.close(code=1001, message=b'relay stopping') for ws in tuple(active)))

    app = web.Application(middlewares=[guard], client_max_size=1024)
    app.on_response_prepare.append(headers)
    app.on_shutdown.append(shutdown)
    app.router.add_route('*', '/native', native)
    app.router.add_route('*', '/{path:.*}', serve)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('asset_dir', type=Path, help='public isolated WASM build directory')
    parser.add_argument('--port', type=port_number, default=18087)
    parser.add_argument('--native-port', type=port_number, default=3979)
    args = parser.parse_args()
    web.run_app(create_app(args.asset_dir, args.port, args.native_port), host='127.0.0.1', port=args.port, access_log=None, shutdown_timeout=5, max_line_size=4096, max_field_size=4096)


if __name__ == '__main__':
    main()
