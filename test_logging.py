import codecs
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class LoggingTests(unittest.TestCase):
    def test_utf8_log_supports_chinese_path(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'nested' / '中文.log'
            result = subprocess.run([sys.executable, str(ROOT / 'SwitchTest.py'), 'echo',
                                     '--host', '127.0.0.1', '--timeout', 'nan', '--log', str(path)],
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stderr)
            data = path.read_bytes()
            self.assertTrue(data.startswith(codecs.BOM_UTF8))
            self.assertIn('必须是有限非负数', data.decode('utf-8-sig'))
            self.assertNotIn('\ufffd', data.decode('utf-8-sig'))

    def test_direct_command_automatically_logs_console_and_error(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            result = subprocess.run([sys.executable, str(ROOT / 'SwitchTest.py'), 'echo',
                                     '--host', '127.0.0.1', '--timeout', 'nan'],
                                    cwd=directory, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            logs = list((Path(directory) / 'out' / 'logs').glob('*.log'))
            self.assertEqual(len(logs), 1)
            self.assertTrue(logs[0].read_bytes().startswith(codecs.BOM_UTF8))
            self.assertIn('日志自动保存', result.stdout.decode('utf-8'))
            self.assertIn('必须是有限非负数', logs[0].read_text(encoding='utf-8-sig'))

    def test_direct_log_option_and_no_log(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / 'chosen.log'
            args = [sys.executable, str(ROOT / 'SwitchTest.py'), 'echo',
                    '--host', '127.0.0.1', '--timeout', 'nan']
            result = subprocess.run([*args, '--log', str(path)], cwd=directory,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn('必须是有限非负数', path.read_text(encoding='utf-8-sig'))
            original = path.read_bytes()
            repeated = subprocess.run([*args, '--log', str(path)], cwd=directory,
                                      capture_output=True, timeout=5)
            self.assertEqual(repeated.returncode, 1)
            self.assertEqual(path.read_bytes(), original)
            result = subprocess.run([*args, '--no-log'], cwd=directory,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((Path(directory) / 'out').exists())


if __name__ == '__main__':
    unittest.main()
