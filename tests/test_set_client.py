"""set-client 回归测试:隔离核心副本,不改动真实安装。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "core" / "claude-switch.sh"


@unittest.skipUnless(sys.platform == "darwin", "sed -i '' is macOS syntax")
class SetClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.copy = self.root / "core.sh"
        self.copy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    def run_set(self, mode):
        env = dict(os.environ, CLAUDE_SWITCHER_CORE=str(self.copy))
        return subprocess.run(["/bin/bash", str(self.copy), "set-client", mode],
                              env=env, text=True, capture_output=True, timeout=10)

    def current_mode(self):
        for line in self.copy.read_text(encoding="utf-8").splitlines():
            if line.startswith("PROXY_MODE="):
                return line.split('"')[1]
        return ""

    def test_switches_mode(self):
        for mode in ("shadowrocket", "sing-box", "auto", "clash-verge"):
            result = self.run_set(mode)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.current_mode(), mode)

    def test_rejects_unknown_mode(self):
        before = self.current_mode()
        result = self.run_set("not-a-client")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current_mode(), before)


if __name__ == "__main__":
    unittest.main()
