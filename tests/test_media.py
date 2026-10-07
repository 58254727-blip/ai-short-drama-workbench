"""Real local FFmpeg media contract tests."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from workbench.domain import DomainError
from workbench.media import decode_check, probe, trim


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="媒体 test ")
        self.root = Path(self.tmp.name)
        self.source = self.root / "有空格 视频.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "2", "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", str(self.source),
        ], check=True, capture_output=True, timeout=30)

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe_and_full_decode_report_real_streams(self):
        info = probe(self.source, data_root=self.root)
        self.assertEqual((info["width"], info["height"]), (160, 90))
        self.assertEqual(info["fps"], "24/1")
        self.assertTrue(info["has_audio"])
        self.assertEqual(info["frame_count"], 48)
        self.assertEqual(info["frame_count_kind"], "reported")
        self.assertTrue(decode_check(self.source, data_root=self.root)["decoded"])

    def test_trim_reencodes_audio_and_rejects_spoken_clip(self):
        dest = self.root / "剪辑 有空格.mp4"
        with self.assertRaises(DomainError):
            trim(self.source, 500, 1500, dest, data_root=self.root, speech_ranges=[(400, 700)])
        result = trim(self.source, 500, 1500, dest, data_root=self.root)
        self.assertTrue(result["decoded"])
        self.assertTrue(result["has_audio"])
        self.assertEqual(result["method"], "reencode")
        self.assertAlmostEqual(result["duration_ms"], 1000, delta=90)

    def test_oversource_and_escape_are_rejected(self):
        with self.assertRaises(DomainError):
            trim(self.source, 0, 3000, self.root / "bad.mp4", data_root=self.root)
        with self.assertRaises(DomainError):
            probe(Path(__file__), data_root=self.root)

    def test_missing_audio_is_explicit(self):
        silent = self.root / "silent.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24", "-t", "1", "-an", "-c:v", "mpeg4", str(silent)], check=True, capture_output=True, timeout=30)
        self.assertFalse(probe(silent, data_root=self.root)["has_audio"])


if __name__ == "__main__":
    unittest.main()
