"""Persistent selected cuts and edit-safe subtitles over real source durations."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from workbench.domain import DomainError
from workbench.store import Store
from workbench.timeline import TimelineService
from workbench.subtitles import SubtitleService, write_srt


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "db.sqlite3")
        self.project = self.store.create_project("甲")
        self.episode = self.store.create_episode(self.project["id"], "一")
        staging = self.root / "staging"
        staging.mkdir()
        source = staging / "source.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True, capture_output=True, timeout=30)
        self.asset = self.store.import_asset(self.project["id"], source, "video", {})
        self.shot = self.store.save_shot(self.episode["id"], {"dialogue": [{"speaker_id": "a", "text": "你好"}]})
        self.shot = self.store.select_candidate(self.shot["id"], self.asset["id"], self.shot["revision"])
        self.timeline = TimelineService(self.store)
        self.subtitles = SubtitleService(self.store)
        self.saved_timeline = self.timeline.save_timeline(self.episode["id"], [{"shot_id": self.shot["id"], "source_asset_id": self.asset["id"], "in_ms": 250, "out_ms": 1250}], self.episode["revision"])

    def cue(self, start=100, end=500, source=None, speaker="a"):
        return {"start_ms": start, "end_ms": end, "text": "你好", "speaker_id": speaker, "source_asset_id": source or self.asset["id"]}

    def test_edit_cues_persists_text_and_advances_revision(self):
        saved = self.subtitles.save_cues(self.episode["id"], [self.cue()], self.saved_timeline["revision"])
        self.assertEqual("你好", self.subtitles.get_cues(self.episode["id"])["cues"][0]["text"])
        changed = self.cue()
        changed["text"] = "再见"
        changed["id"] = saved["cues"][0]["id"]
        again = self.subtitles.save_cues(self.episode["id"], [changed], saved["revision"])
        self.assertEqual("再见", again["cues"][0]["text"])
        self.assertEqual(saved["cues"][0]["id"], again["cues"][0]["id"])

    def test_rejects_overlap_bounds_unknown_speaker_and_stale_revision_atomically(self):
        saved = self.subtitles.save_cues(self.episode["id"], [self.cue()], self.saved_timeline["revision"])
        cases = [([self.cue(), self.cue(400, 700)], "cue_overlap"), ([self.cue(900, 1100)], "cue_out_of_range"), ([self.cue(speaker="alien")], "unknown_speaker")]
        for cues, code in cases:
            with self.subTest(code=code), self.assertRaises(DomainError) as caught:
                self.subtitles.save_cues(self.episode["id"], cues, saved["revision"])
            self.assertEqual(code, caught.exception.code)
            self.assertEqual(saved["cues"], self.subtitles.get_cues(self.episode["id"])["cues"])
        with self.assertRaises(DomainError) as caught:
            self.subtitles.save_cues(self.episode["id"], [self.cue()], self.saved_timeline["revision"])
        self.assertEqual("revision_conflict", caught.exception.code)

    def test_rejects_other_episode_source_and_mismatched_selected_segment(self):
        second = self.store.create_episode(self.project["id"], "二")
        other_shot = self.store.save_shot(second["id"], {})
        other_shot = self.store.select_candidate(other_shot["id"], self.asset["id"], other_shot["revision"])
        with self.assertRaises(DomainError):
            self.timeline.save_timeline(self.episode["id"], [{"shot_id": other_shot["id"], "source_asset_id": self.asset["id"], "in_ms": 0, "out_ms": 1000}], self.saved_timeline["revision"])
        different = self.store.import_asset(self.project["id"], self.root / "staging" / "source.mp4", "video", {})
        with self.assertRaises(DomainError) as caught:
            self.subtitles.save_cues(self.episode["id"], [self.cue(source=different["id"])], self.saved_timeline["revision"])
        self.assertEqual("cue_source_mismatch", caught.exception.code)

    def test_timeline_rejects_source_overrun_and_cut_through_speech(self):
        with self.assertRaises(DomainError) as caught:
            self.timeline.save_timeline(self.episode["id"], [{"shot_id": self.shot["id"], "source_asset_id": self.asset["id"], "in_ms": 0, "out_ms": 3000}], self.saved_timeline["revision"])
        self.assertEqual("invalid_cut", caught.exception.code)
        with self.assertRaises(DomainError) as caught:
            self.timeline.save_timeline(self.episode["id"], [{"shot_id": self.shot["id"], "source_asset_id": self.asset["id"], "in_ms": 250, "out_ms": 1000, "speech_start_ms": 900, "speech_end_ms": 1100}], self.saved_timeline["revision"])
        self.assertEqual("speech_cut", caught.exception.code)

    def test_timeline_change_marks_existing_cues_needs_realign_without_erasing_text(self):
        saved = self.subtitles.save_cues(self.episode["id"], [self.cue(100, 500)], self.saved_timeline["revision"])
        changed = self.timeline.save_timeline(self.episode["id"], [{"shot_id": self.shot["id"], "source_asset_id": self.asset["id"], "in_ms": 250, "out_ms": 600}], saved["revision"])
        self.assertEqual("needs_realign", self.subtitles.get_cues(self.episode["id"])["status"])
        self.assertEqual("你好", self.subtitles.get_cues(self.episode["id"])["cues"][0]["text"])
        self.assertEqual(changed["revision"], self.timeline.get_timeline(self.episode["id"])["revision"])

    def test_cue_save_rejects_changed_source_binary(self):
        stored = self.root / "assets" / self.asset["sha256"]
        stored.write_bytes(b"tampered")
        with self.assertRaises(DomainError) as caught:
            self.subtitles.save_cues(self.episode["id"], [self.cue()], self.saved_timeline["revision"])
        self.assertEqual("asset_hash_mismatch", caught.exception.code)

    def test_srt_unicode_and_no_markup_injection(self):
        path = self.root / "captions.srt"
        write_srt([{**self.cue(), "text": "你好 <b>世界</b>"}], path)
        self.assertEqual("1\n00:00:00,100 --> 00:00:00,500\n你好 &lt;b&gt;世界&lt;/b&gt;\n", path.read_text(encoding="utf-8-sig").strip() + "\n")


if __name__ == "__main__":
    unittest.main()
