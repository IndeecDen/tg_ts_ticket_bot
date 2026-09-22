import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure', ROOT / 'deploy/configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class InstallerTests(unittest.TestCase):
    def test_prompts_reject_injection_and_invalid_values(self):
        with patch('builtins.input', side_effect=['1\nBOT_TOKEN=bad', '0', '45']):
            self.assertEqual(configure.ask('timeout', r'[1-9][0-9]*'), '45')

    def test_configuration_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.env'
            token = '123456:' + 'a' * 35
            with patch.object(sys, 'argv', ['configure.py', str(target)]), \
                    patch('getpass.getpass', return_value=token), \
                    patch('builtins.input', side_effect=['-1001234567890', '']):
                configure.main()
                saved = target.read_text()
                self.assertIn('RESPONSE_TIMEOUT=300', saved)
                with self.assertRaises(SystemExit):
                    configure.main()
                self.assertEqual(target.read_text(), saved)

    def test_external_env_is_used_and_timeout_saved_there(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.env'
            target.write_text('BOT_TOKEN=test\nWORK_CHAT_ID=-1001\nRESPONSE_TIMEOUT=300\n')
            env = {key: value for key, value in os.environ.items()
                   if key.upper() not in {'BOT_TOKEN', 'WORK_CHAT_ID', 'RESPONSE_TIMEOUT'}}
            env['BOT_ENV_FILE'] = str(target)
            result = subprocess.run([sys.executable, '-c',
                'from config import Config; c=Config(); assert c.work_chat_id == -1001; '
                'assert c.update_response_timeout(42); assert Config().response_timeout == 42'],
                cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RESPONSE_TIMEOUT='42'", target.read_text())
