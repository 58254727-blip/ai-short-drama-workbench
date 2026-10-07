"""Real local FFmpeg media contract tests."""

import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from workbench.domain import DomainError
from workbench.media import decode_check, probe, trim
from workbench import media as media_module


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

    def test_two_trims_same_path_preserve_first_success(self):
        destination = self.root / "shared.mp4"
        reached = threading.Barrier(2)
        first_done = threading.Event()
        local = threading.local()
        real_run = media_module._run
        results = [None, None]

        def controlled_run(args, timeout=180):
            if "-c:v" in args:
                reached.wait(timeout=15)
                if local.index == 1:
                    self.assertTrue(first_done.wait(15))
            return real_run(args, timeout=timeout)

        def worker(index):
            local.index = index
            try:
                results[index] = trim(self.source, 0, 1000, destination, data_root=self.root)
            except DomainError as error:
                results[index] = error
            finally:
                if index == 0:
                    first_done.set()

        with patch.object(media_module, "_run", controlled_run):
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
        self.assertEqual(results[0]["sha256"], probe(destination, data_root=self.root)["sha256"])


if __name__ == "__main__":
    unittest.main()
