"""Web UI data-file access must stay inside the configured market-data directory."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui import app as webui_app


class WebUIFileAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data_dir = root / 'data'
        self.data_dir.mkdir()
        self.outside_file = root / 'outside.csv'
        self.outside_file.write_text(
            'timestamps,open,high,low,close\n'
            '2026-01-01T00:00:00Z,10,12,9,11\n')
        self.patch_data_dir = patch.object(webui_app, 'DATA_DIR', self.data_dir)
        self.patch_data_dir.start()
        self.addCleanup(self.patch_data_dir.stop)
        self.client = webui_app.app.test_client()

    def test_resolve_only_accepts_direct_data_files(self):
        market_file = self.data_dir / 'market.csv'
        market_file.write_text(
            'timestamps,open,high,low,close\n'
            '2026-01-01T00:00:00Z,10,12,9,11\n')
        self.assertEqual(webui_app.resolve_data_file(str(market_file)), market_file.resolve())
        self.assertIsNone(webui_app.resolve_data_file(str(self.outside_file)))
        self.assertIsNone(webui_app.resolve_data_file('../outside.csv'))

    def test_resolve_rejects_symlink_to_outside(self):
        link = self.data_dir / 'external.csv'
        link.symlink_to(self.outside_file)
        self.assertIsNone(webui_app.resolve_data_file(str(link)))

    def test_load_data_endpoint_rejects_outside_file(self):
        response = self.client.post('/api/load-data', json={'file_path': str(self.outside_file)})
        self.assertEqual(response.status_code, 400)
        self.assertIn('data directory', response.get_json()['error'])

    def test_predict_endpoint_rejects_outside_file(self):
        response = self.client.post('/api/predict', json={'file_path': str(self.outside_file)})
        self.assertEqual(response.status_code, 400)
        self.assertIn('data directory', response.get_json()['error'])

    def test_load_data_endpoint_keeps_valid_data_file_behavior(self):
        market_file = self.data_dir / 'market.csv'
        market_file.write_text(
            'timestamps,open,high,low,close\n'
            '2026-01-01T00:00:00Z,10,12,9,11\n')
        response = self.client.post('/api/load-data', json={'file_path': str(market_file)})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])


if __name__ == '__main__':
    unittest.main()
