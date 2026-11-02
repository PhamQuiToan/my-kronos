import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from webui import api_token
from flask import Flask
from webui.forecast_api import create_forecast_blueprint


class TokenStorageTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / '.api_token'
        path_patch = patch.object(api_token, 'TOKEN_FILE', self.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        env_patch = patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop('KRONOS_API_TOKEN', None)

    def test_initialize_permissions_and_rotation(self):
        self.assertTrue(api_token.initialize_token())
        first = api_token.read_token()
        self.assertGreaterEqual(len(first), 48)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertFalse(api_token.initialize_token())
        self.assertEqual(api_token.read_token(), first)
        self.assertTrue(api_token.initialize_token(rotate=True))
        self.assertNotEqual(api_token.read_token(), first)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_environment_override_and_fail_closed(self):
        self.assertEqual(api_token.read_token(), '')
        self.path.write_text('local-secret\n')
        self.assertEqual(api_token.read_token(), 'local-secret')
        os.environ['KRONOS_API_TOKEN'] = 'environment-secret'
        self.assertEqual(api_token.read_token(), 'environment-secret')
        os.environ['KRONOS_API_TOKEN'] = ''
        self.assertEqual(api_token.read_token(), '')
        del os.environ['KRONOS_API_TOKEN']
        self.path.write_text('invalid token')
        self.assertEqual(api_token.read_token(), '')

    def test_endpoint_reads_local_token_and_rotation(self):
        api_token.initialize_token()
        token = api_token.read_token()
        app = Flask(__name__)
        app.register_blueprint(create_forecast_blueprint())
        client = app.test_client()
        headers = {'Authorization': f'Bearer {token}'}
        self.assertEqual(client.post('/api/forecast', json={}, headers=headers).status_code, 400)
        api_token.initialize_token(rotate=True)
        self.assertEqual(client.post('/api/forecast', json={}, headers=headers).status_code, 401)
        headers['Authorization'] = f'Bearer {api_token.read_token()}'
        self.assertEqual(client.post('/api/forecast', json={}, headers=headers).status_code, 400)
