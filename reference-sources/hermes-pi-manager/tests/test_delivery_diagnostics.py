"""Issue #7: plugin-local notice routing guard + pi_status delivery diagnostics.

Part A regression (observed live on the deployed revision): a WebUI terminal
notice (``term:pi-90b3525be3bf:verifier``) ended up on the generic messaging
rail and failed with ``Unknown or unregistered plugin platform: webui``. The
generic rail can never reach a plugin-local session; the row must either be
claimed by its own host adapter or stay visibly unclaimed — never fall
through to host messaging.

Part B: one ``pi_status`` read distinguishes execution/verification from
NOTICE delivery and CONTINUATION state, is read-only, bounded, scrubbed, and
reports legacy rows as ``unknown`` rather than as success. ``accepted`` is
never presented as a finished answer.
"""
from __future__ import annotations

import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fakes import FakePiProcess
from test_terminal_wake import _OutboxManagerCase
from outbox import OutboxWorker

WEBUI_ORIGIN = {'platform': 'webui', 'chat_id': 'web-a', 'session_key': 'web-a',
                'session_id': 'web-a', 'ui_session_id': 'web-a',
                'hermes_home': '/test/hermes'}


class NoticeRoutingGuardTests(_OutboxManagerCase):
    """The generic messaging rail must be unreachable for webui/tui/cli rows."""

    def _notice(self, task_id: str, origin=WEBUI_ORIGIN) -> str:
        self.registry.create_task(
            task_id=task_id, execution_state='SETTLED', verification_state='PASS',
            origin=json.dumps(origin),
        )
        return self.outbox.enqueue(task_id, 'verifier', 'Pi zakończył zadanie.')

    def test_a_webui_row_without_a_resolvable_origin_is_not_claimed_by_anyone(self):
        # A row whose task carries NO origin cannot be resolved to any host
        # adapter. Base code claimed it anyway and attempted host messaging
        # (producing 'Unknown or unregistered plugin platform: webui').
        nid = self._notice('pi-broken')
        # The row keeps its prod shape (platform/target stamped at enqueue),
        # but the task origin goes missing/becomes unresolvable afterwards.
        self.registry.update_task('pi-broken', origin=None)
        delivered = []
        worker = OutboxWorker(
            self.outbox,
            deliver=lambda *a, **k: delivered.append((a, k)) or {
                'error': 'Unknown or unregistered plugin platform: webui'},
        )
        claimed = worker.run_once(now=self.clock())
        self.assertEqual(claimed, 0, 'no process may claim an unresolvable row')
        self.assertEqual(delivered, [], 'generic messaging must never be attempted')
        row = [n for n in self.registry.list_notifications(task_id='pi-broken')][0]
        self.assertEqual(row['notification_id'], nid)
        self.assertEqual(row['status'], 'pending', 'left visible for diagnostics')

    def test_direct_fallthrough_for_a_plugin_local_row_fails_closed(self):
        # Even if a row is already leased (older generation/race), the
        # delivery step must refuse the generic rail for plugin-local targets.
        nid = self._notice('pi-direct')
        self.registry.update_task('pi-direct', origin=None)
        leased = self.outbox.claim('worker-1', 30.0, now=self.clock())
        self.assertEqual(len(leased), 1)
        delivered = []
        worker = OutboxWorker(
            self.outbox,
            deliver=lambda *a, **k: delivered.append((a, k)) or {'ok': True},
        )
        worker._deliver_one(leased[0], self.clock())
        self.assertEqual(delivered, [], 'a leased plugin-local row must not use host messaging')
        row = [n for n in self.registry.list_notifications(task_id='pi-direct')][0]
        self.assertEqual(row['notification_id'], nid)
        self.assertEqual(row['status'], 'failed')
        self.assertIn('refusing generic messaging', row['last_error'])
        self.assertNotIn('unknown platform', row['last_error'].lower())

    def test_a_healthy_webui_row_still_delivers_through_its_own_adapter(self):
        frames = []
        session = SimpleNamespace(session_id='web-a', profile=None, active_stream_id=None)
        routes = ModuleType('api.routes')
        routes.get_session = lambda sid: session if sid == 'web-a' else None
        routes.start_session_turn = lambda *a, **k: {'stream_id': 's1'}
        routes._active_stream_blocks_chat_start = lambda *a, **k: False
        profiles = ModuleType('api.profiles')
        profiles._resolve_profile_home_for_name = lambda name: '/test/hermes'
        channel = SimpleNamespace(subscriber_count=lambda: 1,
                                  emit=lambda event, data: frames.append((event, data)) or 1)
        background = ModuleType('api.background_process')
        background.get_session_channel = lambda sid: channel
        with patch.dict('sys.modules', {'api.routes': routes, 'api.profiles': profiles,
                                        'api.background_process': background}):
            nid = self._notice('pi-happy', origin=WEBUI_ORIGIN)

            def refuse(args):
                raise AssertionError('generic messaging must not be used for webui rows')
            worker = OutboxWorker(self.outbox, deliver=refuse)
            claimed = worker.run_once(now=self.clock())
        self.assertEqual(claimed, 1)
        self.assertEqual([event for event, _ in frames], ['bg_task_complete'])
        self.assertEqual(frames[0][1]['task_id'], 'pi-happy')
        row = [n for n in self.registry.list_notifications(task_id='pi-happy')][0]
        self.assertEqual(row['notification_id'], nid)
        self.assertEqual(row['status'], 'sent')


class PiStatusDeliveryDiagnosticsTests(_OutboxManagerCase):
    """One read must separate notice delivery from continuation state."""

    def _manager(self):
        return self.make_manager(FakePiProcess())

    def _diag(self, task_id: str):
        return self._manager().status(task_id)['delivery']

    def test_notice_sent_and_wake_accepted_are_reported_separately(self):
        self.registry.create_task(
            task_id='pi-diag', execution_state='SETTLED', verification_state='PASS',
            origin=json.dumps(WEBUI_ORIGIN), continuation_enabled=1)
        self.registry.update_task('pi-diag', wake_state='accepted', wake_attempts=1,
                                  wake_accepted_at=111.0)
        nid = self.outbox.enqueue('pi-diag', 'verifier', 'Pi zakończył zadanie.')
        leased = self.outbox.claim('w1', 30.0, now=self.clock())  # attempts increments on claim
        self.assertEqual(len(leased), 1)
        self.registry.set_notification_sent(nid, self.clock())
        diag = self._diag('pi-diag')
        self.assertEqual(diag['notice']['status'], 'sent')
        self.assertEqual(diag['notice']['attempts'], 1)
        self.assertEqual(diag['wake']['state'], 'accepted')
        self.assertEqual(diag['wake']['attempts'], 1)
        # accepted is NOT a finished answer: the separate signal is absent.
        self.assertIsNone(diag['wake']['turn_finished_at'])

    def test_legacy_rows_without_data_are_unknown_never_success(self):
        self.registry.create_task(
            task_id='pi-legacy', execution_state='SETTLED', verification_state='NOT_RUN',
            continuation_enabled=1)
        diag = self._diag('pi-legacy')
        self.assertEqual(diag['notice']['status'], 'unknown')
        self.assertEqual(diag['wake']['state'], 'unknown')

    def test_a_running_task_reports_not_yet_rather_than_unknown(self):
        self.registry.create_task(
            task_id='pi-run', execution_state='RUNNING', verification_state='NOT_RUN',
            continuation_enabled=1)
        diag = self._diag('pi-run')
        self.assertEqual(diag['notice']['status'], 'not_yet')

    def test_disabled_continuation_is_explicit(self):
        self.registry.create_task(
            task_id='pi-off', execution_state='SETTLED', verification_state='PASS',
            continuation_enabled=0)
        diag = self._diag('pi-off')
        self.assertEqual(diag['wake']['state'], 'disabled')

    def test_errors_are_scrubbed_and_bounded(self):
        self.registry.create_task(
            task_id='pi-scrub', execution_state='SETTLED', verification_state='PASS',
            continuation_enabled=1)
        self.registry.update_task(
            'pi-scrub', wake_state='exhausted',
            wake_last_error='token=SECRETVALUE123 ' + 'x' * 4000)
        diag = self._diag('pi-scrub')
        blob = json.dumps(diag)
        self.assertNotIn('SECRETVALUE123', blob)
        self.assertLessEqual(len(blob), 900)

    def test_the_read_creates_nothing(self):
        self.registry.create_task(
            task_id='pi-ro', execution_state='SETTLED', verification_state='PASS',
            continuation_enabled=1)
        manager = self._manager()
        before = (len(self.registry.list_notifications()), len(self.registry.recent_events('pi-ro', limit=1000)))
        manager.status('pi-ro')
        manager.status('pi-ro')
        after = (len(self.registry.list_notifications()), len(self.registry.recent_events('pi-ro', limit=1000)))
        self.assertEqual(before, after)
        row = self.registry.get_task('pi-ro')
        self.assertIsNone(row.get('wake_requested_at'))


if __name__ == '__main__':
    unittest.main()
