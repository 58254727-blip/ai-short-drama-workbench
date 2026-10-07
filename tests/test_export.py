"""Real selected timeline and archive tests."""

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from workbench.archive import archive_project, restore_archive
from workbench import archive as archive_module
from workbench import exporter as exporter_module
from workbench.domain import DomainError
from workbench.exporter import _static_suspected, export_episode
from workbench.media import decode_check, probe
from workbench.store import Store


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="项目 备份 ")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "staging").mkdir(parents=True)
        self.store = Store(self.data / "workbench.sqlite", self.data)
        self.project = self.store.create_project("虚构测试")
        self.episode = self.store.create_episode(self.project["id"], "第一集")
        self.shots = []
        self.assets = []
        for index, frequency in enumerate((440, 660)):
            clip = self.data / "staging" / f"动态 {index}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000", "-t", "1.5", "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", str(clip)], check=True, capture_output=True, timeout=30)
            asset = self.store.import_asset(self.project["id"], clip, "video", {"source": "synthetic", "media_qc": "pending_decode"})
            shot = self.store.save_shot(self.episode["id"], {"order": index, "story_job": "测试"})
            shot = self.store.select_candidate(shot["id"], asset["id"], shot["revision"])
            self.shots.append(shot)
            self.assets.append(asset)

    def tearDown(self):
        self.tmp.cleanup()

    def timeline(self):
        return [{"shot_id": s["id"], "source_asset_id": a["id"], "in_ms": 0, "out_ms": 1000} for s, a in zip(self.shots, self.assets)]

    def scope(self):
        return {"store": self.store, "project_id": self.project["id"], "episode_id": self.episode["id"], "output_path": self.data / "exports" / "第一集 成片.mp4", "width": 160, "height": 90, "fps": 24}

    def test_selected_two_clip_export_and_utf8_subtitles(self):
        subtitle = self.data / "staging" / "中文 空格.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:00,800\n你好\n", encoding="utf-8")
        result = export_episode(self.scope(), self.timeline(), subtitle)
        self.assertEqual(result["method"], "reencode_concat")
        self.assertEqual(result["source_versions"], [a["sha256"] for a in self.assets])
        self.assertTrue(result["has_audio"])
        self.assertTrue(result["decoded"])
        self.assertFalse(result["human_reviewed"])
        self.assertAlmostEqual(result["duration_ms"], 2000, delta=110)
        self.assertEqual(probe(Path(result["path"]), data_root=self.data)["width"], 160)

    def test_wrong_selection_and_hash_change_rejected(self):
        timeline = self.timeline()
        timeline[0]["source_asset_id"] = self.assets[1]["id"]
        with self.assertRaises(DomainError):
            export_episode(self.scope(), timeline, None)
        binary = self.data / "assets" / self.assets[0]["sha256"]
        binary.write_bytes(b"changed")
        with self.assertRaises(DomainError):
            export_episode(self.scope(), self.timeline(), None)

    def test_cross_episode_and_spoken_cut_rejected(self):
        other = self.store.create_episode(self.project["id"], "第二集")
        scope = self.scope()
        scope["episode_id"] = other["id"]
        with self.assertRaises(DomainError):
            export_episode(scope, self.timeline(), None)
        timeline = self.timeline()
        timeline[0].update({"in_ms": 500, "speech_start_ms": 400, "speech_end_ms": 800})
        with self.assertRaises(DomainError):
            export_episode(self.scope(), timeline, None)

    def test_archive_restores_binary_and_rejects_conflict(self):
        archive = self.root / "project.zip"
        archived = archive_project(self.store, self.project["id"], archive)
        self.assertEqual(archived["asset_count"], 2)
        restored_root = self.root / "restore data"
        restored_root.mkdir()
        restored = Store(restored_root / "workbench.sqlite", restored_root)
        report = restore_archive(restored, archive)
        self.assertEqual(report["project_id"], self.project["id"])
        for asset in self.assets:
            self.assertTrue(restored.get_asset(asset["id"])["binary_available"])
        with self.assertRaises(DomainError):
            restore_archive(restored, archive)
        self.assertEqual(len(restored.list_assets(self.project["id"])), 2)

    def test_traversal_archive_rejected_without_writes(self):
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("../escape", b"bad")
            z.writestr("metadata.json", json.dumps(self.store.export_project(self.project["id"])))
        target = self.root / "target"
        target.mkdir()
        restored = Store(target / "workbench.sqlite", target)
        with self.assertRaises(DomainError):
            restore_archive(restored, bad)
        self.assertEqual(restored.list_projects(), [])

    def test_static_video_only_flags_review(self):
        still = self.data / "staging" / "static.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:size=160x90:rate=24", "-t", "2", "-c:v", "mpeg4", str(still)], check=True, capture_output=True, timeout=30)
        self.assertTrue(_static_suspected(still, self.data))

    def test_corrupt_binary_archive_leaves_target_clean(self):
        archive = self.root / "bad content.zip"
        bundle = self.store.export_project(self.project["id"])
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("metadata.json", json.dumps(bundle))
            for asset in self.assets:
                z.writestr(f"assets/{asset['sha256']}", b"wrong binary")
        target = self.root / "clean target"
        target.mkdir()
        restored = Store(target / "workbench.sqlite", target)
        with self.assertRaises(DomainError):
            restore_archive(restored, archive)
        self.assertEqual(restored.list_projects(), [])
        self.assertFalse((target / "assets").exists())

    def test_aggregate_uncompressed_budget_rejects_without_records_or_files(self):
        archive = self.root / "budget.zip"
        archive_project(self.store, self.project["id"], archive)
        target = self.root / "budget target"
        target.mkdir()
        restored = Store(target / "workbench.sqlite", target)
        with patch.object(archive_module, "MAX_TOTAL_UNCOMPRESSED_BYTES", 100, create=True):
            with self.assertRaises(DomainError):
                restore_archive(restored, archive)
        self.assertEqual(restored.list_projects(), [])
        self.assertFalse((target / "assets").exists())

    def test_source_changes_during_archive_write_never_publishes_zip(self):
        destination = self.root / "raced.zip"
        original_write = zipfile.ZipFile.write
        changed = False

        def changed_write(z, filename, arcname=None, *args, **kwargs):
            nonlocal changed
            if arcname and arcname.startswith("assets/") and not changed:
                changed = True
                Path(filename).write_bytes(b"changed during packaging")
            return original_write(z, filename, arcname, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "write", changed_write):
            with self.assertRaises(DomainError):
                archive_project(self.store, self.project["id"], destination)
        self.assertFalse(destination.exists())

    def test_two_exports_same_path_preserve_first_success(self):
        destination = self.scope()["output_path"]
        reached = threading.Barrier(2)
        first_done = threading.Event()
        local = threading.local()
        real_run = exporter_module._run
        results = [None, None]

        def controlled_run(args, timeout=180):
            if "-filter_complex" in args:
                reached.wait(timeout=15)
                if local.index == 1:
                    self.assertTrue(first_done.wait(15))
            return real_run(args, timeout=timeout)

        def worker(index):
            local.index = index
            try:
                results[index] = export_episode(self.scope(), self.timeline(), None)
            except DomainError as error:
                results[index] = error
            finally:
                if index == 0:
                    first_done.set()

        with patch.object(exporter_module, "_run", controlled_run):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertIsInstance(results[0], dict)
        self.assertIsInstance(results[1], DomainError)
        self.assertEqual(results[1].code, "output_conflict")
        self.assertTrue(destination.exists())
        self.assertEqual(results[0]["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
