"""Profile policy and tool boundary tests (temporary homes; no providers)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_pi_manager  # Install standalone package import path.
import tools
from executors import load_profile_config
from test_executor_profiles import PROFILES


class ExecutorConfigTest(unittest.TestCase):
    def test_absent_policy_preserves_existing_defaults(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(load_profile_config(home), {})

    def test_explicit_policy_is_loaded(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'policy/pi-executors.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'default': 'local', 'profiles': PROFILES}))
            self.assertEqual(load_profile_config(home),
                             {'default_executor': 'local', 'executor_profiles': PROFILES})

    def test_invalid_policy_is_not_silently_ignored(self):
        for text in ['broken-json', '{}', '[]',
                     json.dumps({'default': 'typo', 'profiles': PROFILES}),
                     json.dumps({'default': 'local', 'profiles': PROFILES, 'fallback': 'commercial'})]:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as home:
                path = Path(home) / 'policy/pi-executors.json'
                path.parent.mkdir()
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_profile_config(home)

    def test_tool_forwards_executor_and_preserves_origin(self):
        manager = mock.Mock()
        manager.start_task.return_value = {'task_id': 'fixture'}
        origin = {'session_key': 'fixture-session'}
        with tempfile.TemporaryDirectory() as cwd, \
                mock.patch.object(tools, 'get_manager', return_value=manager), \
                mock.patch.object(tools, '_capture_routing', return_value=origin):
            tools.handle_pi_task({'cwd': cwd, 'prompt': 'fixture', 'executor': 'commercial',
                                  'verifier_argv': ['python3', 'verify.py']})
        kwargs = manager.start_task.call_args.kwargs
        self.assertEqual(kwargs['executor'], 'commercial')
        self.assertEqual(kwargs['origin'], origin)
        self.assertEqual(kwargs['verifier'].argv, ['python3', 'verify.py'])
        self.assertIn('executor', tools.PI_TASK_SCHEMA['parameters']['properties'])

    def test_manager_loads_policy_before_recovery(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'policy/pi-executors.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'default': 'local', 'profiles': PROFILES}))
            with mock.patch.object(tools, '_manager', None), \
                    mock.patch.object(tools, 'default_db_path', return_value=Path(home) / 'state/pi-manager/registry.sqlite3'), \
                    mock.patch.object(tools, 'Registry') as registry, \
                    mock.patch.object(tools, 'NotificationOutbox'), \
                    mock.patch.object(tools, 'ActivityRecorder'), \
                    mock.patch.object(tools, 'PiManager') as constructor:
                registry.return_value.path = Path(home) / 'state/pi-manager/registry.sqlite3'
                manager = tools.get_manager()
                self.assertEqual(constructor.call_args.kwargs['executor_profiles'], PROFILES)
                self.assertEqual(constructor.call_args.kwargs['default_executor'], 'local')
                manager.recover_all.assert_called_once()
