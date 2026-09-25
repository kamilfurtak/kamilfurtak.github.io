"""Deterministic full-manager startup cancellation; no model or real Pi."""
import threading
from unittest.mock import patch
from test_pi_manager import PiManagerTestCase, TEST_THRESHOLDS, wait_until
from fakes import FakePiProcess, fake_popen_factory
from core import PiManager, EXEC_ABORTED, EXEC_RUNNING, EXEC_SETTLED
import rpc_transport


class TestStartAbort(PiManagerTestCase):
    def test_blocked_spawn_does_not_block_second_task_in_same_manager(self):
        first, second = FakePiProcess(), FakePiProcess()
        entered, release = threading.Event(), threading.Event()
        def spawn(argv, cwd):
            process = first if cwd == str(self.cwd_dir) else second
            if process is first:
                entered.set()
                if not release.wait(4):
                    raise RuntimeError('test spawn barrier timed out')
            process.sync_session_state_from_argv(argv)
            return process
        manager = PiManager(registry=self.registry, popen_factory=spawn,
                            clock=self.clock, default_thresholds=TEST_THRESHOLDS)
        self.addCleanup(manager.shutdown)
        self.addCleanup(first.finish)
        self.addCleanup(second.finish)
        self.addCleanup(release.set)
        other = self.tmp / 'second-worktree'
        other.mkdir()
        ta = manager.start_task(prompt='first', cwd=str(self.cwd_dir))['task_id']
        self.assertTrue(entered.wait(2))
        tb = manager.start_task(prompt='second', cwd=str(other))['task_id']
        self.assertTrue(wait_until(lambda: manager.status(tb)['execution_state'] == 'RUNNING'))
        self.assertTrue(manager.abort_task(ta)['cancellation_pending'])
        self.assertEqual(manager.status(tb)['execution_state'], 'RUNNING')
        self.assertIn(ta, manager._execution_locks)
        release.set()
        self.assertTrue(wait_until(lambda: not manager._rt(ta).boot_pending))
        self.assertEqual(manager.status(ta)['execution_state'], EXEC_ABORTED)
        self.assertEqual(manager.status(tb)['execution_state'], 'RUNNING')
        self.assertFalse(any(c['type'] == 'prompt' for c in first.commands_received))
        self.assertNotEqual(manager.status(ta)['session_id'], manager.status(tb)['session_id'])
        manager.abort_task(tb)

    def test_cleanup_without_confirmed_exit_retains_owner_and_is_retryable(self):
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        task = self.start_and_boot(manager, process)
        self.assertTrue(wait_until(lambda: not manager._rt(task).boot_pending))
        process.terminate_is_effective = False
        with patch.object(process, 'kill', lambda: None):
            result = manager.abort_task(task)
        self.assertTrue(result['cancellation_pending'])
        self.assertNotEqual(result['execution_state'], EXEC_ABORTED)
        self.assertIn(task, manager._execution_locks)
        self.assertIsNone(self.registry.get_task(task)['wake_state'])
        process.terminate_is_effective = True
        result = manager.abort_task(task)
        self.assertFalse(result['cancellation_pending'])
        self.assertEqual(result['execution_state'], EXEC_ABORTED)
        self.assertNotIn(task, manager._execution_locks)

    def test_settlement_arriving_during_abort_cannot_run_verifier(self):
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        task = self.start_and_boot(manager, process)
        def request_then_stale_settlement(*a, **kw):
            manager._on_event(task, {'type': 'agent_settled'})
        with patch.object(manager._rt(task).transport, 'send_abort', request_then_stale_settlement):
            manager.abort_task(task)
        self.assertEqual(manager.status(task)['execution_state'], EXEC_ABORTED)
        self.assertEqual(manager.status(task)['verification_state'], 'NOT_RUN')
        self.assertIsNone(self.registry.get_task(task)['wake_state'])

    def test_abort_during_spawn_retains_owner_and_reaps_late_process(self):
        entered, release = threading.Event(), threading.Event()
        process = FakePiProcess()
        def spawn(argv, cwd):
            entered.set()
            if not release.wait(4): raise RuntimeError('test barrier timeout')
            return fake_popen_factory(process)(argv,cwd)
        manager = PiManager(self.registry,popen_factory=spawn,clock=self.clock,default_thresholds=TEST_THRESHOLDS)
        self.addCleanup(manager.shutdown)
        self.addCleanup(release.set)
        task = manager.start_task('do not execute',str(self.cwd_dir))['task_id']
        self.assertTrue(entered.wait(2))
        response = manager.abort_task(task)
        owner_kept = task in manager._execution_locks
        release.set()
        self.assertTrue(wait_until(lambda: process.poll() is not None or any(c['type']=='prompt' for c in process.commands_received)))
        self.assertEqual([c for c in process.commands_received if c['type']=='prompt'],[])
        self.assertIsNotNone(process.poll())
        self.assertTrue(owner_kept,'abort released execution while spawn still could publish a process')
        self.assertTrue(wait_until(lambda: task not in manager._execution_locks))
        self.assertEqual(manager.status(task)['execution_state'],EXEC_ABORTED)
        self.assertTrue(response.get('cancellation_pending'))
        self.assertIsNone(self.registry.get_task(task)['wake_state'])

    def test_abort_before_spawn_has_no_process(self):
        manager=self.make_manager(FakePiProcess()); self.addCleanup(manager.shutdown)
        with patch.object(threading.Thread,'start'):
            task=manager.start_task('never',str(self.cwd_dir))['task_id']
        manager.abort_task(task)
        row=self.registry.get_task(task)
        with patch.object(manager,'_spawn_process') as spawn:
            manager._boot_and_run(task,'never',str(self.cwd_dir),None,row['session_file'])
        spawn.assert_not_called()
        self.assertEqual(manager.status(task)['execution_state'],EXEC_ABORTED)
        self.assertNotIn(task,manager._execution_locks)

    def test_spawn_exception_after_abort_does_not_request_wake(self):
        entered,release=threading.Event(),threading.Event()
        def spawn(*args):
            entered.set(); release.wait(4); raise OSError('spawn failed')
        manager=PiManager(self.registry,popen_factory=spawn,clock=self.clock,default_thresholds=TEST_THRESHOLDS)
        self.addCleanup(manager.shutdown);self.addCleanup(release.set)
        task=manager.start_task('never',str(self.cwd_dir),origin={'session_key':'test'})['task_id']
        self.assertTrue(entered.wait(2));manager.abort_task(task);release.set()
        self.assertTrue(wait_until(lambda: task not in manager._execution_locks))
        self.assertEqual(manager.status(task)['execution_state'],EXEC_ABORTED)
        self.assertIsNone(self.registry.get_task(task)['wake_state'])

    def test_abort_after_transport_before_prompt(self):
        process=FakePiProcess(); manager=self.make_manager(process);self.addCleanup(manager.shutdown)
        entered,release=threading.Event(),threading.Event()
        original=manager._executor_identity_error
        def gate(*args):
            entered.set();release.wait(4);return original(*args)
        self.addCleanup(release.set)
        with patch.object(manager,'_executor_identity_error',gate):
            task=manager.start_task('never',str(self.cwd_dir))['task_id']
            self.assertTrue(entered.wait(2));manager.abort_task(task);release.set()
            self.assertTrue(wait_until(lambda: task not in manager._execution_locks))
        self.assertEqual([c for c in process.commands_received if c['type']=='prompt'],[])
        self.assertEqual(manager.status(task)['execution_state'],EXEC_ABORTED)

    def test_final_state_cannot_be_resurrected(self):
        process=FakePiProcess();manager=self.make_manager(process);self.addCleanup(manager.shutdown)
        task=self.start_and_boot(manager,process); manager.abort_task(task)
        manager._set_execution_state(task,EXEC_RUNNING,last_error=None)
        self.assertEqual(manager.status(task)['execution_state'],EXEC_ABORTED)

    def test_registry_closes_during_executor_check_without_thread_crash_or_prompt(self):
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        errors = []
        original = manager._executor_identity_error
        def close_before_check(row, state):
            self.registry.close()
            return original(self.registry.get_task(row["task_id"]), state)
        with patch.object(manager, "_executor_identity_error", close_before_check), \
                patch.object(threading, "excepthook", errors.append):
            task = manager.start_task("must not reach Pi", str(self.cwd_dir))["task_id"]
            self.assertTrue(wait_until(lambda: not manager._rt(task).boot_pending))
        self.assertEqual(errors, [], [str(error.exc_value) for error in errors])
        self.assertFalse(any(c["type"] == "prompt" for c in process.commands_received))
        self.assertNotIn(task, manager._execution_locks)
        self.assertTrue(manager._rt(task).lsp_feedback._stop.is_set())

    def test_feedback_stops_on_spawn_failure_and_shutdown(self):
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        task = self.start_and_boot(manager, process)
        self.assertFalse(manager._rt(task).lsp_feedback._stop.is_set())
        manager.shutdown()
        self.assertTrue(manager._rt(task).lsp_feedback._stop.is_set())
        failed_manager = PiManager(self.registry, clock=self.clock,
            default_thresholds=TEST_THRESHOLDS,
            popen_factory=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn denied")))
        self.addCleanup(failed_manager.shutdown)
        failed = failed_manager.start_task("fail", str(self.cwd_dir))["task_id"]
        self.assertTrue(wait_until(lambda: not failed_manager._rt(failed).boot_pending))
        self.assertTrue(failed_manager._rt(failed).lsp_feedback._stop.is_set())

    # -- issue #1 section A gaps: in-flight admission, repeat abort, single prompt --

    def test_in_flight_prompt_abort_stops_without_false_claim(self):
        """A4: abort while the prompt send is ALREADY admitted.

        The abort cannot un-send an admitted prompt: it must wait for the
        admission to finish, then stop the task for real. The honest outcome
        is one prompt on the wire plus a confirmed ABORTED — never a claim
        that nothing was ever sent."""
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        entered, release = threading.Event(), threading.Event()
        original = rpc_transport.PiRpcTransport.send_prompt

        def blocking_send_prompt(self, *args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('prompt admission barrier timed out')
            return original(self, *args, **kwargs)

        results: list = []
        finished = threading.Event()

        def run_abort(task_id):
            try:
                results.append(manager.abort_task(task_id))
            finally:
                finished.set()

        with patch.object(rpc_transport.PiRpcTransport, 'send_prompt', blocking_send_prompt):
            task = manager.start_task(prompt='admitted work', cwd=str(self.cwd_dir))['task_id']
            self.assertTrue(entered.wait(2), 'prompt admission never started')
            aborter = threading.Thread(target=run_abort, args=(task,), daemon=True)
            aborter.start()
            # The abort holds on abort_lock: prompt admission is linearized.
            self.assertFalse(finished.wait(0.5),
                             'abort finalized while the prompt admission was in flight')
            release.set()
            self.assertTrue(finished.wait(5), 'abort never completed after admission')
        prompts = [c for c in process.commands_received if c['type'] == 'prompt']
        self.assertEqual(len(prompts), 1, 'exactly the ONE admitted prompt must be on the wire')
        self.assertEqual(results[0]['execution_state'], EXEC_ABORTED)
        self.assertFalse(results[0].get('cancellation_pending'),
                         'cleanup is confirmed, so nothing may stay pending')
        self.assertEqual(manager.status(task)['execution_state'], EXEC_ABORTED)
        self.assertIsNone(self.registry.get_task(task)['wake_state'])
        self.assertNotIn(task, manager._execution_locks)

    def test_repeat_and_post_final_abort_are_safe(self):
        """A5: a second abort while cleanup is pending, and abort after the
        final state, must not double-release or re-run cleanup dangerously."""
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        task = self.start_and_boot(manager, process)
        self.assertTrue(wait_until(lambda: not manager._rt(task).boot_pending))
        process.terminate_is_effective = False
        with patch.object(process, 'kill', lambda: None):
            first = manager.abort_task(task)
            second = manager.abort_task(task)
        self.assertTrue(first['cancellation_pending'])
        self.assertTrue(second['cancellation_pending'])
        self.assertEqual(manager.status(task)['execution_state'], EXEC_RUNNING)
        self.assertIn(task, manager._execution_locks)
        process.terminate_is_effective = True
        final = manager.abort_task(task)
        self.assertFalse(final['cancellation_pending'])
        self.assertEqual(final['execution_state'], EXEC_ABORTED)
        self.assertNotIn(task, manager._execution_locks)
        again = manager.abort_task(task)  # idempotent, no second cleanup
        self.assertTrue(again.get('already_final'))
        self.assertEqual(again['execution_state'], EXEC_ABORTED)
        self.assertNotIn(task, manager._execution_locks)
        self.assertIsNone(self.registry.get_task(task)['wake_state'])

    def test_normal_start_and_settle_send_exactly_one_prompt(self):
        """A9: the healthy path admits exactly one prompt — a settle or an
        event flood must never re-send it."""
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        task = self.start_and_boot(manager, process)
        self.emit_and_sync(manager, process, task, {'type': 'agent_settled'})
        self.assertEqual(manager.status(task)['execution_state'], EXEC_SETTLED)
        prompts = [c for c in process.commands_received if c['type'] == 'prompt']
        self.assertEqual(len(prompts), 1, 'normal start must send exactly one prompt')


    def test_parallel_aborts_are_safe_and_do_not_double_release(self):
        """A5 (concurrent form): two simultaneous abort calls must not crash,
        double-release ownership or run dangerous cleanup twice."""
        process = FakePiProcess()
        manager = self.make_manager(process)
        self.addCleanup(manager.shutdown)
        self.addCleanup(process.finish)
        task = self.start_and_boot(manager, process)
        self.assertTrue(wait_until(lambda: not manager._rt(task).boot_pending))
        process.terminate_is_effective = False
        barrier = threading.Barrier(2)
        results: list = []
        errors: list = []

        def run_abort():
            try:
                barrier.wait(timeout=5)
                results.append(manager.abort_task(task))
            except Exception as exc:  # surfaced by the assertion below
                errors.append(exc)

        with patch.object(process, 'kill', lambda: None):
            threads = [threading.Thread(target=run_abort, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.get('cancellation_pending') for r in results))
        self.assertEqual(manager.status(task)['execution_state'], EXEC_RUNNING)
        self.assertIn(task, manager._execution_locks)
        process.terminate_is_effective = True
        final = manager.abort_task(task)
        self.assertFalse(final['cancellation_pending'])
        self.assertEqual(final['execution_state'], EXEC_ABORTED)
        self.assertNotIn(task, manager._execution_locks)
        self.assertIsNone(self.registry.get_task(task)['wake_state'])
