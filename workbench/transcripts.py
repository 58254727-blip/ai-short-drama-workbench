"""Conservative dialogue comparison and explicitly local CPU transcription."""

import json
import re
from pathlib import Path

from .domain import DomainError, require

_NEGATIONS = {"不", "别", "没", "无", "未", "勿"}
_NUMBERS = set("零一二三四五六七八九十百千万两0123456789")


def _tokens(text, alphabet):
    return [character for character in text if character in alphabet]


def review_transcript(expected: list[dict], actual: list[dict]) -> dict:
    """Return machine clues; similar wording never certifies a misread."""
    require(isinstance(expected, list) and isinstance(actual, list), "invalid_transcript", 400, "原文和实录必须是列表")
    findings = []

    def flag(kind, expected_index=None, actual_index=None, detail=""):
        findings.append({"kind": kind, "expected_index": expected_index, "actual_index": actual_index, "detail": detail, "status": "machine_clue", "human_reviewed": False})

    for index in range(max(len(expected), len(actual))):
        if index >= len(expected):
            flag("extra_speech", None, index, "实录多于原文；请听审")
            continue
        if index >= len(actual):
            flag("missing_speech", index, None, "原文没有对应实录；请听审")
            continue
        source, heard = expected[index], actual[index]
        require(all(isinstance(line, dict) and isinstance(line.get("text"), str) and isinstance(line.get("speaker_id"), str) for line in (source, heard)), "invalid_transcript", 400, "对白行格式无效")
        if heard["speaker_id"] != source["speaker_id"] or heard["speaker_id"].lower() in {"unknown", "?", ""}:
            flag("unknown_speaker", index, index, "说话人需要人工确认")
        original, observed = source["text"], heard["text"]
        if _tokens(original, _NEGATIONS) != _tokens(observed, _NEGATIONS):
            flag("negation_difference", index, index, "否定词不一致，需听审")
        if _tokens(original, _NUMBERS) != _tokens(observed, _NUMBERS):
            flag("number_difference", index, index, "数字不一致，需听审")
        if original != observed and not any(f["expected_index"] == index and f["kind"] in {"negation_difference", "number_difference"} for f in findings):
            flag("uncertain_wording", index, index, "文字不一致，可能是近音或识别偏差")
    return {"expected": expected, "actual": actual, "findings": findings, "human_reviewed": False}


def transcribe_offline(audio_path: Path, config_path: Path) -> list[dict]:
    """Optional faster-whisper runtime, explicit local model and CPU only."""
    config = Path(config_path)
    require(config.is_file(), "asr_not_configured", 503, "请在本地配置文件填写离线 ASR 模型路径")
    try:
        settings = json.loads(config.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError) as error:
        raise DomainError("asr_invalid_config", 400, "离线 ASR 配置文件无效") from error
    require(isinstance(settings, dict) and settings.get("runtime") == "faster_whisper", "asr_invalid_config", 400, "仅支持显式 faster_whisper 离线运行时")
    model_path = Path(settings.get("model_path", ""))
    require(model_path.is_absolute() and model_path.is_dir(), "asr_model_missing", 503, "请指定已存在的本地模型目录")
    source = Path(audio_path)
    require(source.is_file(), "asr_audio_missing", 400, "音频文件不存在")
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise DomainError("asr_dependency_missing", 503, "请在本地安装 faster-whisper 运行时") from error
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
    segments, _ = model.transcribe(str(source), beam_size=1, vad_filter=False)
    return [{"start_ms": round(segment.start * 1000), "end_ms": round(segment.end * 1000), "text": segment.text.strip(), "speaker_id": "unknown", "status": "machine_clue"} for segment in segments]
