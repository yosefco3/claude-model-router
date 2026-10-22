"""Tests for the `router` bash CLI (stdlib unittest, run via subprocess against temp dirs)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "router"


class RouterCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="router-cli-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / "config"
        self.config.mkdir()
        (self.tmp / "home").mkdir()

    def router(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "HOME")}
        env.update(HOME=str(self.tmp / "home"), CLAUDE_CONFIG_DIR=str(self.config))
        return subprocess.run(["bash", str(ROUTER), *args], capture_output=True, text=True, env=env, timeout=30)

    def set_installed(self, keys):
        (self.config / "plugins").mkdir(exist_ok=True)
        (self.config / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {k: [{"scope": "user"}] for k in keys}}))

    def test_on_off_status(self):
        self.assertEqual(self.router("status").stdout.splitlines()[0], "on")
        out = self.router("off")
        self.assertEqual(out.returncode, 0)
        self.assertIn("next prompt", out.stdout)
        self.assertTrue((self.config / "model-router" / "OFF").exists())
        self.assertEqual(self.router("status").stdout.splitlines()[0], "off")
        self.assertIn("next prompt", self.router("on").stdout)
        self.assertFalse((self.config / "model-router" / "OFF").exists())
        self.assertEqual(self.router("status").stdout.splitlines()[0], "on")

    def test_status_plugin_line_needs_the_exact_plugin_key(self):
        self.assertEqual(self.router("status").stdout.splitlines()[1], "plugin: not installed")
        ok = "plugin: installed (claude-model-router)"
        for keys, expected in ((["model-router@claude-model-router"], ok),
                               (["model-router@someone-else"], "plugin: not installed"),
                               (["other@claude-model-router"], "plugin: not installed"),
                               (["other@x", "model-router@claude-model-router"], ok)):
            with self.subTest(keys=keys):
                self.set_installed(keys)
                self.assertEqual(self.router("status").stdout.splitlines()[1], expected)

    def test_status_sees_a_plugin_synced_from_claude_ai(self):
        synced = "plugin: installed (synced from claude.ai)"
        (self.config / "plugins" / "synced" / "other-plugin").mkdir(parents=True)
        self.assertEqual(self.router("status").stdout.splitlines()[1], "plugin: not installed")
        (self.config / "plugins" / "synced" / "model-router-abc123").mkdir()
        self.assertEqual(self.router("status").stdout.splitlines()[1], synced)
        shutil.rmtree(self.config / "plugins" / "synced")
        self.assertEqual(self.router("status").stdout.splitlines()[1], "plugin: not installed")
        self.set_installed(["model-router@synced"])
        self.assertEqual(self.router("status").stdout.splitlines()[1], synced)

    def test_marketplace_install_wins_over_synced_copy(self):
        (self.config / "plugins" / "synced" / "model-router").mkdir(parents=True)
        self.set_installed(["model-router@claude-model-router"])
        self.assertEqual(self.router("status").stdout.splitlines()[1], "plugin: installed (claude-model-router)")

    def test_a_synced_file_named_model_router_is_not_a_directory_install(self):
        (self.config / "plugins" / "synced").mkdir(parents=True)
        (self.config / "plugins" / "synced" / "model-router.txt").write_text("x")
        self.assertEqual(self.router("status").stdout.splitlines()[1], "plugin: not installed")

    def test_bad_usage_exits_1_and_prints_usage_to_stderr(self):
        for args in ((), ("bogus",), ("on", "extra"), ("status", "x"), ("install-repo",)):
            with self.subTest(args=args):
                proc = self.router(*args)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
                self.assertTrue(proc.stderr.startswith("usage: router"))


if __name__ == "__main__":
    unittest.main()
