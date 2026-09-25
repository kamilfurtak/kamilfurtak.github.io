"""Compatibility adapter for an already-running Hermes WebUI, not Desktop.

Passive notices use the native session SSE channel (no model call). Terminal
wake admission is separate, through start_session_turn and its busy/CAS gate.
No imported host, HTTP endpoint, synthetic terminal process or gateway fallback.
"""
from __future__ import annotations

import os
import sys
from typing import Any


class UncertainDelivery(RuntimeError):
    """Native admission may have started a turn; never replay blindly."""


def is_webui(origin: dict) -> bool:
    return origin.get('platform') == 'webui' or origin.get('source') == 'webui'


def _target(origin: dict):
    routes = sys.modules.get('api.routes')
    profiles = sys.modules.get('api.profiles')
    if not is_webui(origin) or routes is None or profiles is None:
        return None
    sid = origin.get('ui_session_id')
    home = origin.get('hermes_home')
    if not isinstance(sid, str) or not sid or not home:
        return None
    if any(origin.get(k) not in (None, '', sid) for k in ('session_key', 'session_id')):
        return None
    try:
        session = routes.get_session(sid)
        if session is None or session.session_id != sid:
            return None
        target_home = profiles._resolve_profile_home_for_name(session.profile or '')
        if os.path.realpath(target_home) != os.path.realpath(home):
            return None
        if not callable(getattr(routes, 'start_session_turn', None)):
            return None
        return routes, sid, session
    except Exception:
        return None


def available(origin: dict, *, idle: bool = False) -> bool:
    target = _target(origin)
    if target is None:
        return False
    routes, sid, session = target
    try:
        if idle:
            return not routes._active_stream_blocks_chat_start(session, session.active_stream_id)
        background = sys.modules.get('api.background_process')
        return bool(background and background.get_session_channel(sid).subscriber_count())
    except Exception:
        return False


def emit_status(origin: dict, message: str, notification_id: str, *, task_id: str = '') -> dict:
    target = _target(origin)
    background = sys.modules.get('api.background_process')
    if target is None or background is None:
        raise RuntimeError('WebUI session owner is unavailable')
    channel = background.get_session_channel(target[1])
    # Native event ids are deduplicated by the browser. A closed tab leaves
    # passive delivery pending; wake admission never requires a subscriber.
    delivered = channel.emit('bg_task_complete', {
        'session_id': target[1], 'task_id': task_id,
        'event_id': notification_id, 'summary': str(message)[:500],
    })
    if not delivered:
        raise RuntimeError('WebUI notice has no live subscriber')
    return {'success': True}


def deliver_wake(origin: dict, message: str, continuation_id: str, ctx: Any) -> str:
    target = _target(origin)
    if target is None:
        return 'unavailable'
    # Check the saved target profile's grant even on the worker thread.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(origin['hermes_home']))
    try:
        allowed = getattr(ctx, '_gateway_injection_allowed', None)
        if not callable(allowed) or allowed() is not True:
            return 'denied'
    finally:
        reset_hermes_home_override(token)
    routes, sid, _session = target
    if not available(origin, idle=True):
        return 'busy'
    try:
        result = routes.start_session_turn(
            sid, message, source='process_wakeup')
    except Exception as exc:
        raise UncertainDelivery('native WebUI admission raised: %s' % exc) from exc
    # Native admission is atomic against human turns. Never acquire its
    # non-reentrant session lock here, or bypass its active-stream guard.
    # Shape-guard EVERYTHING before classification: a non-dict result or a
    # non-string stream_id must land on UncertainDelivery, never on an
    # AttributeError (and never on a blind re-admission).
    if isinstance(result, dict):
        status = result.get('_status')
        stream_id = result.get('stream_id')
    else:
        status = None
        stream_id = None
    if status == 409:
        return 'busy'
    if status == 404:
        return 'unavailable'
    # Admitted-run evidence is a non-empty STRING stream id. The native
    # direct-mode start response (api/routes.start_session_turn ->
    # _start_chat_stream_for_session) carries ``stream_id`` and NO ``_status``
    # key at all; only error paths and the journal-adapter wrapper add
    # ``_status``. Observed live 2026-09-20: a successful direct-mode admission
    # (no _status) was misclassified as uncertain while the wake turn actually
    # started and completed.
    if (isinstance(stream_id, str) and stream_id.strip()
            and (status is None or status == 200)):
        return 'accepted'
    raise UncertainDelivery('native WebUI admission returned no definite outcome')
