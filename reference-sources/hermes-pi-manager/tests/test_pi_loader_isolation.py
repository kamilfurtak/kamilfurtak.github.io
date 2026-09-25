"""Managed-context isolation against the REAL pi loader — opt-in gate.

Runs the installed ``pi`` binary against a synthetic agent dir
(``PI_CODING_AGENT_DIR``), a synthetic project tree and a local fake
OpenAI-compatible endpoint that captures the exact request payload. Proves:

  * the CONTROL (no flags) loads the planted markers (the test can detect
    the defect), and
  * the ISOLATED arg set (MANAGED_CONTEXT_FLAGS + ``--system-prompt ""`` +
    one ``--append-system-prompt`` policy) loads none of them — context
    files, SYSTEM.md/APPEND_SYSTEM.md, skills, prompt templates and
    extensions — while keeping the default base preamble, the policy once,
    and the RPC contract (get_state / get_entries / agent_settled).

Needs ``pi`` on PATH (or PI_BIN). Never touches the real ``~/.pi``, the
network or a paid provider.

Run: scripts/run-tests.py --suite pi
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from rpc_transport import MANAGED_CONTEXT_FLAGS  # noqa: E402

PI_BIN = os.environ.get("PI_BIN") or shutil.which("pi")
REQUIRE_PI = os.environ.get("PI_REQUIRE_PI") == "1"

if REQUIRE_PI and not PI_BIN:
    # An unexecuted gate must fail loudly, never count as integration proof.
    raise AssertionError("pi binary is required for the isolation suite (PI_REQUIRE_PI=1) "
                         "but was not found on PATH or via PI_BIN")


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


MARKERS = {
    "global_agents": "MARKER_GLOBAL_AGENTS_1f2a",
    "project_agents": "MARKER_PROJECT_AGENTS_3b4c",
    "ancestor_claude": "MARKER_ANCESTOR_CLAUDE_5d6e",
    "wt_claude": "MARKER_WORKTREE_CLAUDE_2e3f",
    "repo_agents": "MARKER_REPO_AGENTS_4a5b",
    "global_system": "MARKER_GLOBAL_SYSTEM_MD_7f80",
    "project_system": "MARKER_PROJECT_SYSTEM_MD_9a1b",
    "global_append": "MARKER_GLOBAL_APPEND_MD_2c3d",
    "project_append": "MARKER_PROJECT_APPEND_MD_3d4e",
    "skill_desc": "MARKER_SKILL_DESC_4e5f",
    "template_body": "MARKER_TEMPLATE_BODY_6a7b",
    "policy": "MARKER_MANAGED_POLICY_8c9d",
}

SSE = (
    'data: {"id":"chatcmpl-iso","object":"chat.completion.chunk","created":0,'
    '"model":"fake-model","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"OK"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-iso","object":"chat.completion.chunk","created":0,'
    '"model":"fake-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


class _CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            record = {"path": self.path, "body": json.loads(raw.decode("utf-8"))}
        except ValueError:
            record = {"path": self.path, "body": raw.decode("utf-8", "replace")}
        self.server.captured.append(record)  # type: ignore[attr-defined]
        payload = SSE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


EXTENSION_MARKER = """// @ts-nocheck
// Isolation probe extension: a load side effect plus one registered tool.
import { writeFileSync } from "node:fs";
try {
  const target = process.env.PI_TEST_EXTENSION_MARKER;
  if (target) writeFileSync(target, "fired");
} catch (e) {}
export default function (pi) {
  pi.registerTool({
    name: "iso_marker_tool",
    label: "ISO Marker Tool",
    description: "Test-only extension tool (isolation probe " + "MARKER_EXT_TOOL_1a2b" + ").",
    parameters: { type: "object", properties: {} },
    async execute() { return { content: [{ type: "text", text: "marker" }] }; },
  });
}
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def payload_text(record) -> str:
    body = record["body"]
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False)


class Synthetic:
    """A throwaway PI_CODING_AGENT_DIR + project tree with planted markers."""

    def __init__(self, tmp: Path, port: int):
        self.tmp = tmp
        self.agent = tmp / "agent"
        self.project = tmp / "project"
        self.sessions = tmp / "sessions"
        self.ext_marker = tmp / "ext-fired"
        self.policy = tmp / "policy.md"
        self.session_file = self.sessions / "iso-session.jsonl"

        write(self.agent / "AGENTS.md", f"{MARKERS['global_agents']}\n")
        write(self.agent / "SYSTEM.md", f"{MARKERS['global_system']}\n")
        write(self.agent / "APPEND_SYSTEM.md", f"{MARKERS['global_append']}\n")
        write(self.agent / "skills" / "iso-marker" / "SKILL.md",
              "---\nname: iso-marker-skill\n"
              f"description: {MARKERS['skill_desc']}\n---\nBody of the marker skill.\n")
        write(self.agent / "extensions" / "iso-marker.ts", EXTENSION_MARKER)
        write(self.agent / "prompts" / "iso-probe.md",
              f"{MARKERS['template_body']}\n")
        write(self.agent / "settings.json", json.dumps({
            "defaultProvider": "fake", "defaultModel": "fake-model",
        }))
        write(self.agent / "models.json", json.dumps({"providers": {"fake": {
            "baseUrl": f"http://127.0.0.1:{port}/v1",
            "api": "openai-completions",
            "apiKey": "test-key",
            "models": [{
                "id": "fake-model", "name": "Fake Model", "reasoning": False,
                "input": ["text"], "contextWindow": 8192, "maxTokens": 1024,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }],
        }}}))

        # Project tree: parent carries a CLAUDE.md variant, the trusted project
        # carries AGENTS.md + .pi/SYSTEM.md + .pi/APPEND_SYSTEM.md + .pi/skills.
        self.parent = tmp
        write(self.parent / "CLAUDE.md", f"{MARKERS['ancestor_claude']}\n")
        write(self.project / "AGENTS.md", f"{MARKERS['project_agents']}\n")
        write(self.project / ".pi" / "SYSTEM.md", f"{MARKERS['project_system']}\n")
        write(self.project / ".pi" / "APPEND_SYSTEM.md", f"{MARKERS['project_append']}\n")
        write(self.project / ".pi" / "skills" / "proj-marker" / "SKILL.md",
              "---\nname: proj-marker-skill\ndescription: project marker skill\n---\nBody.\n")
        write(self.agent / "trust.json", json.dumps({str(self.project): True}))
        write(self.policy, f"{MARKERS['policy']}\n")

    def env(self) -> dict:
        env = dict(os.environ)
        env["PI_CODING_AGENT_DIR"] = str(self.agent)
        env["PI_CODING_AGENT_SESSION_DIR"] = str(self.sessions)
        env["PI_TEST_EXTENSION_MARKER"] = str(self.ext_marker)
        return env


class LoaderIsolationTests(unittest.TestCase):
    """Real subprocess runs; each asserts on the captured request payload."""

    @classmethod
    def setUpClass(cls):
        if not PI_BIN:
            raise unittest.SkipTest("pi binary not available")
        cls.tmp = Path(tempfile.mkdtemp(prefix="pi-loader-isolation-"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        cls.server.captured = []  # type: ignore[attr-defined]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.syn = Synthetic(cls.tmp, cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def run_pi(self, args, timeout=90, cwd=None):
        self.server.captured.clear()  # type: ignore[attr-defined]
        self.syn.ext_marker.unlink(missing_ok=True)
        proc = subprocess.run([PI_BIN, *args], cwd=str(cwd or self.syn.project),
                              env=self.syn.env(), capture_output=True, text=True,
                              timeout=timeout)
        self.assertEqual(proc.returncode, 0,
                         f"pi failed rc={proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertTrue(self.server.captured, "no provider request was captured")  # type: ignore[attr-defined]
        return self.server.captured[0]  # type: ignore[attr-defined]

    def isolated_args(self, prompt, session=None):
        args = [*MANAGED_CONTEXT_FLAGS, "--system-prompt", ""]
        args += ["--append-system-prompt", str(self.syn.policy)]
        if session:
            args += ["--session", session]
        args += ["--provider", "fake", "--model", "fake-model", "-p", prompt]
        return args

    def control_args(self, prompt):
        return ["--provider", "fake", "--model", "fake-model", "-p", prompt]

    def assert_markers(self, text, expected, present):
        for name in expected:
            marker = MARKERS[name]
            if present:
                self.assertIn(marker, text,
                              f"{name} marker missing from the CONTROL payload — the test "
                              "cannot detect this resource type")
            else:
                self.assertNotIn(marker, text,
                                 f"{name} marker leaked into the managed-context payload")

    # -- tests ------------------------------------------------------------

    def test_01_control_loads_every_marker(self):
        record = self.run_pi(self.control_args("reply OK"))
        text = payload_text(record)
        # Context files (global + ancestor + project), global SYSTEM/APPEND and
        # the skills listing all load in the headless default (project trust
        # is opt-in per run; the trusted-project variants are proven by
        # test_01b with --approve).
        self.assert_markers(text, ["global_agents", "project_agents", "ancestor_claude",
                                   "global_system", "global_append", "skill_desc"],
                            present=True)
        self.assert_markers(text, ["project_system", "project_append"], present=False)
        tools = record["body"].get("tools") if isinstance(record["body"], dict) else None
        tool_names = [t.get("function", {}).get("name") or t.get("name") for t in (tools or [])]
        self.assertIn("iso_marker_tool", tool_names,
                      "extension-registered tool missing from the CONTROL tools schema")
        self.assertTrue(self.syn.ext_marker.exists(),
                        "extension load side effect missing in the CONTROL run")

    def test_01b_trusted_project_system_and_append_load_with_approve(self):
        record = self.run_pi(["--approve", *self.control_args("reply OK")])
        text = payload_text(record)
        # With trust granted, project-level SYSTEM.md/APPEND_SYSTEM.md win over
        # the global files (single source per level) — proof the project-level
        # markers are loadable at all, which the isolation test then blocks.
        self.assert_markers(text, ["project_system", "project_append"], present=True)

    def test_02_isolation_blocks_every_marker(self):
        record = self.run_pi(self.isolated_args("reply OK"))
        text = payload_text(record)
        self.assert_markers(text, ["global_agents", "project_agents", "ancestor_claude",
                                   "global_system", "project_system", "global_append",
                                   "project_append", "skill_desc", "template_body"],
                            present=False)
        self.assertFalse(self.syn.ext_marker.exists(),
                         "an extension executed under managed-context isolation")
        tools = record["body"].get("tools") if isinstance(record["body"], dict) else None
        tool_names = [t.get("function", {}).get("name") or t.get("name") for t in (tools or [])]
        self.assertNotIn("iso_marker_tool", tool_names)
        self.assertNotIn("subagent", tool_names)
        # Default base preamble survives the empty --system-prompt sentinel,
        # and the policy appears exactly once.
        self.assertIn("expert coding assistant operating inside pi", text)
        self.assertEqual(text.count(MARKERS["policy"]), 1,
                         "policy must be applied exactly once")
        # The system sections in the request keep pi's default rules, and no
        # project-context/skills sections are present.
        body = record["body"]
        messages = body.get("messages", []) if isinstance(body, dict) else []
        system_msgs = [m for m in messages if m.get("role") == "system"]
        self.assertTrue(system_msgs, "no system message in the isolated payload")

    def test_02b_isolation_holds_even_with_project_trust(self):
        # --approve grants project trust for the run; isolation must hold
        # anyway (the flags and the empty --system-prompt sentinel act
        # regardless of trust).
        record = self.run_pi(["--approve", *self.isolated_args("reply OK")])
        text = payload_text(record)
        self.assert_markers(text, ["project_system", "project_append", "global_system",
                                   "global_append", "project_agents"], present=False)
        self.assertFalse(self.syn.ext_marker.exists())

    def test_03_templates_expand_only_without_isolation(self):
        control = self.run_pi(self.control_args("/iso-probe"))
        self.assertIn(MARKERS["template_body"], payload_text(control))
        isolated = self.run_pi(self.isolated_args("/iso-probe"))
        self.assertNotIn(MARKERS["template_body"], payload_text(isolated))

    def test_04_second_turn_stays_isolated(self):
        first = self.run_pi(self.isolated_args("first turn", session=str(self.syn.session_file)))
        self.assertNotIn(MARKERS["global_agents"], payload_text(first))
        second = self.run_pi(self.isolated_args("second turn", session=str(self.syn.session_file)))
        text = payload_text(second)
        self.assert_markers(text, ["global_agents", "global_system", "global_append",
                                   "skill_desc", "template_body"], present=False)
        self.assertEqual(text.count(MARKERS["policy"]), 1)

    def test_05_rpc_contract_under_isolation(self):
        proc = subprocess.Popen(
            [PI_BIN, "--mode", "rpc", *MANAGED_CONTEXT_FLAGS, "--system-prompt", "",
             "--append-system-prompt", str(self.syn.policy),
             "--provider", "fake", "--model", "fake-model",
             "--session", str(self.syn.session_file)],
            cwd=str(self.syn.project), env=self.syn.env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        events = []
        responses = {}
        lock = threading.Lock()

        def reader():
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                with lock:
                    if obj.get("type") == "response":
                        responses[obj.get("command")] = obj
                    else:
                        events.append(obj)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            def send(obj):
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()

            send({"type": "get_state", "id": "1"})
            self.assertTrue(wait_until(lambda: "get_state" in responses, 30.0),
                            "get_state never answered under isolation")
            state = responses["get_state"]
            self.assertTrue(state.get("success"))
            self.assertEqual(state["data"].get("sessionFile"), str(self.syn.session_file))

            send({"type": "get_entries", "id": "2"})
            self.assertTrue(wait_until(lambda: "get_entries" in responses, 30.0),
                            "get_entries never answered under isolation")
            entries = responses["get_entries"]
            self.assertTrue(entries.get("success"), entries)
            data = entries.get("data") or {}
            self.assertIsInstance(data.get("entries"), list, entries)

            send({"type": "prompt", "id": "3", "message": "reply OK"})
            self.assertTrue(wait_until(
                lambda: any(e.get("type") == "agent_settled" for e in events), 60.0),
                "agent_settled never arrived under isolation")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
                proc.wait(timeout=10)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass


    def test_06_nested_git_worktree_isolation(self):
        """A REAL nested git worktree: project-level markers load in the
        control run and are blocked under isolation, exactly like a plain
        checkout."""
        repo = self.tmp / "nested-repo"
        repo.mkdir()
        git = ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=test"]
        subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run([*git, "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        worktree = self.tmp / "nested-wt"
        subprocess.run([*git, "worktree", "add", "-q", "-b", "wt", str(worktree)],
                       cwd=repo, check=True)
        write(worktree / "CLAUDE.md", f"{MARKERS['wt_claude']}\n")
        write(repo / "AGENTS.md", f"{MARKERS['repo_agents']}\n")

        control = self.run_pi(self.control_args("reply OK"), cwd=worktree)
        self.assertIn(MARKERS["wt_claude"], payload_text(control),
                      "worktree-level CLAUDE.md must load in the CONTROL run")

        record = self.run_pi(self.isolated_args("reply OK"), cwd=worktree)
        text = payload_text(record)
        for name in ("wt_claude", "repo_agents", "ancestor_claude"):
            self.assertNotIn(MARKERS[name], text,
                             f"{name} leaked in a nested worktree under isolation")
        self.assertEqual(text.count(MARKERS["policy"]), 1)
        self.assertFalse(self.syn.ext_marker.exists())

    def test_07_manual_prompt_template_frontmatter(self):
        """Frontmatter detection (``startsWith('---')``): a correct template
        expands its BODY only; a metadata-first file leaks its frontmatter —
        which is how this test detects the defect shape. The REAL workspace
        manual template is checked whenever this checkout is present."""
        good = self.syn.agent / "prompts" / "iso-g2.md"
        good.write_text("---\ndescription: G2 description line\n---\n"
                        f"{MARKERS['template_body']}\n", encoding="utf-8")
        broken = self.syn.agent / "prompts" / "iso-b2.md"
        broken.write_text("<!-- metadata-first (broken) -->\n---\n"
                          "description: broken description\n---\nBODY_B2_LEAK_9f0a\n",
                          encoding="utf-8")

        control = self.run_pi(self.control_args("/iso-g2"))
        text = payload_text(control)
        self.assertIn(MARKERS["template_body"], text)
        self.assertNotIn("description: G2 description line", text,
                         "frontmatter must never expand into the prompt body")
        self.assertNotIn("<!--", text)

        plain = self.run_pi(self.control_args("reply OK"))
        self.assertNotIn(MARKERS["template_body"], payload_text(plain),
                         "discovery alone must never inject a template")

        broken_run = self.run_pi(self.control_args("/iso-b2"))
        broken_text = payload_text(broken_run)
        self.assertIn("BODY_B2_LEAK_9f0a", broken_text)
        self.assertIn("description: broken description", broken_text,
                      "the broken metadata-first shape must leak — that is the "
                      "defect this detector exists for")

        isolated = self.run_pi(self.isolated_args("/iso-g2"))
        self.assertNotIn(MARKERS["template_body"], payload_text(isolated))

        real = Path.home() / "hermes_workspace" / "infra" / "hermes" / "pi-prompts" / "pi-worker.md"
        if real.is_file():
            target = self.syn.agent / "prompts" / "pi-worker.md"
            target.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
            run = self.run_pi(self.control_args("/pi-worker"))
            real_text = payload_text(run)
            self.assertIn("You are the leaf worker in the Hermes", real_text)
            self.assertNotIn("description:", real_text,
                             "the real manual template's frontmatter must not expand")
            self.assertNotIn("Pi Manager default system prompt", real_text,
                             "the stale description text must be gone")


    def test_08_short_policy_text_is_not_reinterpreted_via_task_cwd(self):
        """R2: pi's resolvePromptInput treats an argument as a FILE whenever
        existsSync(input) succeeds, so a short raw-text policy is silently
        reinterpreted against the task cwd. A manager-owned snapshot path
        delivers exactly the validated bytes instead."""
        decoy = self.syn.project / "Be concise."
        decoy.write_text("DECOY_FILE_CONTENT_9e1f\n", encoding="utf-8")
        # Control (raw-text form): the SAME policy text resolves to the decoy
        # file — this is the ambiguity the manager must never expose.
        control = self.run_pi(["--provider", "fake", "--model", "fake-model",
                               "--append-system-prompt", "Be concise.",
                               "-p", "reply OK"])
        self.assertIn("DECOY_FILE_CONTENT_9e1f", payload_text(control),
                      "control must detect the text/path reinterpretation")
        # Fixed form: a private snapshot path carries the validated bytes.
        snapshot = self.tmp / "policy-snapshot-short.md"
        snapshot.write_text("Be concise.\n", encoding="utf-8")
        fixed = self.run_pi(["--provider", "fake", "--model", "fake-model",
                             "--append-system-prompt", str(snapshot),
                             "-p", "reply OK"])
        text = payload_text(fixed)
        self.assertIn("Be concise.", text)
        self.assertEqual(text.count("Be concise."), 1)
        self.assertNotIn("DECOY_FILE_CONTENT_9e1f", text)


if __name__ == "__main__":
    unittest.main()
