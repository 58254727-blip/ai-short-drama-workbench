"""Behavior checks for the explicit Git publication candidate gate."""

import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "tools" / "check_public_files.py"
APPROVED_IMAGE = Path(__file__).resolve().parents[1] / "web" / "assets" / "rain-alley-demo.png"


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

    def test_unsafe_index_blob_cannot_be_hidden_by_safe_worktree_or_candidate_argument(self):
        source = self.root / "notes.txt"
        source.write_text("-----BEGIN PRIVATE KEY-----\nfictional fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "notes.txt"], cwd=self.root, check=True, capture_output=True)
        source.write_text("public notes only\n", encoding="utf-8")
        for candidates in ((), ("notes.txt",)):
            result = self.check_files(*candidates)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("notes.txt", result.stderr)

    def test_safe_index_blob_is_not_replaced_by_unsafe_unstaged_worktree(self):
        source = self.root / "notes.txt"
        source.write_text("public notes only\n", encoding="utf-8")
        subprocess.run(["git", "add", "notes.txt"], cwd=self.root, check=True, capture_output=True)
        source.write_text("-----BEGIN PRIVATE KEY-----\nfictional fixture\n", encoding="utf-8")
        result = self.check_files()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 file", result.stdout)

    def test_index_symlink_mode_is_rejected_even_when_worktree_file_looks_safe(self):
        target = self.root / "target.txt"
        target.write_text("fictional target\n", encoding="utf-8")
        oid = subprocess.run(["git", "hash-object", "-w", "target.txt"], cwd=self.root,
                             check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"120000,{oid},link.txt"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / "link.txt").write_text("safe looking worktree\n", encoding="utf-8")
        result = self.check_files()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("link.txt", result.stderr)

    def test_approved_png_hash_checks_index_bytes_not_changed_worktree(self):
        target = self.root / "web" / "assets" / "rain-alley-demo.png"
        target.parent.mkdir(parents=True)
        shutil.copyfile(APPROVED_IMAGE, target)
        subprocess.run(["git", "add", "web/assets/rain-alley-demo.png"], cwd=self.root,
                       check=True, capture_output=True)
        target.write_bytes(b"not the approved image")
        result = self.check_files()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
