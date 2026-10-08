import json
import hashlib
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
from workbench.store import Store
from workbench.timeline import TimelineService
from workbench.subtitles import SubtitleService
from workbench.review import ManualReviewService
from workbench.domain import DomainError


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ExtendedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = Store(root / "source.sqlite", root / "source")
        TimelineService(self.store); SubtitleService(self.store); ManualReviewService(self.store)
        self.project = self.store.create_project("虚构")
        self.episode = self.store.create_episode(self.project["id"], "第一集")
        self.shot = self.store.save_shot(self.episode["id"], {"story_job": "开门", "dialogue": [{"speaker_id": "甲", "text": "你好"}]})
        media = self.store.data_root / "staging" / "source-media.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2",
                        "-c:v", "mpeg4", "-c:a", "aac", str(media)], check=True, capture_output=True, timeout=30)
        self.asset = self.store.import_asset(self.project["id"], media, "video", {"source": "test"})
        self.shot = self.store.select_candidate(self.shot["id"], self.asset["id"], 1)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO episode_timelines VALUES (?,?)", (self.episode["id"], 3))
            conn.execute("INSERT INTO timeline_items VALUES (?,?,?,?,?,?,?,?)", (self.episode["id"], 0, self.shot["id"], self.asset["id"], 0, 1000, None, None))
            conn.execute("INSERT INTO subtitle_sets (episode_id,timeline_version,shot_snapshot) VALUES (?,?,?)", (self.episode["id"], 3, json.dumps({self.shot["id"]: self.shot["revision"]})))
            conn.execute("INSERT INTO subtitle_cues VALUES (?,?,?,?,?,?,?,?)", ("4c27d6b5-5690-48c8-adff-361b0d6646a1", self.episode["id"], 0, 0, 800, "你好", "甲", self.asset["id"]))
            conn.execute("INSERT INTO manual_qc VALUES (?,?,?,?,?,?,?,?)", (self.episode["id"], self.shot["id"], self.asset["id"], 2, 3, "pass", "已检查", "2026-10-07T00:00:00+00:00"))
        self.archive = root / "complete.zip"
        archive_project(self.store, self.project["id"], self.archive)
        self.target = Store(root / "target.sqlite", root / "target")

    def test_roundtrip_includes_timeline_cues_review_and_binary(self):
        restore_archive(self.target, self.archive)
        self.assertEqual(TimelineService(self.target).get_timeline(self.episode["id"])["version"], 3)
        self.assertEqual(SubtitleService(self.target).get_cues(self.episode["id"])["cues"][0]["text"], "你好")
        self.assertEqual(ManualReviewService(self.target).list(self.episode["id"])[0]["note"], "已检查")
        self.assertTrue(self.target.get_asset(self.asset["id"])["binary_available"])

    def test_corrupt_extension_leaves_destination_empty(self):
        broken = Path(self.temp.name) / "broken.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(broken, "w") as output:
            for name in source.namelist():
                data = source.read(name)
                if name == "metadata.json":
                    metadata = json.loads(data)
                    metadata["extensions"]["timeline_items"][0]["source_asset_id"] = "foreign"
                    data = json.dumps(metadata).encode()
                output.writestr(name, data)
        with self.assertRaises(DomainError): restore_archive(self.target, broken)
        self.assertEqual(self.target.list_projects(), [])
        self.assertFalse((self.target.data_root / "assets" / self.asset["sha256"]).exists())

    def test_conflicting_restore_preserves_existing_data(self):
        restore_archive(self.target, self.archive)
        with self.assertRaises(DomainError): restore_archive(self.target, self.archive)
        self.assertEqual(ManualReviewService(self.target).list(self.episode["id"])[0]["note"], "已检查")

    def test_stale_timeline_and_review_are_preserved_as_stale(self):
        media = self.store.data_root / "staging" / "other.mp4"
        media.write_bytes(b"another synthetic video")
        replacement = self.store.import_asset(self.project["id"], media, "video", {"source":"replacement"})
        self.store.select_candidate(self.shot["id"], replacement["id"], 2)
        stale_archive = Path(self.temp.name) / "stale.zip"
        archive_project(self.store, self.project["id"], stale_archive)
        restore_archive(self.target, stale_archive)
        self.assertEqual(TimelineService(self.target).get_timeline(self.episode["id"])["status"], "needs_selection")
        self.assertFalse(ManualReviewService(self.target).list(self.episode["id"])[0]["current"])

    def test_current_cue_cannot_claim_unrelated_same_project_asset(self):
        media = self.store.data_root / "staging" / "unrelated.mp4"
        media.write_bytes(b"unrelated video bytes")
        unrelated = self.store.import_asset(self.project["id"], media, "video", {"source":"unrelated"})
        complete = Path(self.temp.name) / "two-assets.zip"
        archive_project(self.store, self.project["id"], complete)
        corrupt = Path(self.temp.name) / "wrong-cue.zip"
        with zipfile.ZipFile(complete) as source, zipfile.ZipFile(corrupt, "w") as output:
            for name in source.namelist():
                data = source.read(name)
                if name == "metadata.json":
                    metadata = json.loads(data)
                    metadata["extensions"]["subtitle_cues"][0]["source_asset_id"] = unrelated["id"]
                    data = json.dumps(metadata).encode()
                output.writestr(name, data)
        with self.assertRaises(DomainError): restore_archive(self.target, corrupt)
        self.assertEqual(self.target.list_projects(), [])

    def test_forged_ready_cues_reject_blank_speaker_order_and_overlap_atomically(self):
        from uuid import uuid4
        changes = {
            "blank": lambda cues: cues[0].update(text="   "),
            "speaker": lambda cues: cues[0].update(speaker_id="陌生人"),
            "order": lambda cues: cues[0].update(ordinal=2),
            "overlap": lambda cues: cues.append({**cues[0], "id": str(uuid4()), "ordinal": 1, "start_ms": 400, "end_ms": 900}),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                target_root = Path(self.temp.name) / f"target-{label}"
                target = Store(target_root / "target.sqlite", target_root)
                corrupt = Path(self.temp.name) / f"forged-{label}.zip"
                with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(corrupt, "w") as output:
                    for name in source.namelist():
                        data = source.read(name)
                        if name == "metadata.json":
                            metadata = json.loads(data)
                            change(metadata["extensions"]["subtitle_cues"])
                            data = json.dumps(metadata).encode()
                        output.writestr(name, data)
                with self.assertRaises(DomainError): restore_archive(target, corrupt)
                self.assertEqual(target.list_projects(), [])
                self.assertFalse((target.data_root / "assets" / self.asset["sha256"]).exists())

    def test_current_timeline_rejects_forged_overrun_speech_cut_and_nonvideo_bytes(self):
        def rewrite(label, transform):
            target_root = Path(self.temp.name) / f"target-{label}"
            target = Store(target_root / "target.sqlite", target_root)
            corrupt = Path(self.temp.name) / f"invalid-{label}.zip"
            with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(corrupt, "w") as output:
                metadata = json.loads(source.read("metadata.json"))
                binaries = {name: source.read(name) for name in source.namelist() if name != "metadata.json"}
                transform(metadata, binaries)
                output.writestr("metadata.json", json.dumps(metadata))
                for name, binary in binaries.items(): output.writestr(name, binary)
            with self.assertRaises(DomainError): restore_archive(target, corrupt)
            self.assertEqual(target.list_projects(), [])
            self.assertFalse((target.data_root / "assets" / self.asset["sha256"]).exists())

        def overrun(metadata, _): metadata["extensions"]["timeline_items"][0]["out_ms"] = 5000
        def speech_cut(metadata, _): metadata["extensions"]["timeline_items"][0].update(in_ms=300, speech_start_ms=200, speech_end_ms=500)
        def fake_binary(metadata, binaries):
            old_sha = self.asset["sha256"]
            new_binary = b"not a video but correctly hashed"
            new_sha = hashlib.sha256(new_binary).hexdigest()
            metadata["assets"][0].update(sha256=new_sha, size_bytes=len(new_binary))
            binaries.pop(f"assets/{old_sha}")
            binaries[f"assets/{new_sha}"] = new_binary
        for label, change in (("overrun", overrun), ("speech", speech_cut), ("nonvideo", fake_binary)):
            with self.subTest(label=label): rewrite(label, change)

    def test_current_timeline_rejects_genuine_video_without_audio(self):
        silent = self.store.data_root / "staging" / "silent.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=10", "-t", "2", "-c:v", "mpeg4", str(silent)], check=True, capture_output=True, timeout=30)
        silent_asset = self.store.import_asset(self.project["id"], silent, "video", {"source": "silent fixture"})
        chosen = self.store.select_candidate(self.shot["id"], silent_asset["id"], self.shot["revision"])
        with self.store.transaction() as conn:
            conn.execute("UPDATE timeline_items SET source_asset_id=? WHERE episode_id=?", (silent_asset["id"], self.episode["id"]))
            conn.execute("UPDATE subtitle_cues SET source_asset_id=? WHERE episode_id=?", (silent_asset["id"], self.episode["id"]))
            conn.execute("UPDATE subtitle_sets SET shot_snapshot=? WHERE episode_id=?", (json.dumps({chosen["id"]: chosen["revision"]}), self.episode["id"]))
        archive = Path(self.temp.name) / "silent.zip"
        archive_project(self.store, self.project["id"], archive)
        with self.assertRaises(DomainError): restore_archive(self.target, archive)
        self.assertEqual(self.target.list_projects(), [])

    def test_backup_core_and_extensions_share_one_sqlite_snapshot_under_writer(self):
        started = threading.Event(); done = threading.Event(); threads = []
        original_export = self.store.export_project
        original_collect = archive_module.collect_extensions
        def concurrent_writer():
            started.set()
            with self.store.transaction() as conn:
                conn.execute("UPDATE episodes SET revision=revision+1 WHERE id=?", (self.episode["id"],))
                conn.execute("UPDATE episode_timelines SET version=version+1 WHERE episode_id=?", (self.episode["id"],))
            done.set()
        def export_then_write(*args, **kwargs):
            result = original_export(*args, **kwargs)
            thread = threading.Thread(target=concurrent_writer)
            thread.start(); threads.append(thread)
            self.assertTrue(started.wait(1))
            return result
        def collect_after_writer_attempt(*args, **kwargs):
            done.wait(0.3)
            return original_collect(*args, **kwargs)
        destination = Path(self.temp.name) / "concurrent.zip"
        with patch.object(self.store, "export_project", side_effect=export_then_write), patch.object(archive_module, "collect_extensions", side_effect=collect_after_writer_attempt):
            archive_project(self.store, self.project["id"], destination)
        for thread in threads: thread.join(timeout=5)
        self.assertTrue(done.is_set())
        with zipfile.ZipFile(destination) as archive:
            metadata = json.loads(archive.read("metadata.json"))
        self.assertEqual(metadata["episodes"][0]["revision"], self.episode["revision"])
        self.assertEqual(metadata["extensions"]["episode_timelines"][0]["version"], 3)


if __name__ == "__main__": unittest.main()
