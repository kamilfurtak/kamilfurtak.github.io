"""The test gate must fail closed, including failures outside unittest methods."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class TestRunnerContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'tests').mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'scripts/run-tests.py',
                        self.root / 'scripts/run-tests.py')

    def module(self, body, name='test_sample'):
        (self.root / 'tests' / (name + '.py')).write_text(body)

    def run_suite(self, suite='unit'):
        return subprocess.run([sys.executable, str(self.root / 'scripts/run-tests.py'),
                               '--suite', suite], capture_output=True, text=True, timeout=20)

    def test_zero_tests_and_skips_are_not_green(self):
        for content in ('', 'import unittest\n@unittest.skip("missing")\n'
                        'class Tests(unittest.TestCase):\n def test_missing(self): pass\n'):
            with self.subTest(content=content):
                self.module(content)
                self.assertNotEqual(self.run_suite().returncode, 0)

    def test_uncaught_thread_exception_is_not_green(self):
        self.module('import threading, unittest\nclass Tests(unittest.TestCase):\n'
                    ' def test_thread(self):\n'
                    '  def crash(): raise RuntimeError("fixture worker failure")\n'
                    '  thread = threading.Thread(target=crash)\n'
                    '  thread.start()\n  thread.join()\n')
        result = self.run_suite()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Exception in thread', result.stdout)

    def test_gate_classifies_contract_and_host_modules_explicitly(self):
        content = 'import unittest\nclass Tests(unittest.TestCase):\n def test_ok(self): pass\n'
        for name in ('test_unit', 'test_cli_delivery', 'test_activity_http',
                     'test_loader_integration', 'test_cli_monitor', 'test_cli_startup_host'):
            self.module(content, name)
        for suite, count in (('unit', 1), ('contract', 2), ('host', 3)):
            with self.subTest(suite=suite):
                result = self.run_suite(suite)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f'SUMMARY suite={suite} modules={count} tests={count} failed=[]', result.stdout)

    def test_plugin_root_imports_are_available_in_child_processes(self):
        (self.root / 'review_fixture.py').write_text('VALUE = 42\n')
        self.module('import subprocess, sys, unittest\nclass Tests(unittest.TestCase):\n'
                    ' def test_import(self):\n'
                    '  subprocess.run([sys.executable, "-c", '
                    '"from review_fixture import VALUE; assert VALUE == 42"], check=True)\n')
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_modules_do_not_share_persistent_hermes_state(self):
        self.module('import os, pathlib, unittest\nclass Tests(unittest.TestCase):\n'
                    ' def test_write(self):\n'
                    '  (pathlib.Path(os.environ["HERMES_HOME"]) / "state").write_text("old")\n',
                    'test_a_write')
        self.module('import os, pathlib, unittest\nclass Tests(unittest.TestCase):\n'
                    ' def test_read(self):\n'
                    '  self.assertFalse((pathlib.Path(os.environ["HERMES_HOME"]) / "state").exists())\n',
                    'test_b_read')
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
