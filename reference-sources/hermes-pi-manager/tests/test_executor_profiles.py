"""Executor selection is task-local, durable and fail-closed (no model calls)."""
import json
from pathlib import Path
from unittest import mock

from test_pi_manager import PiManagerTestCase, FakePiProcess, TEST_THRESHOLDS, wait_until
from core import PiManager

PROFILES = {
    'commercial': {'provider': 'fixture-cloud', 'model': 'fixture-model', 'thinking': 'max'},
    'local': {'provider': 'fixture-local', 'model': 'fixture-small', 'thinking': 'medium'},
}


class ExecutorProfilesTest(PiManagerTestCase):
    def manager(self, profiles=None, factory=None):
        manager = PiManager(self.registry, popen_factory=factory or (lambda *a: None),
                            clock=self.clock, default_thresholds=TEST_THRESHOLDS,
                            executor_profiles=PROFILES if profiles is None else profiles,
                            default_executor='local')
        self.addCleanup(manager.shutdown)
        return manager

    def test_persist_before_spawn_and_use_selected_argv(self):
        proc = FakePiProcess()
        proc.state.update(model={'provider': 'fixture-cloud', 'id': 'fixture-model'}, thinkingLevel='max')
        seen = []
        def factory(argv, cwd):
            row = self.registry.list_tasks()[0]
            seen.append((argv, json.loads(row['executor_spec'])))
            proc.sync_session_state_from_argv(argv)
            return proc
        manager = self.manager(factory=factory)
        task = manager.start_task('read fixture', str(self.cwd_dir), executor='commercial')
        self.assertTrue(wait_until(lambda: any(c['type'] == 'prompt' for c in proc.commands_received)))
        argv, spec = seen[0]
        self.assertEqual(spec, PROFILES['commercial'])
        self.assertEqual(argv[argv.index('--model') + 1], 'fixture-model')
        self.assertEqual(argv[argv.index('--thinking') + 1], 'max')
        self.assertEqual(task['executor'], {'profile': 'commercial', **PROFILES['commercial']})
        self.assertEqual(manager.status(task['task_id'])['executor'], task['executor'])

    def test_unknown_profile_has_no_side_effects(self):
        factory = mock.Mock()
        manager = self.manager(factory=factory)
        with self.assertRaises(ValueError):
            manager.start_task('x', str(self.cwd_dir), executor='typo')
        self.assertEqual(self.registry.list_tasks(), [])
        factory.assert_not_called()

    def test_default_profile_and_no_mutation(self):
        profiles = json.loads(json.dumps(PROFILES))
        manager = self.manager(profiles)
        with mock.patch.object(manager, '_boot_and_run'):
            a = manager.start_task('x', str(self.cwd_dir))
            profiles['local']['model'] = 'changed'
            b = manager.start_task('x', str(self.cwd_dir), executor='commercial')
        self.assertEqual(a['executor']['model'], 'fixture-small')
        self.assertEqual(b['executor']['model'], 'fixture-model')
        self.assertEqual(manager.model, 'qwen3.8-27b')

    def test_recovery_uses_snapshot_not_current_profile(self):
        proc = FakePiProcess()
        proc.state.update(model={'provider': 'fixture-cloud', 'id': 'fixture-model'}, thinkingLevel='max')
        captured = []
        def factory(argv, cwd):
            captured.append(argv)
            proc.sync_session_state_from_argv(argv)
            return proc
        manager = self.manager(factory=factory)
        path = self.tmp / 'session.jsonl'
        path.write_text('{"id":"seed"}\n')
        self.registry.create_task(task_id='recover', cwd=str(self.cwd_dir),
            execution_state='RUNNING', session_file=str(path), expected_session_file=str(path),
            expected_session_id='sess-fake-1', executor_profile='removed-profile',
            executor_spec=json.dumps(PROFILES['commercial']))
        result = manager.recover_task('recover')
        self.assertTrue(result['recovered'])
        argv = captured[0]
        self.assertEqual(argv[argv.index('--provider') + 1], 'fixture-cloud')
        self.assertEqual(argv[argv.index('--thinking') + 1], 'max')
        self.assertFalse(any(c['type'] == 'prompt' for c in proc.commands_received))

    def test_profile_mode_rejects_legacy_or_corrupt_recovery_without_spawn(self):
        for i, spec in enumerate([None, 'not-json', '{"model":"incomplete"}']):
            with self.subTest(spec=spec):
                factory = mock.Mock()
                manager = self.manager(factory=factory)
                path = self.tmp / f'session-{i}.jsonl'
                path.write_text('{"id":"seed"}\n')
                self.registry.create_task(task_id=f'recover-{i}', cwd=str(self.cwd_dir),
                    execution_state='RUNNING', session_file=str(path), executor_spec=spec)
                result = manager.recover_task(f'recover-{i}')
                self.assertFalse(result['recovered'])
                self.assertIn('executor', result['reason'])
                factory.assert_not_called()

    def test_invalid_profile_config_rejected(self):
        for spec in [{}, {**PROFILES['local'], 'thinking': 'MAX'},
                     {**PROFILES['local'], 'model': '--override'},
                     {**PROFILES['local'], 'token': 'not-allowed'}]:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.manager({'local': spec})

    def test_wrong_runtime_model_or_thinking_never_receives_prompt(self):
        for state in [{}, {'model': {'provider': 'fixture-cloud', 'id': 'wrong'}, 'thinkingLevel': 'max'},
                      {'model': {'provider': 'fixture-cloud', 'id': 'fixture-model'}, 'thinkingLevel': 'high'}]:
            with self.subTest(state=state):
                proc = FakePiProcess()
                proc.state.update(state)
                def factory(argv, cwd):
                    proc.sync_session_state_from_argv(argv)
                    return proc
                manager = self.manager(factory=factory)
                task = manager.start_task('must not execute', str(self.cwd_dir), executor='commercial')
                self.assertTrue(wait_until(lambda: manager.status(task['task_id'])['execution_state'] == 'CRASHED'))
                self.assertFalse(any(c['type'] == 'prompt' for c in proc.commands_received))
                self.assertIn('executor', manager.status(task['task_id'])['last_error'])
                self.assertIsNotNone(proc.poll())

    def test_recovery_rejects_changed_runtime_before_replay(self):
        proc = FakePiProcess()
        proc.state.update({'model': {'provider': 'fixture-cloud', 'id': 'fixture-model'}, 'thinkingLevel': 'high'})
        def factory(argv, cwd):
            proc.sync_session_state_from_argv(argv)
            return proc
        manager = self.manager(factory=factory)
        path = self.tmp / 'recovery-mismatch.jsonl'
        path.write_text('{"id":"seed"}\n')
        self.registry.create_task(task_id='mismatch', cwd=str(self.cwd_dir),
            execution_state='RUNNING', session_file=str(path), executor_profile='commercial',
            executor_spec=json.dumps(PROFILES['commercial']))
        result = manager.recover_task('mismatch')
        self.assertFalse(result['recovered'])
        self.assertEqual(result['reason'], 'recovery_executor_identity_mismatch')
        self.assertFalse(any(c['type'] in ('prompt', 'get_entries') for c in proc.commands_received))
        self.assertIsNotNone(proc.poll())

    def test_two_active_tasks_keep_distinct_executor_snapshots(self):
        procs, launches = [], []
        def factory(argv, cwd):
            proc = FakePiProcess()
            proc.sync_session_state_from_argv(argv)
            proc.state.update({'model': {'provider': argv[argv.index('--provider')+1],
                                         'id': argv[argv.index('--model')+1]},
                               'thinkingLevel': argv[argv.index('--thinking')+1]})
            launches.append(argv)
            procs.append(proc)
            return proc
        manager = self.manager(factory=factory)
        a = manager.start_task('A', str(self.cwd_dir), executor='commercial')
        b = manager.start_task('B', str(self.cwd_dir), executor='local')
        self.assertTrue(wait_until(lambda: all(manager.status(t['task_id'])['execution_state']=='RUNNING' for t in (a,b))))
        self.assertEqual(len(launches), 2)
        self.assertEqual(manager.status(a['task_id'])['executor']['model'], 'fixture-model')
        self.assertEqual(manager.status(b['task_id'])['executor']['model'], 'fixture-small')
        self.assertNotEqual(a['session_file'], b['session_file'])
        for proc in procs:
            proc.emit({'type': 'agent_settled'})
        self.assertTrue(wait_until(lambda: all(manager.status(t['task_id'])['execution_state']=='SETTLED' for t in (a,b))))

    def test_missing_default_rejected(self):
        with self.assertRaises(ValueError):
            self.manager({'commercial': PROFILES['commercial']})
