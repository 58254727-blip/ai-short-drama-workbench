"""Loopback-only same-origin HTTP surface for 镜序."""

import argparse
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from .archive import archive_project, restore_archive
from .assets import KINDS as ASSET_KINDS
from .domain import DomainError, require
from .exporter import export_episode
from .media import probe
from .queue import Queue
from .review import ManualReviewService
from .story_checks import review_story
from .store import Store
from .subtitles import SubtitleService, write_srt
from .timeline import TimelineService
from .transcripts import review_transcript, transcribe_offline
from .worker import Worker

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_JSON = 2 * 1024 * 1024
MAX_UPLOAD = 8 * 1024 * 1024 * 1024
MAX_RESTORE = 64 * 1024 * 1024 * 1024


def _id(value):
    try:
        require(str(UUID(value)) == value, "invalid_id", 400, "记录 ID 无效")
    except (TypeError, ValueError, AttributeError):
        raise DomainError("invalid_id", 400, "记录 ID 无效") from None
    return value


def _fields(body, allowed, required=()):
    require(isinstance(body, dict) and set(body) <= set(allowed) and set(required) <= set(body),
            "invalid_payload", 400, "字段缺失或不受支持")
    return body


def _public_job(job):
    visible = {key: value for key, value in job.items() if key not in {"claim_token", "lease_until", "source_snapshot"}}
    visible["payload"] = {key: value for key, value in job["payload"].items() if key in {"asset_id", "first_frame_asset_id", "plan_revision", "strategy", "subtitles", "width", "height", "fps"}}
    return visible


def _validate_upload_media(path, kind):
    if kind not in {"video", "image", "audio"}:
        return
    with Path(path).open("rb") as stream:
        head = stream.read(16)
    image = head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff") or head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    video = head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3")
    audio = head.startswith(b"RIFF") and head[8:12] == b"WAVE" or head.startswith(b"ID3") or head.startswith(b"fLaC") or head.startswith(b"OggS") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
    require({"image": image, "video": video, "audio": audio}[kind], "invalid_media", 422, "上传文件与素材类型不符")


class App:
    def __init__(self, data_root):
        self.root = Path(data_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / "workbench.sqlite", self.root)
        self.queue = Queue(self.store)
        self.timeline = TimelineService(self.store)
        self.subtitles = SubtitleService(self.store)
        self.review = ManualReviewService(self.store)
        self.queue.recover()
        self.config = self._config()
        self.workers = []
        self.worker_threads = []
        self.worker_lock = threading.Lock()
        self.reconcile_lock = threading.Lock()

    def _config(self):
        path = self.root / "operator-config.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            require(isinstance(value, dict), "invalid_config", 400, "操作员配置必须是对象")
            return value
        except (ValueError, UnicodeError) as error:
            raise DomainError("invalid_config", 400, "操作员配置无效") from error

    def status(self):
        video = self.config.get("video")
        asr = self.config.get("asr")
        text = self.config.get("text")
        asr_status = "unconfigured"
        if isinstance(asr, dict) and asr.get("config_path"):
            config_path = Path(asr["config_path"])
            if not config_path.is_file():
                asr_status = "config_missing"
            else:
                try:
                    settings = json.loads(config_path.read_text(encoding="utf-8"))
                    model = Path(settings.get("model_path", ""))
                    if settings.get("runtime") != "faster_whisper" or not model.is_absolute() or not model.is_dir():
                        asr_status = "model_missing"
                    elif importlib.util.find_spec("faster_whisper") is None:
                        asr_status = "dependency_missing"
                    else:
                        asr_status = "configured"
                except (ValueError, UnicodeError, OSError, AttributeError, TypeError):
                    asr_status = "invalid_config"
        return {"video": "configured" if isinstance(video, dict) and all(video.get(k) for k in ("endpoint", "workflow", "bindings")) else "unconfigured",
                "asr": asr_status,
                "text": "configured" if isinstance(text, dict) and all(text.get(k) for k in ("endpoint", "model")) else "unconfigured",
                "cpu_workers": 2, "gpu_workers": 1 if isinstance(video, dict) and all(video.get(k) for k in ("endpoint", "workflow", "bindings")) else 0,
                "workers_active": bool(self.workers), "video_requires_first_frame": bool(isinstance(video, dict) and video.get("first_frame_binding"))}

    def h3_values(self, shot):
        cfg = self.config["video"]
        values = cfg.get("values", {})
        mappings = cfg.get("input_map", {})
        require(isinstance(values, dict) and isinstance(mappings, dict), "invalid_config", 400, "视频输入配置无效")
        allowed = {"story_job", "start_state", "action", "end_state", "transition", "dialogue_text", "generation_duration_seconds"}
        require(set(values) <= set(cfg["bindings"]) and set(mappings) <= set(cfg["bindings"]) and all(source in allowed for source in mappings.values()), "invalid_config", 400, "视频输入映射无效")
        require(cfg.get("first_frame_binding") not in set(values) | set(mappings), "invalid_config", 400, "首帧绑定与其他输入冲突")
        result = values.copy()
        for binding, source in mappings.items():
            if source == "dialogue_text":
                result[binding] = "\n".join(f"{line['speaker_id']}：{line['text']}" for line in shot["dialogue"])
                require(bool(result[binding].strip()), "missing_input", 400, "原生对白未填写")
            elif source == "generation_duration_seconds":
                result[binding] = shot["generation_duration_ms"] / 1000
            else:
                result[binding] = shot[source]
        return result

    def _scope(self, project_id, episode_id=None, shot_id=None):
        project = self.store.get_project(_id(project_id))
        if episode_id is not None:
            episode = self.store.get_episode(_id(episode_id))
            require(episode["project_id"] == project["id"], "ownership_conflict", 409, "分集不属于当前作品")
        if shot_id is not None:
            shot = self.store.get_shot(_id(shot_id))
            require(shot["episode_id"] == episode_id, "ownership_conflict", 409, "镜头不属于当前分集")

    def _episode(self, episode_id):
        return self.store.get_episode(_id(episode_id))

    def _shot(self, episode_id, shot_id):
        shot = self.store.get_shot(_id(shot_id))
        require(shot["episode_id"] == episode_id, "ownership_conflict", 409, "镜头不属于当前分集")
        return shot

    def _asset(self, project_id, asset_id):
        asset = self.store.get_asset(_id(asset_id))
        require(asset["project_id"] == project_id, "ownership_conflict", 409, "素材不属于当前作品")
        return asset

    def _job(self, project_id, job_id):
        job = self.queue.get_job(_id(job_id))
        require(job["project_id"] == project_id, "ownership_conflict", 409, "任务不属于当前作品")
        return job

    def _story(self, episode_id):
        episode = self._episode(episode_id)
        scenes = self.store.list_scenes(episode_id)
        shots = self.store.list_shots(episode_id)
        grouped = [{**scene, "shots": [shot for shot in shots if shot["scene_id"] == scene["id"]]} for scene in scenes]
        unassigned = [shot for shot in shots if shot["scene_id"] is None]
        if unassigned:
            grouped.append({"id": None, "title": "未指定场景", "purpose": "", "location": "", "shots": unassigned})
        return review_story({**episode, "scenes": grouped})

    def _qc(self, episode_id):
        return self.review.list(episode_id)

    def _save_qc(self, episode_id, shot_id, body):
        _fields(body, {"asset_id", "verdict", "note"}, {"asset_id", "verdict", "note"})
        shot = self._shot(episode_id, shot_id)
        asset = self._asset(self._episode(episode_id)["project_id"], _id(body["asset_id"]))
        require(shot["selected_candidate_id"] == asset["id"], "ownership_conflict", 409, "只能校核当前选片")
        require(body["verdict"] in ("pass", "revise", "reject") and isinstance(body["note"], str), "invalid_payload", 400, "人工校核状态无效")
        return self.review.save(episode_id, shot_id, asset["id"], body["verdict"], body["note"])

    def _export(self, project_id, episode_id, body, export_id=None):
        _fields(body, {"subtitles", "width", "height", "fps"}, {"subtitles"})
        self._scope(project_id, episode_id)
        timeline = self.timeline.get_timeline(episode_id)
        require(timeline["status"] == "ready", "timeline_missing", 409, "请先保存实际已选时间轴")
        captions = self.subtitles.get_cues(episode_id)
        include = body["subtitles"]
        require(type(include) is bool, "invalid_payload", 400, "字幕选择无效")
        if include:
            require(captions["status"] == "ready" and bool(captions["cues"]), "captions_not_ready", 409, "字幕未就绪")
        output_dir = self.root / "exports"
        output_dir.mkdir(exist_ok=True)
        export_id = _id(export_id) if export_id else str(uuid4())
        output = output_dir / f"{export_id}.mp4"
        srt = output_dir / f"{export_id}.srt" if include else None
        report_path = output_dir / f"{export_id}.json"
        if output.exists() or report_path.exists():
            require(output.is_file() and report_path.is_file(), "export_incomplete", 409, "上次导出中断，成片与报告需人工核对")
            try:
                prior = json.loads(report_path.read_text(encoding="utf-8"))
                with output.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except (OSError, ValueError, UnicodeError):
                raise DomainError("export_incomplete", 409, "上次导出记录不可核对") from None
            require(prior.get("project_id") == project_id and prior.get("episode_id") == episode_id and prior.get("sha256") == digest and prior.get("subtitle_included") is include,
                    "export_incomplete", 409, "上次导出与当前请求不一致")
            require(not include or srt.is_file(), "export_incomplete", 409, "上次导出字幕缺失")
            return prior
        try:
            if srt:
                write_srt(captions["cues"], srt)
            result = export_episode({"store": self.store, "project_id": project_id, "episode_id": episode_id, "output_path": output,
                                     "width": body.get("width", 1280), "height": body.get("height", 720), "fps": body.get("fps", 24)}, timeline["items"], srt)
            report = {k: v for k, v in result.items() if k != "path"}
            report.update({"id": export_id, "project_id": project_id, "episode_id": episode_id,
                           "video_url": f"/api/projects/{project_id}/exports/{export_id}.mp4", "srt_url": f"/api/projects/{project_id}/exports/{export_id}.srt" if srt else None,
                           "missing_caption_flag": not include, "manual_qc": self._qc(episode_id)})
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            return report
        except Exception:
            output.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            if srt:
                srt.unlink(missing_ok=True)
            raise

    def _cpu_handlers(self):
        def probe_job(_, job):
            asset = self._asset(job["project_id"], job["payload"]["asset_id"])
            require(asset["binary_available"], "asset_binary_missing", 409, "素材文件缺失")
            return {key: value for key, value in probe(self.root / "assets" / asset["storage_key"], data_root=self.root).items() if key != "path"}

        def asr_job(_, job):
            config = self.config.get("asr", {})
            require(self.status()["asr"] == "configured", "asr_unconfigured", 503, "离线语音模型未就绪")
            asset = self._asset(job["project_id"], job["payload"]["asset_id"])
            require(asset["binary_available"], "asset_binary_missing", 409, "声音素材缺失")
            return {"segments": transcribe_offline(self.root / "assets" / asset["storage_key"], Path(config["config_path"])), "human_reviewed": False}

        def export_job(_, job):
            require(self.store.get_episode(job["episode_id"])["revision"] == job["source_revision"], "revision_conflict", 409, "排队后分集已变化，请重新检查时间轴和字幕")
            return self._export(job["project_id"], job["episode_id"], {key: value for key, value in job["payload"].items() if key in {"subtitles", "width", "height", "fps"}}, job["id"])

        def text_job(_, job):
            from .adapters.text import TextAdapter
            config = self.config.get("text", {})
            adapter = TextAdapter(config.get("endpoint"), config.get("model"), config.get("secret"))
            snapshot = job["source_snapshot"]
            result = adapter.generate([{"role": "system", "content": "仅提出镜头文本建议，不自动采纳。"}, {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)}])
            return result

        return {"probe": probe_job, "asr": asr_job, "export": export_job, "text": text_job}

    def start_workers(self):
        with self.worker_lock:
            if self.workers: return
            for _ in range(2):
                worker = Worker(self.queue, "cpu", self._cpu_handlers())
                thread = threading.Thread(target=worker.run_forever, daemon=True)
                thread.start()
                self.workers.append(worker)
                self.worker_threads.append(thread)
            if self.status()["video"] == "configured":
                from .adapters.comfy import ComfyAdapter
                from .workflows import make_h3_handler
                cfg = self.config["video"]
                worker = Worker(self.queue, "gpu", {"h3": make_h3_handler(self.queue, self.store, ComfyAdapter(cfg["endpoint"], 20), cfg["workflow"], cfg["bindings"], first_frame_binding=cfg.get("first_frame_binding"))})
                thread = threading.Thread(target=worker.run_forever, daemon=True)
                thread.start()
                self.workers.append(worker)
                self.worker_threads.append(thread)

    def stop_workers(self):
        with self.worker_lock:
            for worker in self.workers: worker.stop()
            self.workers.clear()
            threads = self.worker_threads
            self.worker_threads = []
        for thread in threads: thread.join(timeout=1)

    def resolve_known(self, job, adapter):
        """Complete only a known external execution with provider timestamp proof."""
        from .workflows import collect_candidates
        with self.reconcile_lock:
            current = self.queue.get_job(job["id"])
            require(current["state"] == "needs_reconcile" and current["external_id"], "invalid_state", 409, "外部结果未处于可核对状态")
            token = current["claim_token"]
            self.queue.renew_lease(current["id"], token)
            observed = adapter.status(current["external_id"])
            require(observed["state"] in ("succeeded", "failed") and observed.get("started_at") and observed.get("finished_at"), "evidence_missing", 409, "外部尚无完整执行开始与结束证据")
            if observed["state"] == "failed":
                self.queue.record_execution_started(current["id"], observed["started_at"], token)
                self.queue.fail(current["id"], "provider_execution_failed", "外部执行失败", token, execution_finished_at=observed["finished_at"])
            else:
                done = threading.Event()
                def pulse():
                    while not done.wait(20):
                        try: self.queue.renew_lease(current["id"], token)
                        except DomainError: return
                thread = threading.Thread(target=pulse, daemon=True)
                thread.start()
                try:
                    files = adapter.collect(current["external_id"])
                    candidates = collect_candidates(self.store, current, current["external_id"], files)
                    self.queue.record_execution_started(current["id"], observed["started_at"], token)
                    self.queue.finish(current["id"], {"prompt_id": current["external_id"], "candidate_assets": candidates, "execution_finished_at": observed["finished_at"]}, token, execution_finished_at=observed["finished_at"])
                finally:
                    done.set(); thread.join()
            return self.queue.get_job(current["id"])


class Handler(BaseHTTPRequestHandler):
    server_version = "Jingxu/1"

    def log_message(self, format, *args):
        pass

    def _reply(self, status, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _security(self):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        require(host in allowed, "forbidden_host", 403, "访问主机不受信任")
        origin = self.headers.get("Origin")
        if origin is not None:
            require(origin in {"http://" + h for h in allowed}, "forbidden_origin", 403, "跨站来源不允许")
        site = self.headers.get("Sec-Fetch-Site")
        require(site in (None, "same-origin", "none"), "forbidden_origin", 403, "跨站请求不允许")
        if self.command in ("POST", "PUT", "DELETE"):
            require(self.headers.get("X-Jingxu-Request") == "1", "forbidden_request", 403, "请求标记缺失")

    def _body(self):
        require(self.headers.get("Content-Type", "").split(";")[0] == "application/json", "invalid_content_type", 415, "请发送 JSON")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            require(0 < size <= MAX_JSON, "payload_too_large", 413, "请求体过大或为空")
            return json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            raise DomainError("invalid_json", 400, "JSON 格式无效") from None

    def _path(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        require("\\" not in path and ".." not in path.split("/") and "\x00" not in path and not parsed.query, "invalid_path", 400, "请求路径无效")
        return path

    def _send_file(self, path, media_type):
        require(path.is_file() and not path.is_symlink(), "not_found", 404, "文件不存在")
        size = path.stat().st_size
        start, end = 0, size - 1
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", header)
            require(match is not None, "invalid_range", 416, "读取范围无效")
            start = int(match[1]); end = int(match[2]) if match[2] else end
            require(0 <= start <= end < size, "invalid_range", 416, "读取范围无效")
        self.send_response(206 if header else 200)
        self.send_header("Content-Type", media_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _run(self):
        self._security()
        path = self._path()
        if not path.startswith("/api/"):
            require(self.command == "GET", "method_not_allowed", 405, "此路径只可读取")
            target = "index.html" if path == "/" else path.lstrip("/")
            require(target in {"index.html", "styles.css", "app.js", "api.js", "state.js", "views.js", "editor.js", "production.js", "assets/rain-alley-demo.png"}, "not_found", 404, "页面不存在")
            return self._send_file(WEB_ROOT / target, mimetypes.guess_type(target)[0] or "application/octet-stream")
        app = self.server.app
        parts = path.strip("/").split("/")[1:]
        method = self.command
        body = self._body() if method in ("POST", "PUT") and not (parts[-1] in ("upload", "restore")) else None
        if isinstance(body, dict) and "revision" in body:
            require(type(body["revision"]) is int and body["revision"] >= 1, "invalid_revision", 400, "版本号必须是正整数")
        if parts == ["settings", "status"] and method == "GET":
            return self._reply(200, app.status())
        if parts == ["queue", "start"] and method == "POST":
            _fields(body, set())
            app.start_workers()
            return self._reply(200, app.status())
        if parts == ["queue", "stop"] and method == "POST":
            _fields(body, set())
            app.stop_workers()
            return self._reply(200, app.status())
        if parts == ["projects"]:
            if method == "GET": return self._reply(200, app.store.list_projects())
            if method == "POST": return self._reply(201, app.store.create_project(_fields(body, {"title"}, {"title"})["title"]))
        if len(parts) >= 2 and parts[0] == "projects":
            project_id = _id(parts[1]); app.store.get_project(project_id)
            if len(parts) == 2 and method == "GET": return self._reply(200, app.store.get_project(project_id))
            if parts[2:] == ["episodes"]:
                if method == "GET": return self._reply(200, app.store.list_episodes(project_id))
                if method == "POST": return self._reply(201, app.store.create_episode(project_id, _fields(body, {"title"}, {"title"})["title"]))
            if len(parts) >= 4 and parts[2] == "episodes":
                episode_id = _id(parts[3]); app._scope(project_id, episode_id)
                if parts[4:] == ["shots"] and method == "POST": return self._reply(201, app.store.save_shot(episode_id, body))
            if parts[2:] == ["assets"] and method == "GET": return self._reply(200, app.store.list_assets(project_id))
            if len(parts) == 4 and parts[2] == "assets" and method == "GET": return self._reply(200, app._asset(project_id, _id(parts[3])))
            if len(parts) == 5 and parts[2] == "assets" and parts[4] == "probe" and method == "GET":
                asset = app._asset(project_id, _id(parts[3]))
                require(asset["kind"] == "video" and asset["binary_available"], "invalid_media", 422, "实际视频不可用")
                return self._reply(200, {key: value for key, value in probe(app.root / "assets" / asset["storage_key"], data_root=app.root).items() if key != "path"})
            if len(parts) == 5 and parts[2] == "assets" and parts[4] == "media" and method == "GET":
                asset = app._asset(project_id, _id(parts[3]))
                media = app.root / "assets" / asset["storage_key"]
                require(media.is_file(), "asset_binary_missing", 404, "素材文件缺失")
                with media.open("rb") as stream: signature = stream.read(16)
                media_type = ("image/png" if signature.startswith(b"\x89PNG") else "image/jpeg" if signature.startswith(b"\xff\xd8") else
                              "image/webp" if signature.startswith(b"RIFF") and signature[8:12] == b"WEBP" else
                              "video/mp4" if signature[4:8] == b"ftyp" else "video/webm" if signature.startswith(b"\x1a\x45\xdf\xa3") else
                              "audio/wav" if signature.startswith(b"RIFF") and signature[8:12] == b"WAVE" else "application/octet-stream")
                return self._send_file(media, media_type)
            if parts[2:] == ["assets", "upload"] and method == "POST":
                kind = self.headers.get("X-Asset-Kind", "")
                rights_text = self.headers.get("X-Asset-Rights", "")
                require(kind in ASSET_KINDS, "invalid_kind", 400, "素材类型无效")
                try: rights = json.loads(rights_text)
                except ValueError: raise DomainError("invalid_rights", 400, "权利记录无效") from None
                require(isinstance(rights, dict) and isinstance(rights.get("source"), str) and rights["source"].strip(), "invalid_rights", 400, "请填写素材来源")
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= MAX_UPLOAD, "payload_too_large", 413, "素材大小无效")
                staging = app.root / "staging"; staging.mkdir(exist_ok=True)
                fd, name = tempfile.mkstemp(prefix="upload-", dir=staging)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        remaining = size
                        while remaining:
                            block = self.rfile.read(min(1024 * 1024, remaining))
                            require(block, "invalid_upload", 400, "上传中断")
                            stream.write(block); remaining -= len(block)
                    _validate_upload_media(Path(name), kind)
                    return self._reply(201, app.store.import_asset(project_id, Path(name), kind, rights))
                finally: Path(name).unlink(missing_ok=True)
            if parts[2:] == ["jobs"] and method == "GET": return self._reply(200, [_public_job(job) for job in app.queue.list_jobs(project_id)])
            if parts[2:] == ["exports"] and method == "GET":
                reports = []
                for report_file in sorted((app.root / "exports").glob("*.json")) if (app.root / "exports").is_dir() else []:
                    try:
                        record = json.loads(report_file.read_text(encoding="utf-8"))
                        if record.get("project_id") == project_id: reports.append(record)
                    except (ValueError, UnicodeError, OSError):
                        continue
                return self._reply(200, reports)
            if len(parts) >= 4 and parts[2] == "jobs":
                job = app._job(project_id, _id(parts[3]))
                if len(parts) == 4 and method == "GET": return self._reply(200, _public_job(job))
                if parts[4:] == ["cancel"] and method == "POST": return self._reply(200, _public_job(app.queue.cancel_queued(job["id"])))
                if parts[4:] == ["retry"] and method == "POST":
                    _fields(body, {"plan_revision", "strategy"}, {"plan_revision", "strategy"})
                    return self._reply(201, _public_job(app.queue.retry(job["id"], {**job["payload"], **body})))
                if parts[4:] == ["reconcile"] and method == "GET":
                    require(job["state"] == "needs_reconcile", "invalid_state", 409, "任务目前无需外部核对")
                    if not job["external_id"]:
                        return self._reply(200, {"status": "manual_pending", "message": "外部任务 ID 未知；请在原服务人工核实，不能重投"})
                    require(app.status()["video"] == "configured", "video_unconfigured", 503, "原视频服务配置不可用，需人工核对")
                    from .adapters.comfy import ComfyAdapter
                    adapter = ComfyAdapter(app.config["video"]["endpoint"], 20)
                    observed = adapter.status(job["external_id"])
                    return self._reply(200, {"status": "observed", "external_state": observed["state"], "started_at": observed.get("started_at"), "finished_at": observed.get("finished_at"), "message": "只读外部核对；未改变本地任务状态"})
                if parts[4:] == ["resolve"] and method == "POST":
                    _fields(body, set())
                    require(job["state"] == "needs_reconcile" and job["external_id"], "invalid_state", 409, "未知外部 ID 只能人工核对")
                    require(app.status()["video"] == "configured", "video_unconfigured", 503, "原视频服务配置不可用")
                    from .adapters.comfy import ComfyAdapter
                    adapter = ComfyAdapter(app.config["video"]["endpoint"], 20)
                    return self._reply(200, _public_job(app.resolve_known(job, adapter)))
                if parts[4:] == ["adopt"] and method == "POST":
                    _fields(body, {"revision"}, {"revision"})
                    require(job["kind"] == "text" and job["state"] == "succeeded" and isinstance(job.get("result"), dict), "invalid_state", 409, "任务没有可采纳的文本建议")
                    require(job["shot_id"] is not None and body["revision"] == job["source_revision"], "revision_conflict", 409, "源镜头已变化，请重新审核建议")
                    suggestion = job["result"].get("suggestion")
                    require(isinstance(suggestion, str) and bool(suggestion.strip()), "invalid_suggestion", 400, "建议不是可采纳的镜头文本")
                    return self._reply(200, app.store.save_shot(job["episode_id"], {"id": job["shot_id"], "story_job": suggestion}, body["revision"]))
            if parts[2:] == ["archive"] and method == "POST":
                out = app.root / "archives"; out.mkdir(exist_ok=True)
                dest = out / f"{uuid4()}.zip"
                result = archive_project(app.store, project_id, dest)
                return self._reply(201, {"id": dest.stem, "url": f"/api/projects/{project_id}/archives/{dest.name}/download", "sha256": result["sha256"]})
            if len(parts) == 5 and parts[2] == "archives" and method == "GET":
                name = parts[3]; require(re.fullmatch(r"[0-9a-f-]{36}\.zip", name) and parts[4] == "download", "invalid_path", 400, "归档路径无效")
                archived = app.root / "archives" / name
                require(archived.is_file(), "not_found", 404, "备份不存在")
                try:
                    with zipfile.ZipFile(archived) as bundle:
                        owner = json.loads(bundle.read("metadata.json"))["project"]["id"]
                except (zipfile.BadZipFile, KeyError, ValueError, OSError):
                    raise DomainError("invalid_archive", 400, "备份不可读取") from None
                require(owner == project_id, "ownership_conflict", 409, "备份不属于当前作品")
                return self._send_file(archived, "application/zip")
            if len(parts) == 4 and parts[2] == "exports" and method == "GET":
                name = parts[3]; require(re.fullmatch(r"[0-9a-f-]{36}\.(mp4|srt|json)", name), "invalid_path", 400, "导出路径无效")
                report_file = app.root / "exports" / (name.split(".")[0] + ".json")
                require(report_file.is_file() and json.loads(report_file.read_text(encoding="utf-8"))["project_id"] == project_id, "not_found", 404, "导出不存在")
                return self._send_file(app.root / "exports" / name, mimetypes.guess_type(name)[0] or "application/octet-stream")
            if len(parts) == 5 and parts[2] == "episodes":
                episode_id = _id(parts[3]); app._scope(project_id, episode_id)
                if parts[4] == "export-jobs" and method == "POST":
                    _fields(body, {"subtitles", "width", "height", "fps"}, {"subtitles"})
                    require(type(body["subtitles"]) is bool, "invalid_payload", 400, "字幕选项无效")
                    require(app.timeline.get_timeline(episode_id)["status"] == "ready", "timeline_missing", 409, "请先保存实际已选时间轴")
                    if body["subtitles"]:
                        captions = app.subtitles.get_cues(episode_id)
                        require(captions["status"] == "ready" and bool(captions["cues"]), "captions_not_ready", 409, "字幕未就绪")
                    return self._reply(201, _public_job(app.queue.enqueue({"project_id": project_id, "episode_id": episode_id}, "export", {**body, "idempotent_local": True})))
        if parts == ["restore"] and method == "POST":
            size = int(self.headers.get("Content-Length", "0"))
            require(0 < size <= MAX_RESTORE, "payload_too_large", 413, "备份大小无效")
            staging = app.root / "staging"; staging.mkdir(exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="restore-", suffix=".zip", dir=staging)
            try:
                with os.fdopen(fd, "wb") as stream:
                    remaining = size
                    while remaining:
                        block = self.rfile.read(min(1024 * 1024, remaining))
                        require(block, "invalid_upload", 400, "上传中断")
                        stream.write(block); remaining -= len(block)
                return self._reply(201, restore_archive(app.store, Path(name)))
            finally: Path(name).unlink(missing_ok=True)
        if len(parts) >= 2 and parts[0] == "episodes":
            episode_id = _id(parts[1]); episode = app._episode(episode_id)
            if len(parts) == 2:
                if method == "GET": return self._reply(200, episode)
                if method == "PUT":
                    _fields(body, {"revision", "title", "script", "creative_notes"}, {"revision"})
                    return self._reply(200, app.store.update_episode(episode_id, {k: v for k, v in body.items() if k != "revision"}, body["revision"]))
            if parts[2:] == ["scenes"]:
                if method == "GET": return self._reply(200, app.store.list_scenes(episode_id))
                if method == "POST": return self._reply(201, app.store.create_scene(episode_id, body))
            if parts[2:] == ["shots"]:
                if method == "GET": return self._reply(200, app.store.list_shots(episode_id))
                if method == "POST": return self._reply(201, app.store.save_shot(episode_id, body))
            if len(parts) == 4 and parts[2] == "shots":
                shot = app._shot(episode_id, _id(parts[3]))
                if method == "GET": return self._reply(200, shot)
                if method == "PUT":
                    _fields(body, {"revision", "scene_id", "order", "story_job", "start_state", "action", "end_state", "transition", "dialogue", "screen_duration_ms", "generation_duration_ms", "asset_version_ids"}, {"revision"})
                    return self._reply(200, app.store.save_shot(episode_id, {"id": shot["id"], **{k: v for k, v in body.items() if k != "revision"}}, body["revision"]))
            if len(parts) == 5 and parts[2] == "shots":
                shot = app._shot(episode_id, _id(parts[3]))
                if parts[4] == "candidates" and method == "GET":
                    assets = app.store.list_assets(episode["project_id"])
                    return self._reply(200, [a for a in assets if a["kind"] == "video" and (not a["rights"].get("shot_id") or a["rights"].get("shot_id") == shot["id"])])
                if parts[4] == "select" and method == "POST":
                    _fields(body, {"asset_id", "revision"}, {"asset_id", "revision"})
                    app._asset(episode["project_id"], _id(body["asset_id"]))
                    return self._reply(200, app.store.select_candidate(shot["id"], body["asset_id"], body["revision"]))
                if parts[4] == "qc" and method == "POST": return self._reply(200, app._save_qc(episode_id, shot["id"], body))
                if parts[4] == "jobs" and method == "POST":
                    _fields(body, {"kind", "payload"}, {"kind", "payload"})
                    kind = body["kind"]
                    payload = body["payload"]
                    require(isinstance(payload, dict), "invalid_payload", 400, "任务内容必须是对象")
                    if kind == "h3":
                        require(app.status()["video"] == "configured", "video_unconfigured", 503, "视频工作流未配置")
                        _fields(payload, {"first_frame_asset_id", "plan_revision", "strategy"})
                        if app.status()["video_requires_first_frame"]:
                            require(payload.get("first_frame_asset_id"), "missing_input", 400, "当前工作流要求绑定首帧素材")
                        if payload.get("first_frame_asset_id"):
                            first = app._asset(episode["project_id"], _id(payload["first_frame_asset_id"]))
                            require(first["kind"] == "image" and first["id"] in shot["asset_version_ids"], "ownership_conflict", 409, "首帧须为已绑定的本作品画面素材")
                        payload = {**payload, "values": app.h3_values(shot)}
                    if kind == "text": require(app.status()["text"] == "configured", "text_unconfigured", 503, "文本模型未配置")
                    if kind == "asr": require(app.status()["asr"] == "configured", "asr_unconfigured", 503, "离线语音识别未配置")
                    require(kind in ("h3", "text", "probe", "asr"), "invalid_kind", 400, "任务类型不支持")
                    if kind in ("probe", "asr"):
                        _fields(payload, {"asset_id", "plan_revision", "strategy"}, {"asset_id"})
                        asset = app._asset(episode["project_id"], _id(payload["asset_id"]))
                        if kind == "probe": require(asset["kind"] == "video", "invalid_kind", 400, "视频核验需要视频素材")
                        if kind == "asr": require(asset["kind"] == "audio", "invalid_kind", 400, "离线语音识别需要声音素材")
                    return self._reply(201, _public_job(app.queue.enqueue({"project_id": episode["project_id"], "episode_id": episode_id, "shot_id": shot["id"]}, kind, payload)))
            if parts[2:] == ["timeline"]:
                if method == "GET": return self._reply(200, app.timeline.get_timeline(episode_id))
                if method == "PUT":
                    _fields(body, {"revision", "items"}, {"revision", "items"})
                    return self._reply(200, app.timeline.save_timeline(episode_id, body["items"], body["revision"]))
            if parts[2:] == ["cues"]:
                if method == "GET": return self._reply(200, app.subtitles.get_cues(episode_id))
                if method == "PUT":
                    _fields(body, {"revision", "cues"}, {"revision", "cues"})
                    return self._reply(200, app.subtitles.save_cues(episode_id, body["cues"], body["revision"]))
            if parts[2:] == ["cues", "srt"] and method == "GET":
                cues = app.subtitles.get_cues(episode_id)
                require(cues["status"] == "ready", "captions_not_ready", 409, "字幕未就绪")
                with tempfile.TemporaryDirectory(dir=app.root) as temp:
                    path = Path(temp) / "captions.srt"; write_srt(cues["cues"], path)
                    return self._send_file(path, "text/plain; charset=utf-8")
            if parts[2:] == ["story"] and method == "GET": return self._reply(200, app._story(episode_id))
            if parts[2:] == ["transcript-review"] and method == "POST":
                _fields(body, {"expected", "actual"}, {"expected", "actual"})
                return self._reply(200, review_transcript(body["expected"], body["actual"]))
            if parts[2:] == ["qc"] and method == "GET": return self._reply(200, app._qc(episode_id))
        raise DomainError("not_found", 404, "接口不存在")

    def _dispatch(self):
        try:
            self._run()
        except DomainError as error:
            self._reply(error.status, {"error": {"code": error.code, "message": error.message, "details": error.details}})
        except (KeyError, TypeError, ValueError) as error:
            self._reply(400, {"error": {"code": "invalid_payload", "message": "请求字段无效", "details": {}}})
        except Exception:
            self._reply(500, {"error": {"code": "internal_error", "message": "内部错误，请检查本地日志", "details": {}}})

    do_GET = do_POST = do_PUT = do_DELETE = _dispatch


def make_server(host="127.0.0.1", port=8766, data_root=None):
    require(host == "127.0.0.1", "invalid_host", 400, "服务仅可绑定 127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.app = App(Path(data_root) if data_root else Path.cwd() / ".workbench-data")
    except Exception:
        server.server_close()
        raise
    return server


def main():
    parser = argparse.ArgumentParser(description="镜序本地工作台")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-root", type=Path, default=Path.cwd() / ".workbench-data")
    args = parser.parse_args()
    server = make_server(port=args.port, data_root=args.data_root)
    print(f"镜序：http://127.0.0.1:{server.server_port}/", flush=True)
    try: server.serve_forever()
    finally:
        server.app.stop_workers()
        server.server_close()


if __name__ == "__main__":
    main()
