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
               if k not in ("CLAUDE_ROUTER", "CLAUDE_CODE_REMOTE", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "HOME")}
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

    def popen_hook(self, payload, env_extra=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_ROUTER", "CLAUDE_CODE_REMOTE", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "HOME")}
        env.update(CLAUDE_CONFIG_DIR=str(self.config), HOME=str(self.home))
        env.update(env_extra or {})
        feed = self.tmp / "stdin.json"  # a file, not a pipe: the process can start reading at once
        feed.write_text(json.dumps(payload))
        with open(feed) as stdin:
            return subprocess.Popen([sys.executable, str(SCRIPT)], stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env)

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
        before = self.state.read_text()
        for _ in range(8):  # past the 6-call threshold, with no way to count: quiet, exit 0, state untouched
            self.assertSilent(self.run_hook(self.tool()))
        self.assertTrue(self.state.is_file())
        self.assertEqual(self.state.read_text(), before)

    def test_old_state_is_pruned_on_user_prompt(self):
        self.run_hook(self.event("UserPromptSubmit", sid="fresh"))
        old = self.state / "old"
        old.write_text("3")
        past = old.stat().st_mtime - 8 * 86400
        os.utime(old, (past, past))
        self.run_hook(self.event("UserPromptSubmit", sid="fresh"))
        self.assertFalse(old.exists())
        self.assertTrue((self.state / "fresh").exists())


class TestParallelCounter(NudgeTestCase):
    def test_parallel_post_tool_use_never_loses_a_count(self):
        for batch in range(20):
            procs = [self.popen_hook(self.tool(sid="par")) for _ in range(12)]
            results = [(proc.communicate(timeout=60), proc.returncode) for proc in procs]  # reap all before asserting
            for (_, err), code in results:
                self.assertEqual(code, 0, err)
            outs = [out for (out, _), _ in results]
            self.assertEqual(self.counter("par"), "12", f"batch {batch}")
            nudges = [json.loads(o) for o in outs if o]
            self.assertEqual(len(nudges), 1, f"batch {batch}: exactly the process that reached n == 6 nudges")
            self.assertIn("made 6 read/search/shell/web tool calls", nudges[0]["hookSpecificOutput"]["additionalContext"])
            self.assertSilent(self.run_hook(self.tool("Agent", sid="par")))  # a delegation resets the counter
            self.assertEqual(self.counter("par"), "0")

    def test_counter_file_is_plain_integer_text(self):
        for n in range(1, 4):
            self.run_hook(self.tool())
            self.assertEqual(self.counter(), str(n))
        (self.state / "sess1").write_text("garbage")
        self.run_hook(self.tool())
        self.assertEqual(self.counter(), "1")


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
        for _ in range(8):  # more than enough PostToolUse calls to cross the nudge threshold
            self.assertSilent(self.run_hook(self.tool()))
        self.assertFalse(self.state.exists())  # no state change either: no counter, no marker
        self.assertIsNone(self.counter())

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


class TestPluginYieldsToRepoCopy(NudgeTestCase):
    def make_project(self, with_script=True):
        proj = self.tmp / "proj"
        if with_script:
            d = proj / ".claude" / "model-router"
            d.mkdir(parents=True)
            shutil.copy(SCRIPT, d / "router_nudge.py")
            shutil.copy(ROOT / "plugin" / "POLICY.md", d / "POLICY.md")
        else:
            proj.mkdir()
        return proj

    def all_events(self, **extra):
        return (self.event("SessionStart", **extra), self.event("UserPromptSubmit", **extra), self.tool(**extra))

    def test_cloud_session_with_repo_copy_is_silent_via_project_dir_env(self):
        proj = self.make_project()
        for payload in self.all_events():
            self.assertSilent(self.run_hook(payload, env_extra={"CLAUDE_CODE_REMOTE": "true",
                                                                 "CLAUDE_PROJECT_DIR": str(proj)}))
        self.assertFalse(self.state.exists())

    def test_cloud_session_with_repo_copy_is_silent_via_cwd(self):
        proj = self.make_project()
        for payload in self.all_events(cwd=str(proj)):
            self.assertSilent(self.run_hook(payload, env_extra={"CLAUDE_CODE_REMOTE": "1"}))
        self.assertFalse(self.state.exists())

    def test_cloud_session_without_repo_copy_still_acts(self):
        proj = self.make_project(with_script=False)
        proc = self.run_hook(self.event("SessionStart", cwd=str(proj)), env_extra={"CLAUDE_CODE_REMOTE": "1"})
        self.assertIn("Tripwires", self.parse(proc)["hookSpecificOutput"]["additionalContext"])
        proc = self.run_hook(self.event("UserPromptSubmit"), env_extra={"CLAUDE_CODE_REMOTE": "1"})  # no project known
        self.assertIn("Model router: ON", self.parse(proc)["hookSpecificOutput"]["additionalContext"])

    def test_local_session_with_repo_copy_still_acts(self):
        proj = self.make_project()
        proc = self.run_hook(self.event("SessionStart"), env_extra={"CLAUDE_PROJECT_DIR": str(proj)})
        self.assertIn("Tripwires", self.parse(proc)["hookSpecificOutput"]["additionalContext"])

    def test_repo_copy_itself_is_not_affected(self):
        proj = self.make_project()
        proc = self.run_hook(self.event("SessionStart"), "--repo", script=proj / ".claude" / "model-router" / "router_nudge.py",
                             env_extra={"CLAUDE_CODE_REMOTE": "1", "CLAUDE_PROJECT_DIR": str(proj)})
        self.assertIn("Tripwires", self.parse(proc)["hookSpecificOutput"]["additionalContext"])

    def test_script_that_is_the_project_copy_does_not_yield_to_itself(self):
        proj = self.make_project()  # same file, started without --repo
        d = proj / ".claude" / "model-router"
        (d / "POLICY.md").write_text("# Tripwires from the project copy\n", encoding="utf-8")  # replaces the real one
        proc = self.run_hook(self.event("SessionStart"), script=d / "router_nudge.py",
                             env_extra={"CLAUDE_CODE_REMOTE": "1", "CLAUDE_PROJECT_DIR": str(proj)})
        self.assertEqual(self.parse(proc)["hookSpecificOutput"]["additionalContext"], "# Tripwires from the project copy")


class TestPolicyDelivery(NudgeTestCase):
    def ctx(self, proc):
        return self.parse(proc)["hookSpecificOutput"]["additionalContext"]

    def test_session_start_leaves_a_marker(self):
        self.run_hook(self.event("SessionStart", sid="m1"))
        self.assertTrue((self.state / "m1.policy").exists())
        text = self.ctx(self.run_hook(self.event("UserPromptSubmit", sid="m1")))
        self.assertNotIn("Tripwires", text)  # already delivered at session start
        self.assertIn("Model router: ON", text)

    def test_policy_arrives_on_first_prompt_when_session_start_was_skipped(self):
        self.set_off_flag()
        self.assertSilent(self.run_hook(self.event("SessionStart")))
        self.assertFalse((self.state / "sess1.policy").exists())
        (self.config / "model-router" / "OFF").unlink()  # `router on` mid-session

        first = self.ctx(self.run_hook(self.event("UserPromptSubmit")))
        self.assertIn("Tripwires", first)
        self.assertIn("Model router: ON", first)
        self.assertLess(first.index("Tripwires"), first.index("Model router: ON"))
        self.assertIn("\n\nModel router: ON", first)  # policy, blank line, reminder: one string
        self.assertTrue((self.state / "sess1.policy").exists())

        second = self.ctx(self.run_hook(self.event("UserPromptSubmit")))
        self.assertNotIn("Tripwires", second)
        self.assertIn("Model router: ON", second)

    def test_marker_is_per_session(self):
        self.run_hook(self.event("SessionStart", sid="a"))
        self.assertNotIn("Tripwires", self.ctx(self.run_hook(self.event("UserPromptSubmit", sid="a"))))
        self.assertIn("Tripwires", self.ctx(self.run_hook(self.event("UserPromptSubmit", sid="b"))))

    def test_policy_is_not_repeated_when_state_cannot_be_written(self):
        (self.config / "model-router").mkdir(parents=True)
        self.state.write_text("a file where the state dir should be")
        text = self.ctx(self.run_hook(self.event("UserPromptSubmit")))
        self.assertEqual(text.count("Model router: ON"), 1)
        self.assertNotIn("Tripwires", text)  # no marker can be created, so just the reminder

    def test_no_policy_file_gives_just_the_reminder_and_no_marker(self):
        d = self.tmp / "lonely"
        d.mkdir()
        shutil.copy(SCRIPT, d / "router_nudge.py")
        text = self.ctx(self.run_hook(self.event("UserPromptSubmit"), script=d / "router_nudge.py"))
        self.assertTrue(text.startswith("Model router: ON"))
        self.assertFalse((self.state / "sess1.policy").exists())

    def test_old_markers_are_pruned_and_live_ones_kept(self):
        self.run_hook(self.event("SessionStart", sid="live"))
        old = self.state / "gone.policy"
        old.write_text("1")
        past = old.stat().st_mtime - 8 * 86400
        os.utime(old, (past, past))
        live = self.state / "live.policy"
        os.utime(live, (past, past))  # a long-lived session's marker looks old too
        self.run_hook(self.event("UserPromptSubmit", sid="live"))
        self.assertFalse(old.exists())
        self.assertTrue(live.exists())  # touched by the prompt before pruning


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
