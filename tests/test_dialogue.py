"""Dialogue and story review behavior, without pretending ASR is a human ear."""

import json
import tempfile
import unittest
from pathlib import Path

from workbench.domain import DomainError
from workbench.transcripts import review_transcript, transcribe_offline
from workbench.story_checks import review_story


class DialogueTests(unittest.TestCase):
    def test_negation_number_missing_extra_and_unknown_speaker_are_clues(self):
        expected = [
            {"text": "不要交出三把钥匙", "speaker_id": "a", "start_ms": 0, "end_ms": 1000},
            {"text": "等我回来", "speaker_id": "b", "start_ms": 1000, "end_ms": 2000},
        ]
        actual = [
            {"text": "交出两把钥匙", "speaker_id": "a", "start_ms": 0, "end_ms": 1000},
            {"text": "等我回来", "speaker_id": "unknown", "start_ms": 1000, "end_ms": 2000},
            {"text": "门开了", "speaker_id": "a", "start_ms": 2000, "end_ms": 2500},
        ]
        findings = review_transcript(expected, actual)["findings"]
        self.assertTrue({"negation_difference", "number_difference", "unknown_speaker", "extra_speech"} <= {f["kind"] for f in findings})
        self.assertTrue(all(f["status"] == "machine_clue" for f in findings))

    def test_similar_sound_is_uncertain_and_originals_remain(self):
        expected = [{"text": "我要开门", "speaker_id": "a", "start_ms": 0, "end_ms": 900}]
        actual = [{"text": "我要凯门", "speaker_id": "a", "start_ms": 0, "end_ms": 900}]
        result = review_transcript(expected, actual)
        self.assertEqual(expected, result["expected"])
        self.assertEqual(actual, result["actual"])
        self.assertEqual("uncertain_wording", result["findings"][0]["kind"])
        self.assertFalse(result["human_reviewed"])

    def test_offline_asr_requires_explicit_local_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(DomainError) as caught:
                transcribe_offline(root / "clip.wav", root / "missing.json")
            self.assertEqual("asr_not_configured", caught.exception.code)
            config = root / "asr.json"
            config.write_text(json.dumps({"model_path": str(root / "absent"), "runtime": "faster_whisper"}), encoding="utf-8")
            with self.assertRaises(DomainError) as caught:
                transcribe_offline(root / "clip.wav", config)
            self.assertEqual("asr_model_missing", caught.exception.code)

    def test_story_advice_is_located_and_does_not_assert_viewing(self):
        episode = {"id": "episode-1", "scenes": [{"id": "scene-1", "purpose": "", "location": "屋内", "shots": [{"id": "shot-1", "story_job": "", "action": "开门", "start_state": "", "end_state": "", "transition": ""}]}]}
        findings = review_story(episode)
        self.assertTrue(any(f["field"] == "purpose" and f["scene_id"] == "scene-1" for f in findings))
        self.assertTrue(any(f["field"] == "end_state" and f["shot_id"] == "shot-1" for f in findings))
        self.assertTrue(all(f["confidence"] == "structural" and f["limitation"] for f in findings))

    def test_story_flags_unexplained_time_transition_and_missing_choice(self):
        episode = {"id": "ep", "next_expectation": "谁来了", "scenes": [
            {"id": "s1", "purpose": "寻找线索", "location": "房间", "time_of_day": "夜", "shots": [{"id": "a", "story_job": "找物", "start_state": "空手", "action": "翻柜", "end_state": "拿到信", "transition": "推门", "motivation": "", "choice": ""}]},
            {"id": "s2", "purpose": "逃跑", "location": "房间", "time_of_day": "早", "shots": []},
        ]}
        findings = review_story(episode)
        self.assertTrue(any(f["scene_id"] == "s2" and f["field"] == "transition" for f in findings))
        self.assertTrue(any(f["shot_id"] == "a" and f["field"] == "motivation" for f in findings))
        self.assertTrue(any(f["shot_id"] == "a" and f["field"] == "choice" for f in findings))


if __name__ == "__main__":
    unittest.main()
