"""Explicit private-tailnet transport does not weaken ordinary HTTP policy."""
import os
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler

from backend.client import BackendClient


class TailnetClientTests(unittest.TestCase):
    def test_tailnet_http_is_explicit_and_literal(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError): BackendClient('http://100.111.4.97:18087', 'test-token')
        with patch.dict(os.environ, {'OPENTTD_ALLOW_TAILSCALE_HTTP': '1'}, clear=True):
            self.assertEqual(BackendClient('http://100.111.4.97:18087', 'test-token').base_url, 'http://100.111.4.97:18087')
            for host in ['example.com', '192.168.1.1', '100.63.255.255', '100.128.0.1', 'game.example.ts.net']:
                with self.assertRaises(ValueError): BackendClient('http://' + host, 'test-token')

    def test_tailnet_transport_disables_environment_proxies(self):
        with patch.dict(os.environ, {'OPENTTD_ALLOW_TAILSCALE_HTTP': '1', 'http_proxy': 'http://127.0.0.1:1'}, clear=True):
            client = BackendClient('http://100.111.4.97:18087', 'test-token')
            self.assertFalse(any(isinstance(h, ProxyHandler) and h.proxies for h in client._opener.handlers))
