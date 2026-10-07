import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from workbench.archive import archive_project, restore_archive
from workbench.store import Store
from workbench.timeline import TimelineService
from workbench.subtitles import SubtitleService
from workbench.review import ManualReviewService
from workbench.domain import DomainError


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
        media.write_bytes(b"synthetic archive binary")
        self.asset = self.store.import_asset(self.project["id"], media, "video", {"source": "test"})
        self.shot = self.store.select_candidate(self.shot["id"], self.asset["id"], 1)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO episode_timelines VALUES (?,?)", (self.episode["id"], 3))
            conn.execute("INSERT INTO timeline_items VALUES (?,?,?,?,?,?,?,?)", (self.episode["id"], 0, self.shot["id"], self.asset["id"], 0, 1000, None, None))
            conn.execute("INSERT INTO subtitle_sets VALUES (?,?)", (self.episode["id"], 3))
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


if __name__ == "__main__": unittest.main()
