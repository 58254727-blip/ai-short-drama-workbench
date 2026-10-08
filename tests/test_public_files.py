"""Behavior checks for the explicit Git publication candidate gate."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "tools" / "check_public_files.py"


class PublicFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)

    def check_files(self, *candidates):
        return subprocess.run([sys.executable, str(CHECKER), *candidates], cwd=self.root, text=True, capture_output=True)

    def test_clean_tracked_source_passes_without_walking_ignored_runtime(self):
        (self.root / "app.py").write_text("print('fictional demo')\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=self.root, check=True, capture_output=True)
        private = self.root / ".runtime"
        private.mkdir()
        (private / "operator-config.json").write_text('{"secret":"leave alone"}', encoding="utf-8")
        result = self.check_files()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 file", result.stdout)

    def test_staged_database_and_candidate_model_are_rejected(self):
        (self.root / "workbench.db").write_bytes(b"SQLite format 3\0")
        subprocess.run(["git", "add", "workbench.db"], cwd=self.root, check=True, capture_output=True)
        (self.root / "weights.safetensors").write_bytes(b"fictional")
        result = self.check_files("weights.safetensors")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workbench.db", result.stderr)
        self.assertIn("weights.safetensors", result.stderr)

    def test_unlisted_png_and_private_key_material_are_rejected(self):
        (self.root / "portrait.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.root / "notes.txt").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
        result = self.check_files("portrait.png", "notes.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("portrait.png", result.stderr)
        self.assertIn("notes.txt", result.stderr)


if __name__ == "__main__":
    unittest.main()
