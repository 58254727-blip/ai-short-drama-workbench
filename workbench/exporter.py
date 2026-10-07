"""Verified selected timeline to actual video, audio and optional subtitles."""

from pathlib import Path

from .assets import binary_available
from .domain import DomainError, require
from .media import _path, _publish_output, _run, _speech_guard, _temporary_output, _tool, decode_check, probe


def _static_suspected(path, root):
    result = _run([_tool("ffmpeg"), "-v", "error", "-i", str(path), "-vf", "fps=2,scale=16:9", "-frames:v", "8", "-f", "framemd5", "-"], timeout=60)
    if result.returncode:
        return None
    hashes = [line.split(",")[-1].strip() for line in result.stdout.splitlines() if line and not line.startswith("#")]
    return len(set(hashes)) <= 1 if len(hashes) >= 2 else None


def export_episode(scope: dict, timeline: list[dict], subtitle_path: Path | None) -> dict:
    """Scope: store, project_id, episode_id, output_path, width, height, fps."""
    require(isinstance(scope, dict) and isinstance(timeline, list) and bool(timeline), "invalid_timeline", 400, "时间轴不能为空")
    try:
        store = scope["store"]
        project_id = scope["project_id"]
        episode_id = scope["episode_id"]
        root = store.data_root
        output = _path(scope["output_path"], root, output=True)
        width, height, fps = (scope[key] for key in ("width", "height", "fps"))
    except KeyError as exc:
        raise DomainError("invalid_scope", 400, f"导出范围缺少 {exc}") from exc
    require(all(type(v) is int and v > 0 for v in (width, height, fps)) and width % 2 == height % 2 == 0, "invalid_scope", 400, "画幅或帧率无效")
    require(store.get_episode(episode_id)["project_id"] == store.get_project(project_id)["id"], "ownership_conflict", 409, "分集不属于作品")
    subtitle = _path(subtitle_path, root) if subtitle_path is not None else None
    if subtitle is not None:
        try:
            subtitle.read_text(encoding="utf-8", errors="strict")
        except UnicodeError as exc:
            raise DomainError("invalid_subtitle", 400, "字幕必须是 UTF-8") from exc
    validated = []
    for item in timeline:
        require(isinstance(item, dict), "invalid_timeline", 400, "时间轴项无效")
        try:
            shot = store.get_shot(item["shot_id"])
            asset = store.get_asset(item["source_asset_id"])
            start, end = item["in_ms"], item["out_ms"]
        except KeyError as exc:
            raise DomainError("invalid_timeline", 400, f"时间轴项缺少 {exc}") from exc
        require(shot["episode_id"] == episode_id and shot["selected_candidate_id"] == asset["id"] and asset["project_id"] == project_id and asset["kind"] == "video", "ownership_conflict", 409, "镜头与已选视频不匹配")
        require(binary_available(root, asset["storage_key"]) and asset["sha256"] == asset["storage_key"], "asset_hash_mismatch", 409, "素材文件缺失或哈希变化")
        source = _path(root / "assets" / asset["storage_key"], root)
        info = probe(source, data_root=root)
        require(info["sha256"] == asset["sha256"], "asset_hash_mismatch", 409, "素材哈希变化")
        require(info["has_audio"], "audio_missing", 422, "源视频缺少原音轨，需人工处理")
        require(type(start) is int and type(end) is int and 0 <= start < end <= info["duration_ms"], "invalid_cut", 400, "切点超出源视频")
        if item.get("speech_start_ms") is not None or item.get("speech_end_ms") is not None:
            require(item.get("speech_start_ms") is not None and item.get("speech_end_ms") is not None, "invalid_speech_range", 400, "对白范围必须有起止")
            _speech_guard(start, end, [(item["speech_start_ms"], item["speech_end_ms"])])
        checked = decode_check(source, data_root=root)
        require(checked["decoded"], "decode_failed", 422, checked["errors"])
        validated.append((source, start, end, asset["sha256"], _static_suspected(source, root)))
    temporary = _temporary_output(output, root)
    try:
        return _render(validated, subtitle, width, height, fps, temporary, output, root)
    finally:
        temporary.unlink(missing_ok=True)


def _render(validated, subtitle, width, height, fps, temporary, output, root):
    args = [_tool("ffmpeg"), "-v", "error"]
    for source, _, _, _, _ in validated:
        args += ["-i", str(source)]
    if subtitle is not None:
        args += ["-sub_charenc", "UTF-8", "-i", str(subtitle)]
    filters = []
    for index, (_, start, end, _, _) in enumerate(validated):
        begin, finish = start / 1000, end / 1000
        filters.append(f"[{index}:v:0]trim=start={begin:.3f}:end={finish:.3f},setpts=PTS-STARTPTS,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1[v{index}]")
        filters.append(f"[{index}:a:0]atrim=start={begin:.3f}:end={finish:.3f},asetpts=PTS-STARTPTS,aresample=48000[a{index}]")
    inputs = "".join(f"[v{i}][a{i}]" for i in range(len(validated)))
    filters.append(f"{inputs}concat=n={len(validated)}:v=1:a=1[v][a]")
    args += ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]"]
    if subtitle is not None:
        args += ["-map", f"{len(validated)}:s:0", "-c:s", "mov_text"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-y", str(temporary)]
    result = _run(args, timeout=600)
    if result.returncode:
        raise DomainError("export_failed", 502, result.stderr[-2000:])
    checked = decode_check(temporary, data_root=root)
    if not checked["decoded"]:
        raise DomainError("decode_failed", 422, checked["errors"])
    actual = probe(temporary, data_root=root)
    _publish_output(temporary, output)
    return {**actual, "path": str(output), **checked, "method": "reencode_concat", "source_versions": [entry[3] for entry in validated], "static_review_flags": [entry[4] for entry in validated], "subtitle_included": subtitle is not None, "human_reviewed": False, "machine_qc": "decoded", "review_pending": ["画面内容", "对白听审", "表演与连续性", "字幕校对"]}
