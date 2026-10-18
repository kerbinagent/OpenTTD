"""Opt-in real save-publication/backend-observation crash-gap regression.

Use OPENTTD_TEST_BINARY, OPENTTD_TEST_BASESET and optional
OPENTTD_TEST_LIBRARY_PATH/OPENTTD_TEST_KEEP as in test_engine_live.py.
OPENTTD_TEST_GAP_PORT defaults to the isolated native port 13976.
"""

import hashlib
import json
import marshal
import os
from pathlib import Path
import sys
import time
import unittest
import uuid

from engine_harness import LiveWorld, ROOT
from fixture_plans import MODES

sys.path.insert(0, str(ROOT))
from backend.bridge import UnixEngine
from backend.errors import ServiceError
from backend.service import Service
from backend.store import Store


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def fingerprints(binary):
    paths = [Path(binary), ROOT / 'backend/schemas/engine-catalog-v1.json',
             ROOT / 'regression/regression/test.sav', *sorted((ROOT / 'backend').glob('*.py'))]
    return {str(path): digest(path) for path in paths}


class ObservedUnixEngine(UnixEngine):
    """Record receipt facts while every call uses the real Unix transport."""

    def __init__(self, path):
        super().__init__(path)
        self.trace = []

    def call(self, message):
        reply = super().call(message)
        result = reply.get('result') or {}
        self.trace.append({'type': message['type'], 'request_id': message.get('request_id'),
                           'request_sequence': message.get('request_sequence'), 'status': reply.get('status'),
                           'receipt_request_id': result.get('request_id'),
                           'receipt_sequence': result.get('request_sequence'),
                           'checkpoint_state': result.get('checkpoint_state')})
        return reply


@unittest.skipUnless(os.environ.get('OPENTTD_TEST_BINARY') and os.environ.get('OPENTTD_TEST_BASESET'),
                     'Set OPENTTD_TEST_BINARY and OPENTTD_TEST_BASESET for native recovery acceptance')
class CheckpointPublicationGapTests(unittest.TestCase):
    def setUp(self):
        self.world = LiveWorld(os.environ['OPENTTD_TEST_BINARY'], os.environ['OPENTTD_TEST_BASESET'],
                               port=int(os.environ.get('OPENTTD_TEST_GAP_PORT', '13976')))
        self.store = self.service = None
        self.evidence = {'stage': 'startup', 'result': 'incomplete', 'rpc_traces': [],
                         'runtime_files_before': fingerprints(self.world.binary),
                         'loaded_service_code': {name: hashlib.sha256(marshal.dumps(method.__code__)).hexdigest()
                                                 for name, method in Service.__dict__.items()
                                                 if hasattr(method, '__code__')}}
        self.addCleanup(self.close)
        self.world.start()  # The production Service must finish native recovery.
        self.database = self.world.directory / 'backend.sqlite3'
        self.open_backend()
        self.pump()

    def open_backend(self):
        self.store = Store(self.database, world_id=self.world.world_id)
        if not hasattr(self, 'token'):
            self.token = self.store.provision(self.world.world_id, 'publication-gap-test', 0,
                                             mode='text', world_epoch=self.world.epoch)
        self.principal = self.store.authenticate(self.token)
        self.engine = ObservedUnixEngine(self.world.socket_path)
        self.service = Service(self.store, self.engine, autostart=False,
                               world_id=self.world.world_id, checkpoint_interval=3600)

    def close_backend(self):
        if self.service is not None:
            self.service.close()
            self.evidence['rpc_traces'].append(self.engine.trace)
            self.service = None
        if self.store is not None:
            self.store.close()
            self.store = None

    def close(self):
        try:
            self.close_backend()
            after = fingerprints(self.world.binary)
            self.evidence['runtime_files_after'] = after
            self.evidence['runtime_files_changed'] = [path for path, value in after.items()
                                                       if self.evidence['runtime_files_before'].get(path) != value]
            self.evidence['loaded_service_code_after'] = {
                name: hashlib.sha256(marshal.dumps(method.__code__)).hexdigest()
                for name, method in Service.__dict__.items() if hasattr(method, '__code__')}
            path = self.world.directory / 'publication-gap-result.json'
            path.write_text(json.dumps(self.evidence, indent=2) + '\n')
            print(f'Publication-gap evidence: {path}', flush=True)
        finally:
            self.world.close()

    def pump(self):
        self.service._process_world(self.world.world_id, self.store.pending_requests())

    def api(self, method, path, body=None):
        status, result = self.service.handle(method, path, self.token, body)
        self.assertIn(status, (200, 201, 202), result)
        return result

    def observe(self, name):
        return self.api('POST', '/v1/observations', {'query': name, 'parameters': {}})['observation']

    def action(self, name, parameters):
        body = {'request_id': uuid.uuid4().hex, 'action': {'name': name, 'parameters': parameters}}
        self.api('POST', '/v1/actions', body)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.pump()
            reply = self.api('GET', '/v1/actions/' + body['request_id'])
            if reply['status'] == 'completed':
                self.assertTrue(reply['result'].get('success'), reply)
                return body, reply
            self.assertIn(reply['status'], ('accepted', 'queued'), reply)
            time.sleep(0.02)
        self.fail(f'Public action never completed: {name}')

    def test_native_successor_published_while_backend_closed_is_adopted_once(self):
        self.evidence['stage'] = 'setup normal gameplay and observed generation G'
        plan = MODES[0]
        self.action(plan['build'], dict(plan['parameters']))
        self.assertEqual(self.observe('vehicles')['items'], [])
        generation_g = self.world.checkpoint_now()
        self.pump()
        self.assertEqual(self.store.latest_checkpoint(self.world.world_id)['save_id'], generation_g['checkpoint_id'])
        self.evidence['generation_g'] = generation_g

        self.evidence['stage'] = 'completed bus after G, before backend closes'
        body, original = self.action('vehicle.build', {'tile': plan['depot'], 'engine_id': 116})
        self.assertGreater(original['result']['cost'], 0)
        record = self.store.get_request(self.principal, body['request_id'])
        sequence = record['request_sequence']
        self.assertEqual(record['checkpoint_state'], 'uncheckpointed')
        native = self.world.completed(self.world.rpc('checkpoint.status'))
        self.assertEqual(native['checkpoint_id'], generation_g['checkpoint_id'])
        self.assertEqual(self.store.latest_checkpoint(self.world.world_id)['save_id'], generation_g['checkpoint_id'])
        self.assertGreater(sequence, native['retired_through'])
        self.evidence.update(original_public_receipt=original, bus_request_sequence=sequence)
        self.close_backend()

        self.evidence['stage'] = 'native G+1 publication with backend and SQLite closed'
        successor = self.world.checkpoint_now()
        self.evidence['successor'] = successor
        self.assertNotEqual(successor['checkpoint_id'], generation_g['checkpoint_id'])
        previous_instance = self.world.instance
        self.world.stop()
        self.world.start(successor['save_path'])  # No helper recovery.finish bypass.
        self.assertNotEqual(self.world.instance, previous_instance)
        self.open_backend()
        self.assertEqual(self.store.latest_checkpoint(self.world.world_id)['save_id'], generation_g['checkpoint_id'])
        loaded = self.world.completed(self.world.rpc('checkpoint.status'))
        self.evidence['loaded_before_recovery'] = loaded
        self.assertEqual(loaded['checkpoint_id'], successor['checkpoint_id'])
        self.assertTrue(loaded['load_verified'])
        self.assertTrue(loaded['recovery_required'])

        self.evidence['stage'] = 'production recovery adopts the directly proved successor'
        try:
            self.pump()
        except ServiceError as error:
            self.evidence['recovery_error_code'] = error.code
            self.fail(f'Native verified successor {successor["checkpoint_id"]} with database at '
                      f'{generation_g["checkpoint_id"]} refused recovery: {error.code}')
        self.assertEqual(loaded['parent_checkpoint'], {'checkpoint_id': generation_g['checkpoint_id'],
                         'save_checksum': generation_g['save_checksum'], 'saved_tick': generation_g['saved_tick']})
        adopted = self.store.latest_checkpoint(self.world.world_id)
        self.assertEqual(adopted['save_id'], successor['checkpoint_id'])
        self.assertEqual(adopted['retired_through'], successor['checkpoint_retired_through'])
        receipt = self.store.get_request(self.principal, body['request_id'])
        self.assertEqual((receipt['request_sequence'], receipt['status'], receipt['checkpoint_state']),
                         (sequence, 'completed', 'checkpointed'))
        self.assertTrue(any(call['type'] == 'recovery.reconcile' and call['request_id'] == body['request_id']
                            and call['receipt_request_id'] == body['request_id'] and call['receipt_sequence'] == sequence
                            and call['status'] == 'completed' and call['checkpoint_state'] == 'checkpointed'
                            for call in self.engine.trace), self.engine.trace)
        self.assertFalse(self.world.completed(self.world.rpc('checkpoint.status'))['recovery_required'])

        self.evidence['stage'] = 'exact public retry, fresh handle action, subsequent checkpoint'
        vehicles = self.observe('vehicles')['items']
        self.assertEqual(len(vehicles), 1)
        self.assertEqual(vehicles[0]['vehicle_id']['id'], original['result']['vehicle_id']['id'])
        balance = self.observe('company')['balance']
        retry = self.api('POST', '/v1/actions', body)
        self.assertEqual((retry['request_id'], retry['status'], retry['result']),
                         (original['request_id'], 'completed', original['result']))
        self.assertEqual(retry['checkpoint_state'], 'checkpointed')
        self.assertEqual(self.observe('company')['balance'], balance)
        self.assertEqual(len(self.observe('vehicles')['items']), 1)
        self.assertEqual(vehicles[0]['vehicle_id']['engine_instance_id'], self.world.instance)
        self.action('vehicle.rename', {'vehicle_id': vehicles[0]['vehicle_id'], 'name': 'Recovered bus'})
        following = self.world.checkpoint_now()
        self.pump()
        self.assertEqual(self.store.latest_checkpoint(self.world.world_id)['save_id'], following['checkpoint_id'])
        self.assertNotEqual(following['checkpoint_id'], successor['checkpoint_id'])
        self.evidence.update(result='passed', stage='complete', adopted=adopted,
                             retry=retry, following_checkpoint=following)
