"""Quiet dispatch, native post-turn display and subsequent human/terminal turns."""
import importlib.util
import io
import os
from pathlib import Path
import queue
import sys
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from test_pi_manager import PiManagerTestCase
from cli_startup import ACKNOWLEDGEMENT, QuietStart


class NativeRenderTests(unittest.TestCase):
    def test_installed_cli_renders_next_answer_and_keeps_interrupt_and_steer_queues(self):
        source = Path(os.environ.get('HERMES_AGENT_SOURCE', '~/.hermes/hermes-agent')).expanduser() / 'hermes_cli/cli_chat_turn_mixin.py'
        if not source.exists():
            self.skipTest('installed Hermes CLI required')
        # The installed module imports its own package (hermes_cli.*), so add
        # the package root the way the runtime does, then restore sys.path so
        # the 'cli' stub below cannot be shadowed by a real top-level module.
        package_root = str(source.parent.parent)
        added = package_root not in sys.path
        if added:
            sys.path.insert(0, package_root)
        try:
            spec = importlib.util.spec_from_file_location('native_cli_turn_contract', source)
            host = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(host)
        finally:
            if added:
                sys.path.remove(package_root)
        output = io.StringIO()
        from rich.console import Console
        from rich.text import Text
        console = Console(file=output, color_system=None, width=100)
        module = ModuleType('cli')
        for key in ('_DIM', '_RST', '_ACCENT'):
            setattr(module, key, '')
        module._cprint = console.print
        module._suspend_output_history = nullcontext
        module.ChatConsole = lambda: console
        module._maybe_remap_for_light_mode = lambda color: color
        module._post_stream_transform_output = lambda response, result: ''
        module._render_final_assistant_content = lambda text, **kw: Text(text)
        cli = host.CLIChatTurnMixin()
        cli.session_id = 'mine'
        cli.streaming_enabled = False
        cli.agent = SimpleNamespace(stream_delta_callback=None, _stream_callback=None,
                                    _interrupt_requested=False, clear_interrupt=Mock())
        cli._voice_tts = cli._voice_continuous = False
        cli._stream_started = cli._stream_box_opened = False
        cli._interrupt_queue = queue.Queue()
        cli._pending_input = queue.Queue()
        cli._flush_stream = Mock()
        cli._chat_print_reasoning_box = Mock()
        cli._ring_bell = Mock()
        cli._scrollback_box_width = lambda: 100
        cli.final_response_markdown = 'strip'
        thread = SimpleNamespace(is_alive=lambda: False)
        quiet = QuietStart(cli, lambda: True)
        quiet.attach()
        self.addCleanup(quiet.close)
        result = dict(final_response=ACKNOWLEDGEMENT, completed=True, pending_steer='follow-up')
        turn = SimpleNamespace(result=result, use_streaming_tts=False)
        with patch.dict(sys.modules, {'cli': module}):
            self.assertEqual(cli._chat_render_turn(turn, thread, None), '')
            self.assertNotIn(ACKNOWLEDGEMENT, output.getvalue())
            self.assertNotIn('Hermes', output.getvalue(), 'no empty response box')
            self.assertEqual(cli._pending_input.get_nowait(), 'follow-up')
            turn.result = dict(final_response='5 × 5 = 25', completed=True)
            self.assertEqual(cli._chat_render_turn(turn, thread, None), '5 × 5 = 25')
            self.assertIn('5 × 5 = 25', output.getvalue())
            turn.result = dict(final_response='Pi: weryfikacja PASS', completed=True)
            cli._chat_render_turn(turn, thread, 'human interrupt')
            self.assertIn('Pi: weryfikacja PASS', output.getvalue())
            self.assertEqual(cli._pending_input.get_nowait(), 'human interrupt')
        self.assertNotIn('_chat_render_turn', vars(cli), 'restore the host descriptor')
