#!/usr/bin/env python3
"""Export a verified native catalog to an explicitly new, private JSON file.

This read-only operator tool never opens a database or sends gameplay commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import uuid


MAX_FRAME = 65536  # Complete UTF-8 NDJSON frame must be strictly smaller.
MAX_BYTES = 16 * 1024 * 1024
MAX_NAMES = 1024
DEADLINE_SECONDS = 60
HASH_ALGORITHM = 'blake2b-256'
HASH_KEYS = {'catalog_hash_algorithm', 'catalog_hash'}
CATALOG_KEYS = {'version', 'actions', 'queries', 'limits', 'conventions', 'human_only'}
NAME = re.compile(r'[a-z][a-z0-9_.]{0,127}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')


class ExportError(ValueError):
    """The export could not establish one complete, consistent native catalog."""


def _fail(message):
    raise ExportError(message)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail('Duplicate JSON key')
        value[key] = item
    return value


def _not_integer(_):
    _fail('Catalog protocol permits no floating-point or nonfinite numbers')


def _restricted(value, depth=0):
    """Match native JSON scalar domains; floats have no portable catalog form."""
    if depth > 128:
        _fail('Catalog nesting exceeds the limit')
    if value is None or type(value) in (str, bool):
        return
    if type(value) is int and -(1 << 63) <= value < (1 << 64):
        return
    if type(value) is list:
        for item in value:
            _restricted(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _restricted(item, depth + 1)
        return
    _fail('Catalog contains an unsupported JSON value')


def canonical_bytes(value):
    _restricted(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _names(value, label, *, pattern=NAME):
    if (type(value) is not list or len(value) > MAX_NAMES
            or any(type(name) is not str or pattern.fullmatch(name) is None for name in value)
            or len(set(value)) != len(value)):
        _fail(f'{label} must contain bounded, ordered, unique names')
    return value


def validate_capabilities(value):
    if type(value) is not dict or set(value) != CATALOG_KEYS | HASH_KEYS:
        _fail('Unexpected compact catalog metadata')
    _restricted(value)
    if type(value['version']) is not int or value['version'] < 1:
        _fail('Invalid catalog version')
    if value['catalog_hash_algorithm'] != HASH_ALGORITHM or type(value['catalog_hash']) is not str or HASH.fullmatch(value['catalog_hash']) is None:
        _fail('Invalid catalog hash identity')
    _names(value['actions'], 'Actions')
    _names(value['queries'], 'Queries')
    if len(value['actions']) + len(value['queries']) > MAX_NAMES or 'ui.apply' not in value['actions']:
        _fail('Incomplete or oversized world catalog')
    if type(value['limits']) is not dict or any(type(item) is not int or item < 0 for item in value['limits'].values()):
        _fail('Invalid catalog limits')
    if type(value['conventions']) is not dict:
        _fail('Invalid catalog conventions')
    if (type(value['human_only']) is not list or len(value['human_only']) > MAX_NAMES
            or any(type(item) is not str or not 0 < len(item) <= 256 for item in value['human_only'])):
        _fail('Invalid human-only capability list')
    return value


class CatalogReader:
    """One bounded request per connection, with a single export-wide deadline."""

    def __init__(self, socket_path, world, epoch):
        self.socket_path = str(socket_path)
        self.world, self.epoch = world, epoch
        self.instance = None
        self.deadline = time.monotonic() + DEADLINE_SECONDS
        self.bytes_transferred = 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            _fail('Catalog export exceeded its 60-second deadline')
        return min(2.0, remaining)

    def _charge(self, count):
        self.bytes_transferred += count
        if self.bytes_transferred > MAX_BYTES:
            _fail('Catalog export exceeded its 16-MiB transfer limit')

    def call(self, rpc_type, **fields):
        correlation = uuid.uuid4().hex
        request = {'protocol_version': 1, 'type': rpc_type, 'correlation_id': correlation,
                   'world_id': self.world, 'world_epoch': self.epoch, **fields}
        if self.instance is not None:
            request['engine_instance_id'] = self.instance
        data = canonical_bytes(request) + b'\n'
        if len(data) >= MAX_FRAME:
            _fail('Catalog request exceeds the private frame limit')
        self._charge(len(data))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.remaining())
            connection.connect(self.socket_path)
            connection.settimeout(self.remaining())
            connection.sendall(data)
            received = bytearray()
            while b'\n' not in received:
                connection.settimeout(self.remaining())
                block = connection.recv(min(4096, MAX_FRAME - len(received)))
                if not block:
                    _fail('Truncated catalog response')
                self._charge(len(block))
                received.extend(block)
                if len(received) >= MAX_FRAME:
                    _fail('Catalog response exceeds the private frame limit')
        self.remaining()
        line, trailing = received.split(b'\n', 1)
        if trailing:
            _fail('Unexpected extra catalog response data')
        reply = json.loads(line.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_float=_not_integer, parse_constant=_not_integer)
        _restricted(reply)
        if (type(reply) is not dict or type(reply.get('protocol_version')) is not int
                or reply['protocol_version'] != 1 or reply.get('type') != rpc_type + '.result'
                or reply.get('correlation_id') != correlation or reply.get('world_id') != self.world
                or reply.get('world_epoch') != self.epoch or reply.get('status') != 'completed'
                or 'error' in reply or type(reply.get('result')) is not dict):
            _fail('Catalog response failed envelope or world verification')
        instance = reply.get('engine_instance_id')
        if type(instance) is not str or not 1 <= len(instance) <= 128 or (self.instance is not None and instance != self.instance):
            _fail('Catalog engine instance changed or is invalid')
        self.instance = instance
        return reply['result']

    def hello(self):
        result = self.call('hello')
        if result.get('role') != 'world' or result.get('engine_instance_id') != self.instance or result.get('world_epoch') != self.epoch:
            _fail('Catalog discovery must come from the fenced authoritative world')
        return validate_capabilities(result.get('capabilities'))

    def export(self):
        capabilities = self.hello()
        catalog = {key: value for key, value in capabilities.items() if key not in HASH_KEYS}
        for plural, kind in (('actions', 'action'), ('queries', 'query')):
            entries = []
            for name in capabilities[plural]:
                result = self.call('catalog.describe', kind=kind, name=name)
                if (set(result) != {'entry', 'version', *HASH_KEYS}
                        or type(result['version']) is not int or result['version'] != capabilities['version']
                        or any(result[key] != capabilities[key] for key in HASH_KEYS)):
                    _fail('Catalog descriptor identity changed')
                entry = result['entry']
                if (type(entry) is not dict or entry.get('name') != name
                        or type(entry.get('parameters')) is not dict or type(entry.get('target_kind')) is not str
                        or type(entry.get('modes')) is not list or not entry['modes']
                        or any(mode not in ('text', 'visual') for mode in entry['modes'])
                        or len(set(entry['modes'])) != len(entry['modes'])):
                    _fail('Invalid or incorrectly named catalog descriptor')
                _names(entry.get('result_fields'), 'Result fields')
                entries.append(entry)
            catalog[plural] = entries
        encoded = canonical_bytes(catalog)
        if len(encoded) + 1 > MAX_BYTES:
            _fail('Reconstructed catalog exceeds 16 MiB')
        actual_hash = hashlib.blake2b(encoded, digest_size=32).hexdigest()
        if actual_hash != capabilities['catalog_hash']:
            _fail('Reconstructed catalog hash does not match the native catalog')
        if canonical_bytes(self.hello()) != canonical_bytes(capabilities):
            _fail('Catalog discovery changed during export')
        return encoded + b'\n', actual_hash, len(catalog['actions']), len(catalog['queries'])


def publish_new(output, data):
    """Publish a flushed 0600 file atomically without replacing any target."""
    output = Path(output)
    if not output.is_absolute() or not output.name or '..' in output.parts:
        _fail('Output must have an absolute, canonical parent directory')
    # Pin every directory component and refuse symlinks. All creation, linking
    # and cleanup then use the checked directory FD, even if an ancestor moves.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open('/', flags)
    temporary = None
    try:
        for part in output.parent.parts[1:]:
            following = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = following
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            _fail('Output parent must be an owner-only private directory')
        try:
            os.stat(output.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail('Output already exists; choose a new explicit path')
        candidate = '.catalog-' + uuid.uuid4().hex
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        temporary = candidate
        with os.fdopen(descriptor, 'wb') as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.link(temporary, output.name, src_dir_fd=directory, dst_dir_fd=directory,
                follow_symlinks=False)
        os.fsync(directory)
    finally:
        try:
            if temporary is not None:
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True, type=Path)
    parser.add_argument('--world', required=True)
    parser.add_argument('--epoch', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.socket.is_absolute() or not args.world or not args.epoch or len(args.world) > 128 or len(args.epoch) > 128:
            _fail('Use an absolute private socket and bounded world/epoch identifiers')
        if os.path.lexists(args.output):
            _fail('Output already exists; choose a new explicit path')
        reader = CatalogReader(args.socket, args.world, args.epoch)
        data, digest, actions, queries = reader.export()
        reader.remaining()
        publish_new(args.output, data)
        print(f'Exported {actions} actions and {queries} queries to {args.output} ({HASH_ALGORITHM} {digest})')
        return 0
    except (ExportError, OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        print(f'Catalog export failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
