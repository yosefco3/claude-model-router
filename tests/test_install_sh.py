"""Tests for install.sh (stdlib unittest, run via subprocess against a temp HOME and config dir)."""
import filecmp
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"


class InstallShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="router-install-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.config = self.tmp / "config"
        self.home.mkdir()
        self.bin = self.home / ".local" / "bin"
        self.data = self.config / "model-router"

    def install(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "HOME")}
        env.update(HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.config))
        return subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=env, timeout=60)

    def test_fresh_install_copies_files(self):
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(filecmp.cmp(ROOT / "router", self.bin / "router", shallow=False))
        self.assertTrue(os.access(self.bin / "router", os.X_OK))
        for name in ("router_stats.py", "router_kit.py"):
            self.assertTrue(filecmp.cmp(ROOT / name, self.data / name, shallow=False))
        self.assertTrue((self.data / "kit" / "hooks" / "router_nudge.py").is_file())
        self.assertEqual(self.install().returncode, 0)  # safe to re-run

    def test_symlinked_destinations_do_not_break_the_copy(self):
        # a dev setup: the installed files are symlinks to the checkout, where cp fails with "same file"
        self.bin.mkdir(parents=True)
        self.data.mkdir(parents=True)
        (self.bin / "router").symlink_to(ROOT / "router")
        for name in ("router_stats.py", "router_kit.py"):
            (self.data / name).symlink_to(ROOT / name)
        before = {n: (ROOT / n).read_bytes() for n in ("router", "router_stats.py", "router_kit.py")}
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("same file", proc.stderr)
        for path in (self.bin / "router", self.data / "router_stats.py", self.data / "router_kit.py"):
            self.assertFalse(path.is_symlink(), path)
            self.assertTrue(filecmp.cmp(ROOT / path.name, path, shallow=False))
        self.assertEqual({n: (ROOT / n).read_bytes() for n in before}, before)  # the checkout is untouched

    def test_symlink_to_another_file_is_replaced_not_written_through(self):
        self.bin.mkdir(parents=True)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.write_text("do not touch\n")
        (self.bin / "router").symlink_to(elsewhere)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(elsewhere.read_text(), "do not touch\n")
        self.assertFalse((self.bin / "router").is_symlink())
        self.assertTrue(filecmp.cmp(ROOT / "router", self.bin / "router", shallow=False))


if __name__ == "__main__":
    unittest.main()
