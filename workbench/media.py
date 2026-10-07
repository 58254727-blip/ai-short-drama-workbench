"""CPU FFmpeg probing, full decode and accurate source trims."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

from .domain import DomainError, require


def _tool(name):
    executable = os.environ.get(f"WORKBENCH_{name.upper()}") or shutil.which(name)
    require(bool(executable), "tool_unavailable", 503, f"{name} 不可用")
    return executable


def _run(args, timeout=180):
    try:
        return subprocess.run(args, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DomainError("media_tool_error", 502, f"媒体工具运行失败: {exc}") from exc


def _path(path, root, *, output=False):
    value = Path(path)
    require(root is not None, "data_root_required", 400, "需要数据根目录")
    base = Path(root).resolve(strict=True)
    try:
        resolved = value.resolve(strict=not output)
        resolved.relative_to(base)
    except (OSError, ValueError):
        raise DomainError("path_outside_data_root", 400, "媒体路径不在数据根目录内") from None
    require(resolved != base, "invalid_path", 400, "媒体路径不能是数据根目录")
    if output:
        require(not value.is_symlink() and not value.is_junction() and not value.exists(), "output_conflict", 409, "输出文件已存在或是链接")
        parent = value.parent.resolve()
        require(parent.is_relative_to(base), "path_outside_data_root", 400, "输出目录不在数据根目录内")
    else:
        require(value.is_file() and not value.is_symlink() and not value.is_junction(), "invalid_source", 400, "媒体源文件不可用")
    return resolved


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as media:
        while chunk := media.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_output(destination, data_root):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".media-", suffix=destination.suffix or ".mp4", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        _path(temporary, data_root)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_output(temporary, destination):
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise DomainError("output_conflict", 409, "输出文件已存在") from None


def probe(path: Path, *, data_root: Path) -> dict:
    source = _path(path, data_root)
    result = _run([_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,time_base,sample_rate,channels", "-of", "json", str(source)], timeout=30)
    require(result.returncode == 0, "probe_failed", 422, result.stderr[-1000:] or "无法读取媒体")
    try:
        raw = json.loads(result.stdout)
        streams = raw["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        duration_ms = round(float(raw["format"]["duration"]) * 1000)
        fps = video.get("avg_frame_rate") or video.get("r_frame_rate")
        fraction = Fraction(fps)
        require(duration_ms > 0 and fraction > 0, "invalid_media", 422, "媒体时长或帧率无效")
        reported_frames = video.get("nb_frames")
        frames = int(reported_frames) if reported_frames and reported_frames != "N/A" else None
    except (KeyError, StopIteration, ValueError, ZeroDivisionError, TypeError) as exc:
        raise DomainError("invalid_media", 422, "媒体缺少可用视频流或元数据") from exc
    return {"path": str(source), "sha256": _sha(source), "duration_ms": duration_ms, "width": video.get("width"), "height": video.get("height"), "fps": f"{fraction.numerator}/{fraction.denominator}", "frame_count": frames, "frame_count_kind": "reported" if frames is not None else "unknown", "has_video": True, "has_audio": audio is not None, "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name") if audio else None, "video_time_base": video.get("time_base"), "audio_sample_rate": audio.get("sample_rate") if audio else None, "audio_channels": audio.get("channels") if audio else None}


def decode_check(path: Path, *, data_root: Path) -> dict:
    source = _path(path, data_root)
    result = _run([_tool("ffmpeg"), "-v", "error", "-xerror", "-i", str(source), "-map", "0:v?", "-map", "0:a?", "-sn", "-f", "null", "-"], timeout=300)
    return {"decoded": result.returncode == 0, "exit_code": result.returncode, "errors": result.stderr[-2000:]}


def _speech_guard(start, end, speech_ranges):
    for left, right in speech_ranges:
        require(type(left) is int and type(right) is int and 0 <= left < right, "invalid_speech_range", 400, "对白范围无效")
        require(not (left < start < right or left < end < right), "speech_cut", 422, "切点落在对白范围内")


def trim(source: Path, in_ms: int, out_ms: int, dest: Path, *, data_root: Path, speech_ranges=()) -> dict:
    source = _path(source, data_root)
    destination = _path(dest, data_root, output=True)
    info = probe(source, data_root=data_root)
    require(type(in_ms) is int and type(out_ms) is int and 0 <= in_ms < out_ms <= info["duration_ms"], "invalid_cut", 400, "剪辑范围超出实际片长")
    _speech_guard(in_ms, out_ms, speech_ranges)
    temporary = _temporary_output(destination, data_root)
    try:
        args = [_tool("ffmpeg"), "-v", "error", "-i", str(source), "-ss", f"{in_ms/1000:.3f}", "-t", f"{(out_ms-in_ms)/1000:.3f}", "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(temporary)]
        result = _run(args)
        if result.returncode != 0:
            raise DomainError("trim_failed", 502, result.stderr[-1000:])
        checked = decode_check(temporary, data_root=data_root)
        if not checked["decoded"]:
            raise DomainError("decode_failed", 422, checked["errors"])
        output = probe(temporary, data_root=data_root)
        _publish_output(temporary, destination)
        return {**output, "path": str(destination), **checked, "method": "reencode", "source_sha256": info["sha256"], "human_reviewed": False}
    finally:
        temporary.unlink(missing_ok=True)
