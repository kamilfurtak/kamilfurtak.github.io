"""Approved executor profiles; no credentials, model routing or automatic retry."""
from __future__ import annotations

import json
import re
from pathlib import Path


def validate_spec(value):
    if not isinstance(value, dict) or set(value) != {'provider', 'model', 'thinking'}:
        raise ValueError('executor requires exactly provider, model and thinking')
    for key in ('provider', 'model'):
        token = value[key]
        if (not isinstance(token, str) or not token or len(token) > 200
                or token.startswith('-') or any(c.isspace() or ord(c) < 32 for c in token)):
            raise ValueError(f'invalid executor {key}')
    if value['thinking'] not in ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('invalid executor thinking')
    return dict(value)


def validate_profiles(profiles, default):
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError('executor profiles must be a nonempty object')
    out = {}
    for name, spec in profiles.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name):
            raise ValueError('invalid executor profile name')
        out[name] = validate_spec(spec)
    if not isinstance(default, str) or default not in out:
        raise ValueError('default executor is not an approved profile')
    return out


def load_profile_config(home):
    """Absence preserves legacy defaults; malformed configuration fails closed."""
    path = Path(home) / 'policy' / 'pi-executors.json'
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        raise ValueError('invalid Pi executor configuration JSON') from None
    if not isinstance(value, dict) or set(value) != {'default', 'profiles'}:
        raise ValueError('Pi executor configuration requires default and profiles')
    profiles = validate_profiles(value['profiles'], value['default'])
    return {'executor_profiles': profiles, 'default_executor': value['default']}
