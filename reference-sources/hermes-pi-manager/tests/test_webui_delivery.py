"""WebUI is not Desktop; real manager queues with fake host admission only."""
import json
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from test_terminal_wake import _OutboxManagerCase, FakePluginContext
from wake_worker import TerminalWakeWorker
from outbox import OutboxWorker
import desktop_host


class TestWebUIDelivery(_OutboxManagerCase):
    def setUp(self):
        super().setUp()
        self.origin = {'platform': 'webui', 'session_key': 'web-a',
                       'session_id': 'web-a', 'ui_session_id': 'web-a',
                       'hermes_home': '/test/hermes'}
        self.turns, self.frames = [], []
        self.session = SimpleNamespace(session_id='web-a', profile=None, active_stream_id=None)
        self.routes = ModuleType('api.routes')
        self.routes.get_session = lambda sid: self.session
        self.routes._active_stream_blocks_chat_start = lambda s, sid: bool(sid)
        def start_session_turn(sid, message, *, source='process_wakeup'):
            self.turns.append((sid, message, {'source': source}))
            return {'_status': 200, 'stream_id': 'stream-proof'}
        self.routes.start_session_turn = start_session_turn
        self.profiles = ModuleType('api.profiles')
        self.profiles._resolve_profile_home_for_name = lambda name: '/test/hermes'
        self.channel = SimpleNamespace(subscriber_count=lambda: 1,
            emit=lambda event, data: self.frames.append((event,data)) or 1)
        self.background = ModuleType('api.background_process')
        self.background.get_session_channel = lambda sid: self.channel
        self.config = ModuleType('api.config')
        self.config._get_session_agent_lock = lambda sid: threading.RLock()
        try:
            import hermes_constants
        except ImportError:  # isolated run: fake the runtime's home-override helpers
            hermes_constants = ModuleType('hermes_constants')
            hermes_constants.set_hermes_home_override = lambda home: ('home', home)
            hermes_constants.reset_hermes_home_override = lambda token: None
        self.mods = patch.dict('sys.modules', {'api.routes': self.routes, 'api.profiles': self.profiles,
            'api.background_process': self.background, 'api.config': self.config,
            'hermes_constants': hermes_constants})
        self.mods.start(); self.addCleanup(self.mods.stop)
        self.ctx = FakePluginContext(); self.ctx._gateway_injection_allowed = lambda: True
        self.worker = TerminalWakeWorker(self.registry, self.ctx, now_fn=self.clock)

    def test_direct_mode_success_without_status_is_accepted(self):
        """Regression (observed live 2026-09-20): the native direct-mode start
        response carries ``stream_id`` but NO ``_status`` key at all; the
        deployed classifier required _status == 200 and mislabeled a wake
        that actually started as 'uncertain'."""
        task = self._wake('pi-direct', origin=self.origin)

        def direct(sid, message, *, source='process_wakeup'):
            self.turns.append((sid, message, {'source': source}))
            return {'stream_id': 'stream-live', 'session_id': sid,
                    'pending_started_at': 1.0, 'turn_id': 'turn-1', 'title': ''}

        self.routes.start_session_turn = direct
        self.worker.run_once(now=self.clock())
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.registry.get_task(task)['wake_state'], 'accepted')

    def test_statusless_response_without_stream_id_stays_uncertain(self):
        task = self._wake('pi-ambiguous', origin=self.origin)
        self.routes.start_session_turn = (
            lambda sid, message, *, source='process_wakeup': {'ok': True})
        self.worker.run_once(now=self.clock())
        self.assertEqual(self.registry.get_task(task)['wake_state'], 'uncertain')

    def test_tool_does_not_offer_desktop_card_to_webui(self):
        import tools
        manager = SimpleNamespace(start_task=lambda **kw: {'task_id': 'pi-webui-tool'})
        with patch.object(tools, 'get_manager', return_value=manager), \
                patch.object(tools, '_capture_routing', return_value=self.origin):
            result = tools.handle_pi_task({'prompt': 'read only', 'cwd': str(self.tmp)})
        self.assertNotIn('error', result)
        self.assertNotIn('desktop_live_view', result)

    def test_webui_is_not_desktop(self):
        self.assertFalse(desktop_host.is_desktop(self.origin))

    def test_closed_tab_does_not_spend_notice_retries_or_block_wake(self):
        task = self._wake('pi-closed-tab', origin=self.origin)
        nid = self.outbox.enqueue(task, 'settled', 'Completed', now=self.clock())
        self.channel.subscriber_count = lambda: 0
        worker = OutboxWorker(self.outbox, now_fn=self.clock)
        worker.run_once(now=self.clock())
        self.assertEqual(self.registry.get_notification(nid)['attempts'], 0)
        self.worker.run_once(now=self.clock())
        self.assertEqual(len(self.turns), 1)

    def test_progress_is_not_misreported_as_completion(self):
        task = self._wake('pi-progress', origin=self.origin)
        nid = self.outbox.enqueue(task, 'progress', 'Working', now=self.clock())
        OutboxWorker(self.outbox, now_fn=self.clock).run_once(now=self.clock())
        self.assertEqual(self.frames, [])
        row = self.registry.get_notification(nid)
        self.assertEqual(row['status'], 'failed')
        self.assertIn('no passive progress', row['last_error'])

    def test_sse_failure_does_not_claim_delivery(self):
        task = self._wake('pi-sse-failure', origin=self.origin)
        nid = self.outbox.enqueue(task, 'settled', 'Completed', now=self.clock())
        self.channel.emit = lambda *a: 0
        OutboxWorker(self.outbox, now_fn=self.clock).run_once(now=self.clock())
        row = self.registry.get_notification(nid)
        self.assertNotEqual(row['status'], 'sent')
        self.assertEqual(row['attempts'], 1)

    def test_exact_session_one_admission_no_gateway(self):
        task = self._wake('pi-web', origin=self.origin)
        self.worker.run_once(now=self.clock()); self.worker.run_once(now=self.clock())
        self.assertEqual(len(self.turns),1)
        self.assertEqual(self.turns[0][0],'web-a')
        self.assertEqual(self.registry.get_task(task)['wake_state'],'accepted')
        self.assertEqual(self.ctx.injects,[])

    def test_busy_then_native_race_keeps_retry_budget(self):
        task=self._wake('pi-busy',origin=self.origin)
        self.session.active_stream_id='human'
        self.worker.run_once(now=self.clock())
        self.assertEqual(self.registry.get_task(task)['wake_attempts'],0)
        self.session.active_stream_id=None
        self.routes.start_session_turn=lambda *a,**k: {'_status':409}
        self.worker.run_once(now=self.clock())
        self.assertEqual(self.registry.get_task(task)['wake_attempts'],0)
        self.assertEqual(self.registry.get_task(task)['wake_state'],'pending')

    def test_foreign_profile_or_session_never_delivers(self):
        task=self._wake('pi-foreign',origin=self.origin)
        self.profiles._resolve_profile_home_for_name=lambda name:'/other/home'
        self.worker.run_once(now=self.clock())
        self.assertEqual(self.turns,[])
        self.assertEqual(self.registry.get_task(task)['wake_attempts'],0)
        self.profiles._resolve_profile_home_for_name=lambda name:'/test/hermes'
        self.session.session_id='different'
        self.worker.run_once(now=self.clock())
        self.assertEqual(self.turns,[])

    def test_exception_after_admission_is_uncertain_not_replayed(self):
        task=self._wake('pi-uncertain',origin=self.origin)
        def broken(*a,**k): raise RuntimeError('ambiguous')
        self.routes.start_session_turn=broken
        self.worker.run_once(now=self.clock());self.worker.run_once(now=self.clock())
        self.assertEqual(self.registry.get_task(task)['wake_state'],'uncertain')

    def test_missing_grant_never_starts_turn(self):
        task=self._wake('pi-denied',origin=self.origin)
        self.ctx._gateway_injection_allowed=lambda:False
        self.worker.run_once(now=self.clock())
        self.assertEqual(self.turns,[])
        self.assertIn('grant',self.registry.get_task(task)['wake_last_error'])

    def test_missing_host_never_falls_into_gateway(self):
        task=self._wake('pi-nohost',origin=self.origin)
        with patch.dict('sys.modules',{'api.routes':None}):self.worker.run_once(now=self.clock())
        self.assertEqual(self.ctx.injects,[])
        self.assertEqual(self.registry.get_task(task)['wake_attempts'],0)

    def test_passive_notification_no_turn_and_old_misclassified_row(self):
        task=self._wake('pi-notice',origin=self.origin)
        nid=self.outbox.enqueue(task,'settled','Zadanie zakończone',now=self.clock())
        self.assertEqual(self.registry.get_notification(nid)['platform'],'webui')
        # Recover a pre-fix row: origin, not the erroneous table projection, owns routing.
        self.registry._conn.execute("UPDATE notifications SET platform='tui' WHERE notification_id=?",(nid,))
        self.registry._conn.commit()
        worker=OutboxWorker(self.outbox,now_fn=self.clock)
        worker.run_once(now=self.clock());worker.run_once(now=self.clock())
        self.assertEqual(len(self.frames),1)
        self.assertEqual(self.frames[0][0],'bg_task_complete')
        self.assertEqual(self.frames[0][1]['task_id'],task)
        self.assertEqual(self.turns,[])
        self.assertEqual(self.registry.get_notification(nid)['status'],'sent')

    def test_non_dict_and_malformed_shapes_are_uncertain_not_crash(self):
        """Every malformed or ambiguous admission result must end as an
        UncertainDelivery-based 'uncertain' — never AttributeError, never a
        stuck 'dispatching' row, never a blind re-admission."""
        shapes = [None, [], "text", {}, {"ok": True},
                  {"stream_id": None}, {"stream_id": 123}, {"stream_id": "   "},
                  {"_status": 500, "error": "boom"}]
        for index, shape in enumerate(shapes):
            task = self._wake(f"pi-shape-{index}", origin=self.origin)
            self.turns.clear()
            self.routes.start_session_turn = (
                lambda sid, message, *, source="process_wakeup", _s=shape: (
                    self.turns.append((sid, message, {})) or _s))
            self.worker.run_once(now=self.clock())
            row = self.registry.get_task(task)
            self.assertEqual(row["wake_state"], "uncertain", f"shape={shape!r}")
            self.assertIsNotNone(row["wake_last_error"], f"shape={shape!r}")
            calls_after_first = len(self.turns)
            self.worker.run_once(now=self.clock())
            self.assertEqual(len(self.turns), calls_after_first,
                             f"uncertain rows must never be re-admitted (shape={shape!r})")
            self.assertEqual(self.registry.get_task(task)["wake_state"], "uncertain",
                             f"shape={shape!r}")

    def test_status_and_stream_matrix_outcomes(self):
        cases = [
            ({"stream_id": "stream-ok"}, "accepted"),
            ({"_status": 200, "stream_id": "stream-ok"}, "accepted"),
            ({"_status": 409}, "pending"),
            ({"_status": 404}, "pending"),
        ]
        for index, (shape, expected) in enumerate(cases):
            task = self._wake(f"pi-matrix-{index}", origin=self.origin)
            self.turns.clear()
            self.routes.start_session_turn = (
                lambda sid, message, *, source="process_wakeup", _s=shape: (
                    self.turns.append((sid, message, {})) or _s))
            self.worker.run_once(now=self.clock())
            row = self.registry.get_task(task)
            self.assertEqual(row["wake_state"], expected, f"shape={shape!r}")
            if expected == "accepted":
                self.assertEqual(row["wake_attempts"], 1)
                calls_after_first = len(self.turns)
                self.worker.run_once(now=self.clock())
                self.assertEqual(len(self.turns), calls_after_first,
                                 f"accepted rows must never be re-admitted (shape={shape!r})")
                self.assertEqual(self.registry.get_task(task)["wake_state"], "accepted")
            else:
                self.assertEqual(row["wake_attempts"], 0,
                                 f"deferral must not spend the wake budget (shape={shape!r})")
