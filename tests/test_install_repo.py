"""Tests for router_kit.py install-repo (stdlib unittest, run via subprocess)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIT_SCRIPT = ROOT / "router_kit.py"
KIT = ROOT / "plugin"
AGENTS = ["builder", "mechanic", "reviewer", "scout", "test-writer"]
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse")
EXISTING_SETTINGS = {
    "permissions": {"allow": ["Bash(ls)"]},
    "hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "echo hi"}]}]},
}
COMMAND = 'python3 "$CLAUDE_PROJECT_DIR/.claude/model-router/router_nudge.py" --repo || true'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class InstallRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="router-kit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.tmp / "home").mkdir()
        (self.tmp / "config").mkdir()
        self.claude = self.repo / ".claude"

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "HOME")}
        env["HOME"] = str(self.tmp / "home")
        env["CLAUDE_CONFIG_DIR"] = str(self.tmp / "config")
        env.update(extra)
        return env

    def run_kit(self, *args, kit=KIT):
        argv = [sys.executable, str(KIT_SCRIPT), "install-repo", *map(str, args)]
        if kit is not None:
            argv += ["--kit", str(kit)]
        return subprocess.run(argv, capture_output=True, text=True, env=self.env(), timeout=30)

    def settings(self):
        return json.loads((self.claude / "settings.json").read_text(encoding="utf-8"))

    def write_settings(self, obj, **dump_kw):
        self.claude.mkdir(exist_ok=True)
        text = obj if isinstance(obj, str) else json.dumps(obj, **dump_kw)
        (self.claude / "settings.json").write_text(text, encoding="utf-8")

    def router_groups(self, event):
        return [g for g in self.settings()["hooks"][event]
                if any("model-router/router_nudge.py" in h["command"] for h in g["hooks"])]

    def tree(self, root=None):
        root = Path(root or self.repo)
        return sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    def assertInstalled(self):
        c = self.claude
        self.assertEqual(sorted(p.stem for p in (c / "agents").glob("*.md") if p.stem != "other"), AGENTS)
        for a in AGENTS:
            self.assertEqual((c / "agents" / f"{a}.md").read_bytes(), (KIT / "agents" / f"{a}.md").read_bytes())
        for name, src in (("router_nudge.py", KIT / "hooks" / "router_nudge.py"), ("POLICY.md", KIT / "POLICY.md")):
            self.assertEqual((c / "model-router" / name).read_bytes(), src.read_bytes())
        for event in EVENTS:
            groups = self.router_groups(event)
            self.assertEqual(len(groups), 1, event)
            self.assertEqual(groups[0]["hooks"], [{"type": "command", "command": COMMAND, "timeout": 10}])
        plugin_matcher = json.loads((KIT / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"][0]["matcher"]
        self.assertEqual(self.router_groups("PostToolUse")[0]["matcher"], plugin_matcher)
        self.assertNotIn("matcher", self.router_groups("SessionStart")[0])
        self.assertNotIn("matcher", self.router_groups("UserPromptSubmit")[0])
        manifest = json.loads((c / "model-router" / "manifest.json").read_text())
        self.assertEqual(manifest["agents"], {f"{a}.md": sha(KIT / "agents" / f"{a}.md") for a in AGENTS})
        leftovers = [p for p in self.tree() if p.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def copy_kit(self, name="kit"):
        dst = self.tmp / name
        shutil.copytree(KIT, dst)
        return dst


class TestInstallRepo(InstallRepoTestCase):
    def test_merge_into_existing_repo_twice_is_idempotent(self):
        c = self.claude
        (c / "agents").mkdir(parents=True)
        (c / "agents" / "other.md").write_text("other agent\n")
        self.write_settings(EXISTING_SETTINGS)

        first = self.run_kit(self.repo)
        self.assertEqual(first.returncode, 0, first.stderr)
        after_first = self.tree(), (c / "settings.json").read_text()
        second = self.run_kit(self.repo)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.tree(), (c / "settings.json").read_text()), after_first)

        self.assertInstalled()
        s = self.settings()
        self.assertEqual(s["permissions"], EXISTING_SETTINGS["permissions"])
        self.assertEqual(s["hooks"]["PostToolUse"][0], EXISTING_SETTINGS["hooks"]["PostToolUse"][0])
        self.assertEqual(len(s["hooks"]["PostToolUse"]), 2)
        self.assertEqual(len(s["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(s["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual((c / "agents" / "other.md").read_text(), "other agent\n")

    def test_coordinator_example_settings(self):
        existing = {"hooks": {
            "PostToolUse": [{"matcher": "Edit|Write|Bash", "hooks": [{"type": "command", "command": "echo a", "timeout": 30}]}],
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "echo b", "timeout": 10}]}]}}
        self.write_settings(existing)
        for _ in range(2):
            self.assertEqual(self.run_kit(self.repo).returncode, 0)
        s = self.settings()
        self.assertEqual(s["hooks"]["PostToolUse"][0], existing["hooks"]["PostToolUse"][0])
        self.assertEqual(s["hooks"]["SessionStart"][0], existing["hooks"]["SessionStart"][0])
        self.assertEqual([len(s["hooks"][e]) for e in EVENTS], [2, 1, 2])
        self.assertInstalled()

    def test_settings_format_is_two_space_indent_with_trailing_newline(self):
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        text = (self.claude / "settings.json").read_text()
        self.assertTrue(text.endswith("}\n"))
        self.assertIn('\n  "hooks": {\n    "SessionStart"', text)

    def test_original_indent_is_kept(self):
        for label, kw, marker in (("four", {"indent": 4}, '\n    "hooks": {\n        "SessionStart": [\n            {'),
                                  ("tab", {"indent": "\t"}, '\n\t"hooks": {\n\t\t"SessionStart": [\n\t\t\t{'),
                                  ("two", {"indent": 2}, '\n  "hooks": {\n    "SessionStart": [\n      {')):
            with self.subTest(label):
                shutil.rmtree(self.claude, ignore_errors=True)
                self.write_settings({"permissions": {"allow": ["x"]}}, **kw)
                self.assertEqual(self.run_kit(self.repo).returncode, 0)
                text = (self.claude / "settings.json").read_text()
                self.assertIn(marker, text)
                self.assertTrue(text.endswith("}\n"))

    def test_minified_settings_are_expanded_with_the_default_two_space_indent(self):
        self.write_settings({"permissions": {"allow": ["x"]}})  # json.dumps default: one line
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        text = (self.claude / "settings.json").read_text()
        self.assertIn('\n  "hooks": {\n    "SessionStart"', text)
        self.assertTrue(text.endswith("}\n"))

    def test_non_ascii_settings_preserved(self):
        self.write_settings('{"note": "שלום ✓"}')
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        self.assertIn("שלום ✓", (self.claude / "settings.json").read_text(encoding="utf-8"))

    def test_settings_file_mode_is_kept(self):
        self.write_settings(EXISTING_SETTINGS)
        os.chmod(self.claude / "settings.json", 0o640)
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        self.assertEqual((self.claude / "settings.json").stat().st_mode & 0o777, 0o640)

    def test_repo_without_claude_dir(self):
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertInstalled()
        s = self.settings()
        self.assertEqual(sorted(s), ["hooks"])
        self.assertEqual(sorted(s["hooks"]), ["PostToolUse", "SessionStart", "UserPromptSubmit"])

    def test_existing_router_hook_is_not_duplicated_or_touched(self):
        custom = {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "python3 .claude/model-router/router_nudge.py", "timeout": 3}]}]}}
        self.write_settings(custom)
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        self.assertEqual(self.settings()["hooks"]["SessionStart"], custom["hooks"]["SessionStart"])
        self.assertEqual(len(self.router_groups("UserPromptSubmit")), 1)

    def test_output_lists_files(self):
        out = self.run_kit(self.repo).stdout
        for path in (".claude/agents/scout.md", ".claude/model-router/router_nudge.py",
                     ".claude/model-router/POLICY.md", ".claude/model-router/manifest.json",
                     ".claude/settings.json"):
            self.assertIn(path, out)
        self.assertIn("created", out)
        self.assertNotIn("created", self.run_kit(self.repo).stdout)
        self.assertIn("unchanged", self.run_kit(self.repo).stdout)

    def test_installed_repo_copy_runs_in_cloud_session(self):
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        script = self.claude / "model-router" / "router_nudge.py"
        proc = subprocess.run([sys.executable, str(script), "--repo"],
                              input=json.dumps({"hook_event_name": "SessionStart", "session_id": "s"}),
                              capture_output=True, text=True, env=self.env(CLAUDE_CODE_REMOTE="1"), timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Tripwires", json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_default_kit_falls_back_to_config_dir(self):
        # router_kit.py with no ./plugin next to it: the kit lives in <CONFIG>/model-router/kit
        data = self.tmp / "config" / "model-router"
        data.mkdir(parents=True)
        shutil.copy(KIT_SCRIPT, data / "router_kit.py")
        shutil.copytree(KIT, data / "kit")
        proc = subprocess.run([sys.executable, str(data / "router_kit.py"), "install-repo", str(self.repo)],
                              capture_output=True, text=True, env=self.env(), timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertInstalled()


class TestHookCommand(InstallRepoTestCase):
    def test_every_merged_command_ends_with_or_true(self):
        self.write_settings(EXISTING_SETTINGS)
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        for event in EVENTS:
            for group in self.router_groups(event):
                for hook in group["hooks"]:
                    self.assertTrue(hook["command"].endswith(" --repo || true"), hook["command"])

    def test_missing_script_cannot_block_a_prompt(self):
        # python3 exits 2 for a missing script, and exit 2 from UserPromptSubmit blocks the prompt
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        (self.claude / "model-router" / "router_nudge.py").unlink()
        env = self.env(CLAUDE_PROJECT_DIR=str(self.repo))
        bare = subprocess.run(["sh", "-c", COMMAND.removesuffix(" || true")], capture_output=True, env=env, timeout=30)
        self.assertEqual(bare.returncode, 2)
        full = subprocess.run(["sh", "-c", COMMAND], capture_output=True, env=env, timeout=30)
        self.assertEqual(full.returncode, 0)
        self.assertEqual(self.router_groups("UserPromptSubmit")[0]["hooks"][0]["command"], COMMAND)

    def test_installed_command_runs_the_hook(self):
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        command = self.router_groups("SessionStart")[0]["hooks"][0]["command"]
        env = self.env(CLAUDE_PROJECT_DIR=str(self.repo), CLAUDE_CODE_REMOTE="1")
        proc = subprocess.run(["sh", "-c", command], input=json.dumps({"hook_event_name": "SessionStart", "session_id": "s"}),
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Tripwires", json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_older_command_without_or_true_is_upgraded_in_place(self):
        old_cmd = 'python3 "$CLAUDE_PROJECT_DIR/.claude/model-router/router_nudge.py" --repo'
        old = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "timeout": 10, "command": old_cmd}]}],
                         "Stop": [{"hooks": [{"type": "command", "command": "echo keep"}]}]}}
        self.write_settings(old)
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        got = self.settings()["hooks"]
        self.assertEqual(len(got["SessionStart"]), 1, "no second router group was added")
        self.assertTrue(got["SessionStart"][0]["hooks"][0]["command"].endswith("--repo || true"))
        self.assertEqual(got["SessionStart"][0]["hooks"][0]["timeout"], 10)
        self.assertEqual(got["Stop"], old["hooks"]["Stop"])
        # second run: nothing left to change
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("unchanged  .claude/settings.json", proc.stdout)


class TestSettingsSerialization(InstallRepoTestCase):
    def test_crlf_line_endings_are_preserved(self):
        self.write_settings(json.dumps({"permissions": {"allow": ["x"]}}, indent=2).replace("\n", "\r\n") + "\r\n")
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        raw = (self.claude / "settings.json").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotRegex(raw, rb"(?<!\r)\n")  # every newline is CRLF
        self.assertTrue(raw.endswith(b"}\r\n"))
        self.assertEqual(self.settings()["permissions"], {"allow": ["x"]})
        self.assertEqual(len(self.router_groups("SessionStart")), 1)

    def test_lf_files_stay_lf(self):
        self.write_settings({"a": 1}, indent=2)
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        self.assertNotIn(b"\r", (self.claude / "settings.json").read_bytes())

    def test_nan_is_refused_and_nothing_is_written(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal):
                shutil.rmtree(self.claude, ignore_errors=True)
                self.write_settings('{"threshold": %s}' % literal)
                proc = self.run_kit(self.repo)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("settings.json contains a non-JSON number", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual((self.claude / "settings.json").read_text(), '{"threshold": %s}' % literal)
                self.assertEqual(self.tree(), [".claude", ".claude/settings.json"])

    def test_nan_in_a_file_that_needs_no_change_is_not_an_error(self):
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        text = (self.claude / "settings.json").read_text().replace('"hooks"', '"threshold": NaN, "hooks"', 1)
        (self.claude / "settings.json").write_text(text)
        proc = self.run_kit(self.repo)  # already installed: nothing to serialize
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.claude / "settings.json").read_text(), text)

    def test_empty_or_whitespace_only_settings_count_as_empty_object(self):
        for label, text in (("empty", ""), ("spaces", "  \n\t\n")):
            with self.subTest(label):
                shutil.rmtree(self.claude, ignore_errors=True)
                self.write_settings(text)
                proc = self.run_kit(self.repo)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(sorted(self.settings()), ["hooks"])
                self.assertRegex(proc.stdout, r"updated\s+\.claude/settings\.json")
                self.assertInstalled()

    def test_literal_null_settings_are_still_refused(self):
        self.write_settings("null")
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("top level is not a JSON object", proc.stderr)


class TestAgentDiscovery(InstallRepoTestCase):
    def test_stray_files_in_the_kit_agents_dir_are_not_installed(self):
        kit = self.copy_kit()
        (kit / "agents" / "README.md").write_text("# About these agents\n\nname: not frontmatter\n")
        (kit / "agents" / "notes.md").write_text("---\ndescription: frontmatter but no name\n---\nbody\n")
        (kit / "agents" / "draft.md").write_text("name: draft\n---\nbody\n")  # does not open with ---
        (kit / "agents" / "unterminated.md").write_text("---\ndescription: x\n")
        proc = self.run_kit(self.repo, kit=kit)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(p.name for p in (self.claude / "agents").iterdir()), [f"{a}.md" for a in AGENTS])
        self.assertNotIn("README", proc.stdout)
        self.assertEqual(sorted(json.loads((self.claude / "model-router" / "manifest.json").read_text())["agents"]),
                         [f"{a}.md" for a in AGENTS])

    def test_a_valid_extra_agent_in_the_kit_is_installed(self):
        kit = self.copy_kit()
        (kit / "agents" / "extra.md").write_text("---\nname: extra\ndescription: another one\n---\nbody\n")
        self.assertEqual(self.run_kit(self.repo, kit=kit).returncode, 0)
        self.assertTrue((self.claude / "agents" / "extra.md").exists())

    def test_kit_with_only_stray_files_is_incomplete(self):
        kit = self.copy_kit()
        for p in (kit / "agents").glob("*.md"):
            p.unlink()
        (kit / "agents" / "README.md").write_text("# nothing here\n")
        proc = self.run_kit(self.repo, kit=kit)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("router kit incomplete", proc.stderr)
        self.assertEqual(self.tree(), [])


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
class TestWritability(InstallRepoTestCase):
    def lock(self, path, mode):
        os.chmod(path, mode)
        self.addCleanup(os.chmod, path, 0o755)  # runs before the tmp tree is removed

    def test_read_only_model_router_dir_exits_1_and_writes_nothing(self):
        (self.claude / "model-router").mkdir(parents=True)
        self.lock(self.claude / "model-router", 0o555)
        before = self.tree()
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn(".claude/model-router", proc.stderr)
        self.assertIn("not writable", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.tree(), before)  # no agents, no settings, no temp files

    def test_read_only_claude_dir_exits_1_and_writes_nothing(self):
        self.claude.mkdir()
        self.lock(self.claude, 0o555)
        before = self.tree()
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not writable", proc.stderr)
        self.assertEqual(self.tree(), before)

    def test_read_only_agents_dir_exits_1_and_writes_nothing(self):
        (self.claude / "agents").mkdir(parents=True)
        self.lock(self.claude / "agents", 0o555)
        before = self.tree()
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(".claude/agents", proc.stderr)
        self.assertEqual(self.tree(), before)

    def test_read_only_settings_file_that_needs_a_change_exits_1_and_writes_nothing(self):
        self.write_settings(EXISTING_SETTINGS)
        os.chmod(self.claude / "settings.json", 0o444)
        before = self.tree()
        text = (self.claude / "settings.json").read_text()
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("settings.json", proc.stderr)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.claude / "settings.json").read_text(), text)

    def test_read_only_settings_file_that_needs_no_change_is_fine(self):
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        os.chmod(self.claude / "settings.json", 0o444)
        proc = self.run_kit(self.repo)  # nothing to write anywhere
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestRefusals(InstallRepoTestCase):
    def test_non_directory_path_exits_1(self):
        f = self.tmp / "a-file"
        f.write_text("x")
        for bad in (f, self.tmp / "does-not-exist"):
            proc = self.run_kit(bad)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("not a directory", proc.stderr)
        self.assertFalse((self.tmp / "does-not-exist").exists())

    def test_invalid_settings_json_exits_1_and_changes_nothing(self):
        self.write_settings("{ not json")
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((self.claude / "settings.json").read_text(), "{ not json")
        self.assertEqual(self.tree(), [".claude", ".claude/settings.json"])

    def test_wrong_settings_shapes_exit_1_and_write_nothing(self):
        cases = {
            "top level list": [1],
            "hooks not a dict": {"hooks": []},
            "hooks null": {"hooks": None},
            "event not a list": {"hooks": {"PostToolUse": {"matcher": "x"}}},
            "group not a dict": {"hooks": {"SessionStart": ["oops"]}},
            "group hooks not a list": {"hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": "x"}]}},
            "group hooks missing": {"hooks": {"UserPromptSubmit": [{"matcher": ""}]}},
            "hook entry not a dict": {"hooks": {"PostToolUse": [{"hooks": ["echo hi"]}]}},
        }
        for label, shape in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.claude, ignore_errors=True)
                self.write_settings(shape)
                before = (self.claude / "settings.json").read_text()
                proc = self.run_kit(self.repo)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("settings.json", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual((self.claude / "settings.json").read_text(), before)
                self.assertEqual(self.tree(), [".claude", ".claude/settings.json"])

    def test_odd_shapes_in_other_events_are_left_alone(self):
        existing = {"hooks": {"Stop": "whatever", "Notification": [5]}}
        self.write_settings(existing)
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        self.assertEqual(self.settings()["hooks"]["Stop"], "whatever")
        self.assertEqual(self.settings()["hooks"]["Notification"], [5])

    def test_claude_is_a_file_exits_1(self):
        (self.repo / ".claude").write_text("i am a file")
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual((self.repo / ".claude").read_text(), "i am a file")

    def test_settings_json_is_a_directory_exits_1(self):
        (self.claude / "settings.json").mkdir(parents=True)
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.tree(), [".claude", ".claude/settings.json"])

    def test_agents_path_is_a_file_exits_1_and_writes_nothing(self):
        self.claude.mkdir()
        (self.claude / "agents").write_text("file")
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.tree(), [".claude", ".claude/agents"])

    def test_missing_kit_exits_1(self):
        proc = self.run_kit(self.repo, kit=self.tmp / "no-kit")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("router kit incomplete", proc.stderr)
        self.assertEqual(self.tree(), [])


class TestForeignAgents(InstallRepoTestCase):
    def put_agent(self, name, text):
        (self.claude / "agents").mkdir(parents=True, exist_ok=True)
        (self.claude / "agents" / name).write_text(text)

    def test_foreign_agent_blocks_install_and_nothing_is_written(self):
        self.put_agent("reviewer.md", "my own reviewer\n")
        self.write_settings(EXISTING_SETTINGS)
        before = self.tree(), (self.claude / "settings.json").read_text()
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(".claude/agents/reviewer.md", proc.stderr)
        self.assertIn("--force", proc.stderr)
        self.assertNotIn("scout.md", proc.stderr)
        self.assertEqual((self.tree(), (self.claude / "settings.json").read_text()), before)
        self.assertEqual((self.claude / "agents" / "reviewer.md").read_text(), "my own reviewer\n")

    def test_force_saves_bak_and_replaces(self):
        self.put_agent("reviewer.md", "my own reviewer\n")
        proc = self.run_kit(self.repo, "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.claude / "agents" / "reviewer.md.bak").read_text(), "my own reviewer\n")
        self.assertEqual((self.claude / "agents" / "reviewer.md").read_bytes(), (KIT / "agents" / "reviewer.md").read_bytes())
        self.assertIn("reviewer.md.bak", proc.stdout)
        self.assertInstalled()
        # a second forced install has nothing foreign left and keeps the .bak untouched
        again = self.run_kit(self.repo, "--force")
        self.assertEqual(again.returncode, 0)
        self.assertEqual((self.claude / "agents" / "reviewer.md.bak").read_text(), "my own reviewer\n")
        self.assertFalse((self.claude / "agents" / "reviewer.md.bak.2").exists())

    def test_force_never_overwrites_an_existing_bak(self):
        self.put_agent("reviewer.md", "first\n")
        self.assertEqual(self.run_kit(self.repo, "--force").returncode, 0)
        self.put_agent("reviewer.md", "second\n")
        self.assertEqual(self.run_kit(self.repo, "--force").returncode, 0)
        agents = self.claude / "agents"
        self.assertEqual((agents / "reviewer.md.bak").read_text(), "first\n")
        self.assertEqual((agents / "reviewer.md.bak.2").read_text(), "second\n")

    def test_untouched_earlier_copies_are_updated_without_force(self):
        kit = self.copy_kit()
        self.assertEqual(self.run_kit(self.repo, kit=kit).returncode, 0)
        (kit / "agents" / "scout.md").write_text((kit / "agents" / "scout.md").read_text() + "\nv2 change\n")
        proc = self.run_kit(self.repo, kit=kit)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("v2 change", (self.claude / "agents" / "scout.md").read_text())
        self.assertRegex(proc.stdout, r"updated\s+\.claude/agents/scout\.md")
        manifest = json.loads((self.claude / "model-router" / "manifest.json").read_text())
        self.assertEqual(manifest["agents"]["scout.md"], sha(kit / "agents" / "scout.md"))
        self.assertFalse(list((self.claude / "agents").glob("*.bak*")))

    def test_hand_edited_copy_is_protected_after_install(self):
        kit = self.copy_kit()
        self.assertEqual(self.run_kit(self.repo, kit=kit).returncode, 0)
        edited = (self.claude / "agents" / "builder.md").read_text() + "\nmy tweak\n"
        (self.claude / "agents" / "builder.md").write_text(edited)
        (kit / "agents" / "builder.md").write_text((kit / "agents" / "builder.md").read_text() + "\nv2\n")
        proc = self.run_kit(self.repo, kit=kit)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("builder.md", proc.stderr)
        self.assertEqual((self.claude / "agents" / "builder.md").read_text(), edited)

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_identical_preexisting_copy_is_accepted_silently(self):
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True, env=self.env())  # no "no .git" warning
        self.put_agent("scout.md", (KIT / "agents" / "scout.md").read_text())
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertRegex(proc.stdout, r"unchanged\s+\.claude/agents/scout\.md")
        self.assertNotRegex(proc.stdout, r"(backup|replaced)\s")
        self.assertFalse(list((self.claude / "agents").glob("*.bak*")))
        self.assertInstalled()

    def test_corrupt_manifest_is_treated_as_empty(self):
        self.assertEqual(self.run_kit(self.repo).returncode, 0)
        (self.claude / "model-router" / "manifest.json").write_text("{ nope")
        self.assertEqual(self.run_kit(self.repo).returncode, 0)  # copies equal the kit, so still fine
        self.assertInstalled()  # and the manifest has been rewritten

    def test_other_agents_are_never_touched(self):
        self.put_agent("other.md", "other agent\n")
        self.assertEqual(self.run_kit(self.repo, "--force").returncode, 0)
        self.assertEqual((self.claude / "agents" / "other.md").read_text(), "other agent\n")
        self.assertNotIn("other", json.loads((self.claude / "model-router" / "manifest.json").read_text())["agents"])


class TestWarnings(InstallRepoTestCase):
    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, env=self.env())

    def test_no_git_warns_but_succeeds(self):
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("no .git", proc.stderr)

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_git_repo_not_ignored_has_no_warning(self):
        self.assertEqual(self.git("init", "-q").returncode, 0)
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_ignored_claude_dir_warns_but_succeeds(self):
        self.assertEqual(self.git("init", "-q").returncode, 0)
        (self.repo / ".gitignore").write_text(".claude/\n")
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("warning:", proc.stderr)
        self.assertIn("git-ignored", proc.stderr)
        self.assertInstalled()

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_only_the_ignored_paths_are_named(self):
        self.assertEqual(self.git("init", "-q").returncode, 0)
        (self.repo / ".gitignore").write_text(".claude/agents/scout.md\n")
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr.count("warning:"), 1)  # one warning, not one per path
        self.assertIn(".claude/agents/scout.md", proc.stderr)
        self.assertNotIn(".claude/settings.json", proc.stderr)
        self.assertNotIn("agents/builder.md", proc.stderr)
        self.assertIn("broken install", proc.stderr)
        self.assertIn("hooks without the script", proc.stderr)
        self.assertInstalled()

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_ignored_script_or_policy_warns(self):
        self.assertEqual(self.git("init", "-q").returncode, 0)
        (self.repo / ".gitignore").write_text("*.py\nPOLICY.md\n")
        proc = self.run_kit(self.repo)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(".claude/model-router/router_nudge.py", proc.stderr)
        self.assertIn(".claude/model-router/POLICY.md", proc.stderr)
        self.assertNotIn(".claude/settings.json", proc.stderr)

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_ignored_settings_and_manifest_are_named(self):
        self.assertEqual(self.git("init", "-q").returncode, 0)
        (self.repo / ".gitignore").write_text("settings.json\nmanifest.json\n")
        proc = self.run_kit(self.repo)
        self.assertIn(".claude/settings.json", proc.stderr)
        self.assertIn(".claude/model-router/manifest.json", proc.stderr)
        self.assertIn("script without hooks", proc.stderr)

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_install_never_stages_or_commits(self):
        self.assertEqual(self.git("init", "-q").returncode, 0)
        self.run_kit(self.repo)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no").stdout, "")
        self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD").returncode, 0)


if __name__ == "__main__":
    unittest.main()
