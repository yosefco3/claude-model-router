"""Tests for plugin/hooks/router_nudge.py (stdlib unittest, run via subprocess)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugin" / "hooks" / "router_nudge.py"


class NudgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="router-nudge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / "config"
        self.home = self.tmp / "home"
        self.config.mkdir()
        self.home.mkdir()
        self.state = self.config / "model-router" / "state"

    def run_hook(self, payload, *args, script=SCRIPT, env_extra=None, raw=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_ROUTER", "CLAUDE_CODE_REMOTE", "CLAUDE_CONFIG_DIR", "HOME")}
        env["CLAUDE_CONFIG_DIR"] = str(self.config)
        env["HOME"] = str(self.home)
        env.update(env_extra or {})
        stdin = raw if raw is not None else json.dumps(payload)
        return subprocess.run([sys.executable, str(script), *args], input=stdin,
                              capture_output=True, text=True, env=env, timeout=30)

    def event(self, name, sid="sess1", **extra):
        return {"hook_event_name": name, "session_id": sid, **extra}

    def tool(self, name="Read", sid="sess1", **extra):
        return self.event("PostToolUse", sid=sid, tool_name=name, **extra)

    def parse(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def assertSilent(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def counter(self, sid="sess1"):
        f = self.state / sid
        return f.read_text() if f.exists() else None

    def set_off_flag(self):
        (self.config / "model-router").mkdir(parents=True, exist_ok=True)
        (self.config / "model-router" / "OFF").write_text("")


class TestEvents(NudgeTestCase):
    def test_session_start_emits_policy(self):
        out = self.parse(self.run_hook(self.event("SessionStart")))
        self.assertEqual(list(out), ["hookSpecificOutput"])
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("Tripwires", hso["additionalContext"])
        self.assertIn("model-router:scout", hso["additionalContext"])

    def test_user_prompt_submit_emits_reminder_and_resets_counter(self):
        for _ in range(3):
            self.run_hook(self.tool())
        self.assertEqual(self.counter(), "3")
        out = self.parse(self.run_hook(self.event("UserPromptSubmit")))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("Model router: ON", hso["additionalContext"])
        self.assertEqual(self.counter(), "0")

    def test_post_tool_use_nudges_at_6_and_16_only(self):
        nudged = []
        for n in range(1, 27):
            proc = self.run_hook(self.tool())
            if proc.stdout:
                out = self.parse(proc)["hookSpecificOutput"]
                self.assertEqual(out["hookEventName"], "PostToolUse")
                self.assertIn(f"made {n} read/search/shell/web tool calls in a row", out["additionalContext"])
                self.assertIn("last message or the last delegation", out["additionalContext"])
                nudged.append(n)
        self.assertEqual(nudged, [6, 16, 26])

    def test_first_two_nudges_are_6_and_16(self):
        nudged = [n for n in range(1, 20) if self.run_hook(self.tool()).stdout]
        self.assertEqual(nudged, [6, 16])

    def test_delegation_tool_resets_counter_silently(self):
        for _ in range(4):
            self.run_hook(self.tool())
        for name in ("Agent", "Task"):
            self.run_hook(self.tool())
            proc = self.run_hook(self.tool(name))
            self.assertSilent(proc)
            self.assertEqual(self.counter(), "0")

    def test_sessions_are_independent_and_sid_is_sanitized(self):
        self.run_hook(self.tool(sid="a"))
        self.run_hook(self.tool(sid="a"))
        self.run_hook(self.tool(sid="b"))
        self.assertEqual(self.counter("a"), "2")
        self.assertEqual(self.counter("b"), "1")
        self.run_hook(self.tool(sid="../../evil"))
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["a", "b", "evil"])

    def test_user_prompt_reminder_survives_state_failure(self):
        (self.config / "model-router").mkdir(parents=True)
        self.state.write_text("a file where the state dir should be")  # mkdir/write must fail
        out = self.parse(self.run_hook(self.event("UserPromptSubmit")))
        self.assertIn("Model router: ON", out["hookSpecificOutput"]["additionalContext"])
        self.assertSilent(self.run_hook(self.tool()))  # PostToolUse just stays quiet, exit 0

    def test_old_state_is_pruned_on_user_prompt(self):
        self.run_hook(self.event("UserPromptSubmit", sid="fresh"))
        old = self.state / "old"
        old.write_text("3")
        past = old.stat().st_mtime - 8 * 86400
        os.utime(old, (past, past))
        self.run_hook(self.event("UserPromptSubmit", sid="fresh"))
        self.assertFalse(old.exists())
        self.assertTrue((self.state / "fresh").exists())


class TestSilence(NudgeTestCase):
    def test_agent_id_is_silent_and_counter_unchanged(self):
        for _ in range(2):
            self.run_hook(self.tool())
        self.assertEqual(self.counter(), "2")
        for payload in (self.tool(agent_id="abc123"),
                        self.event("UserPromptSubmit", agent_id="abc123"),
                        self.event("SessionStart", agent_id="abc123")):
            self.assertSilent(self.run_hook(payload))
        self.assertEqual(self.counter(), "2")

    def test_off_file_is_silent_on_all_events(self):
        self.set_off_flag()
        for payload in (self.event("SessionStart"), self.event("UserPromptSubmit"), self.tool()):
            self.assertSilent(self.run_hook(payload))
        self.assertFalse(self.state.exists())  # no state change either

    def test_off_file_removed_turns_it_back_on(self):
        self.set_off_flag()
        self.assertSilent(self.run_hook(self.event("UserPromptSubmit")))
        (self.config / "model-router" / "OFF").unlink()
        self.assertIn("Model router: ON", self.parse(self.run_hook(self.event("UserPromptSubmit")))
                      ["hookSpecificOutput"]["additionalContext"])

    def test_env_off_values_are_silent(self):
        for val in ("off", "OFF", "0", "false", "False"):
            with self.subTest(val=val):
                for payload in (self.event("SessionStart"), self.event("UserPromptSubmit"), self.tool()):
                    self.assertSilent(self.run_hook(payload, env_extra={"CLAUDE_ROUTER": val}))

    def test_env_other_values_do_not_disable(self):
        proc = self.run_hook(self.event("SessionStart"), env_extra={"CLAUDE_ROUTER": "on"})
        self.assertIn("Tripwires", self.parse(proc)["hookSpecificOutput"]["additionalContext"])

    def test_invalid_stdin_is_silent_exit_0(self):
        for raw in ("", "not json", "[1, 2]", "null"):
            with self.subTest(raw=raw):
                self.assertSilent(self.run_hook(None, raw=raw))

    def test_unknown_event_is_silent(self):
        self.assertSilent(self.run_hook(self.event("Stop")))


class TestRepoMode(NudgeTestCase):
    def test_repo_flag_without_remote_is_silent(self):
        for payload in (self.event("SessionStart"), self.event("UserPromptSubmit"), self.tool()):
            self.assertSilent(self.run_hook(payload, "--repo"))
        for val in ("", "0", "false"):
            self.assertSilent(self.run_hook(self.event("SessionStart"), "--repo",
                                            env_extra={"CLAUDE_CODE_REMOTE": val}))
        self.assertFalse(self.state.exists())

    def test_repo_flag_with_remote_emits_policy(self):
        for val in ("1", "true", "YES"):
            with self.subTest(val=val):
                proc = self.run_hook(self.event("SessionStart"), "--repo",
                                     env_extra={"CLAUDE_CODE_REMOTE": val})
                hso = self.parse(proc)["hookSpecificOutput"]
                self.assertEqual(hso["hookEventName"], "SessionStart")
                self.assertIn("Tripwires", hso["additionalContext"])

    def test_repo_flag_with_remote_still_honours_off(self):
        self.set_off_flag()
        self.assertSilent(self.run_hook(self.event("SessionStart"), "--repo",
                                        env_extra={"CLAUDE_CODE_REMOTE": "1"}))

    def test_without_repo_flag_remote_is_irrelevant(self):
        proc = self.run_hook(self.event("SessionStart"), env_extra={"CLAUDE_CODE_REMOTE": "0"})
        self.assertIn("Tripwires", self.parse(proc)["hookSpecificOutput"]["additionalContext"])


class TestPolicyLookup(NudgeTestCase):
    def test_repo_layout_finds_policy_next_to_script(self):
        d = self.tmp / "proj" / ".claude" / "model-router"
        d.mkdir(parents=True)
        shutil.copy(SCRIPT, d / "router_nudge.py")
        (d / "POLICY.md").write_text("# Tripwires from the repo copy\n", encoding="utf-8")
        proc = self.run_hook(self.event("SessionStart"), "--repo", script=d / "router_nudge.py",
                             env_extra={"CLAUDE_CODE_REMOTE": "1"})
        self.assertEqual(self.parse(proc)["hookSpecificOutput"]["additionalContext"],
                         "# Tripwires from the repo copy")

    def test_plugin_layout_finds_policy_in_parent(self):
        plug = self.tmp / "plug"
        (plug / "hooks").mkdir(parents=True)
        shutil.copy(SCRIPT, plug / "hooks" / "router_nudge.py")
        (plug / "POLICY.md").write_text("parent policy\n", encoding="utf-8")
        proc = self.run_hook(self.event("SessionStart"), script=plug / "hooks" / "router_nudge.py")
        self.assertEqual(self.parse(proc)["hookSpecificOutput"]["additionalContext"], "parent policy")

    def test_missing_policy_emits_nothing(self):
        d = self.tmp / "lonely"
        d.mkdir()
        shutil.copy(SCRIPT, d / "router_nudge.py")
        self.assertSilent(self.run_hook(self.event("SessionStart"), script=d / "router_nudge.py"))

    def test_shipped_policy_has_no_markers_and_mentions_plugin_names(self):
        text = (ROOT / "plugin" / "POLICY.md").read_text(encoding="utf-8")
        self.assertNotIn("model-router:start", text)
        self.assertNotIn("model-router:end", text)
        self.assertIn("`model-router:scout`", text)


if __name__ == "__main__":
    unittest.main()
