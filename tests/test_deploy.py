"""Exercise deployment ordering locally; SSH is replaced, no server is contacted."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/deploy.sh'


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / 'source'
        self.target = root / 'target'
        bins = root / 'bin'
        (self.source / 'scripts').mkdir(parents=True)
        (self.source / 'webui').mkdir()
        self.target.mkdir()
        bins.mkdir()
        shutil.copy2(SCRIPT, self.source / 'scripts/deploy.sh')
        (self.source / 'webui/forecast_api.py').write_text('new source\n')
        (self.source / 'webui/.api_token').write_text('local-test-token')
        (self.target / 'server-only.txt').write_text('keep')
        (bins / 'ssh').write_text('#!/bin/bash\nexec bash -c "${!#}"\n')
        (bins / 'rsync').write_text(
            '#!/usr/bin/env python3\nimport os,sys\n'
            'if os.environ.get("FAIL_SYNC"): sys.exit(23)\n'
            'args=sys.argv[1:]\nargs[-1]=os.environ["REMOTE_DIR"]+"/"\n'
            f'os.execv({shutil.which("rsync")!r}, [{shutil.which("rsync")!r}]+args)\n')
        (bins / 'systemctl').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> systemctl-calls\n')
        for binary in bins.iterdir():
            binary.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bins)+':'+os.environ['PATH'],
                        REMOTE_DIR=str(self.target), SSH_HOST='test.invalid',
                        SSH_KEY='', INSTALL_DEPS='0')
        self.env.pop('POST_DEPLOY_COMMAND', None)
        self.env.pop('SSH_USER', None)
        self.env.pop('root', None)

    def run_deploy(self, *args):
        return subprocess.run(['bash', str(self.source / 'scripts/deploy.sh'), '--non-interactive', *args],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def test_source_then_default_restart(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / 'webui/forecast_api.py').read_text(), 'new source\n')
        self.assertEqual((self.target / 'systemctl-calls').read_text().splitlines(),
                         ['restart kronos-api', 'is-active --quiet kronos-api'])
        self.assertFalse((self.target / 'webui/.api_token').exists())
        self.assertTrue((self.target / 'server-only.txt').exists())

    def test_custom_command_sees_new_code(self):
        self.env['POST_DEPLOY_COMMAND'] = "grep -q 'new source' webui/forecast_api.py && touch updated"
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / 'updated').exists())

    def test_dry_run_does_not_restart_or_copy(self):
        self.assertEqual(self.run_deploy('--dry-run').returncode, 0)
        self.assertFalse((self.target / 'webui/forecast_api.py').exists())
        self.assertFalse((self.target / 'systemctl-calls').exists())

    def test_failed_sync_never_restarts(self):
        self.env['FAIL_SYNC'] = '1'
        self.assertEqual(self.run_deploy().returncode, 23)
        self.assertFalse((self.target / 'systemctl-calls').exists())

    def test_failed_command_fails_deploy(self):
        self.env['POST_DEPLOY_COMMAND'] = 'exit 7'
        result = self.run_deploy()
        self.assertEqual(result.returncode, 7)
        self.assertIn('POST_DEPLOY_COMMAND', result.stderr)
        self.assertNotIn('Deploy hoàn tất.', result.stdout)

    def test_failed_environment_setup_never_restarts(self):
        self.env.update(INSTALL_DEPS='1', PYTHON_BIN='/usr/bin/false')
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / 'systemctl-calls').exists())

    def test_empty_command_skips_restart(self):
        self.env['POST_DEPLOY_COMMAND'] = ''
        self.assertEqual(self.run_deploy().returncode, 0)
        self.assertFalse((self.target / 'systemctl-calls').exists())
