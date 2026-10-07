"""Explicit workflow bindings and scoped H3 job handler."""

import copy
import os
import tempfile
import time
from pathlib import Path
import math

from .domain import DomainError, require
from .worker import ProviderFailure


def _validate_input(value, spec, workflow, object_info, uploaded_names):
    require(isinstance(spec, list) and bool(spec), "invalid_workflow", 400, "节点输入规格无效")
    datatype = spec[0]
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int:
        source = workflow.get(value[0])
        require(isinstance(source, dict) and value[1] >= 0, "invalid_link", 400, "节点连接源无效")
        source_meta = object_info.get(source.get("class_type"), {})
        outputs = source_meta.get("output", [])
        require(isinstance(outputs, list) and value[1] < len(outputs), "invalid_link", 400, "节点输出不存在")
        source_type = outputs[value[1]]
        require(source_type == datatype or (isinstance(datatype, list) and source_type == "STRING"),
                "invalid_link", 400, "节点连接类型不匹配")
        return
    limits = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
    if datatype == "INT":
        valid = type(value) is int
    elif datatype == "FLOAT":
        valid = type(value) in (int, float) and math.isfinite(value)
    elif datatype == "STRING":
        valid = isinstance(value, str)
    elif datatype == "BOOLEAN":
        valid = type(value) is bool
    elif isinstance(datatype, list):
        valid = value in datatype or value in uploaded_names
    else:
        valid = False
    require(valid, "invalid_value", 400, "节点输入类型或选项无效")
    if datatype in ("INT", "FLOAT"):
        require(limits.get("min", value) <= value <= limits.get("max", value),
                "invalid_value", 400, "数值输入超出节点范围")


def bind_workflow(workflow: dict, bindings: dict, values: dict, object_info: dict,
                  *, uploaded_names=()) -> dict:
    require(isinstance(workflow, dict) and bool(workflow) and isinstance(bindings, dict) and
            isinstance(values, dict) and isinstance(object_info, dict), "invalid_workflow", 400, "工作流配置无效")
    bound = copy.deepcopy(workflow)
    for node_id, node in bound.items():
        require(isinstance(node_id, str) and isinstance(node, dict) and
                isinstance(node.get("class_type"), str) and isinstance(node.get("inputs"), dict),
                "invalid_workflow", 400, "工作流节点无效")
        meta = object_info.get(node["class_type"])
        require(isinstance(meta, dict) and isinstance(meta.get("input"), dict),
                "missing_node", 400, "所需 ComfyUI 节点不存在")
        required = meta["input"].get("required", {})
        require(isinstance(required, dict) and set(required) <= set(node["inputs"]),
                "missing_input", 400, "工作流缺少必填输入")
    require(set(values) <= set(bindings), "invalid_binding", 400, "输入未绑定工作流节点")
    for name, value in values.items():
        binding = bindings[name]
        require(isinstance(binding, dict) and set(binding) == {"node", "input"} and
                binding["node"] in bound and isinstance(binding["input"], str),
                "invalid_binding", 400, "节点输入绑定无效")
        node = bound[binding["node"]]
        meta = object_info[node["class_type"]]["input"]
        declared = {**meta.get("required", {}), **meta.get("optional", {})}
        field = binding["input"]
        require(field in declared and field in node["inputs"], "invalid_binding", 400, "绑定输入不存在")
        node["inputs"][field] = value
    for node in bound.values():
        meta = object_info[node["class_type"]]["input"]
        declared = {**meta.get("required", {}), **meta.get("optional", {})}
        require(set(node["inputs"]) <= set(declared), "invalid_input", 400, "工作流包含未知输入")
        for field, value in node["inputs"].items():
            _validate_input(value, declared[field], bound, object_info, uploaded_names)
    return bound


def _scope(store, job):
    require(job.get("kind") == "h3" and all(isinstance(job.get(key), str) for key in
            ("id", "project_id", "episode_id", "shot_id", "claim_token")),
            "invalid_job", 400, "H3 任务范围无效")
    episode = store.get_episode(job["episode_id"])
    shot = store.get_shot(job["shot_id"])
    require(episode["project_id"] == job["project_id"] and shot["episode_id"] == job["episode_id"],
            "ownership_conflict", 409, "任务素材范围冲突")


def _media_kind(filename: str, content: bytes) -> str:
    """Reject obvious non-media; full decoding belongs to the later QC stage."""
    require(isinstance(content, bytes), "invalid_media", 502, "输出内容无效")
    suffix = Path(filename).suffix.lower()
    if suffix == ".png":
        valid = (len(content) >= 45 and content.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR")
                 and content.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82"))
        kind = "image"
    elif suffix in (".jpg", ".jpeg"):
        valid = len(content) >= 4 and content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
        kind = "image"
    elif suffix == ".webp":
        valid = len(content) >= 16 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
        kind = "image"
    elif suffix in (".mp4", ".mov"):
        valid = len(content) >= 16 and content[4:8] == b"ftyp"
        kind = "video"
    elif suffix in (".webm", ".mkv"):
        marker = b"webm" if suffix == ".webm" else b"matroska"
        valid = len(content) >= 16 and content.startswith(b"\x1a\x45\xdf\xa3") and marker in content[:256]
        kind = "video"
    else:
        raise DomainError("unsupported_output", 502, "输出文件格式不支持")
    require(valid, "invalid_media", 502, "输出文件签名与类型不符")
    return kind


def make_h3_handler(queue, store, adapter, workflow: dict, bindings: dict, *,
                    first_frame_binding: str | None = None, poll_interval_s: float = 1,
                    max_wait_s: float = 300):
    """Return a Worker handler. A claimed job is never submitted twice."""
    require(0 < poll_interval_s <= 30 and 0 < max_wait_s <= 3600, "invalid_timeout", 400, "等待范围无效")

    def handle(kind, job):
        job_id, token = job["id"], job["claim_token"]
        attempted = False
        try:
            _scope(store, job)
            require(job.get("submission_attempted_at") is None and job.get("external_id") is None,
                    "submission_uncertain", 409, "任务已有外部提交记录")
            values = job.get("payload", {}).get("values", {})
            info = adapter.object_info()
            bound = bind_workflow(workflow, bindings, values, info)
            require(not adapter.check()["busy"], "provider_busy", 409, "ComfyUI 队列正忙")
            first_frame_id = job.get("payload", {}).get("first_frame_asset_id")
            if first_frame_id is not None:
                require(isinstance(first_frame_binding, str) and first_frame_binding in bindings and
                        first_frame_binding not in values, "invalid_binding", 400, "首帧工作流输入未绑定")
                asset = store.get_asset(first_frame_id)
                require(asset["project_id"] == job["project_id"] and asset["kind"] == "image" and
                        asset["binary_available"], "ownership_conflict", 409, "首帧素材不属于当前作品或文件不可用")
                source = store.data_root / "assets" / asset["storage_key"]
                name = adapter.upload_image(source.read_bytes(), asset["sha256"] + ".png")
                bound = bind_workflow(workflow, bindings, {**values, first_frame_binding: name}, info,
                                      uploaded_names={name})

            def mark():
                nonlocal attempted
                queue.mark_submission_attempt(job_id, token)
                attempted = True

            prompt_id = adapter.submit(bound, job_id, before_post=mark)
            queue.record_external(job_id, prompt_id, token)
            deadline = time.monotonic() + max_wait_s
            while True:
                queue.renew_lease(job_id, token)
                state = adapter.status(prompt_id)
                if state["started_at"]:
                    queue.record_execution_started(job_id, state["started_at"], token)
                if state["state"] == "failed":
                    if state["started_at"] and state["finished_at"]:
                        raise ProviderFailure("provider_execution_failed", "ComfyUI 执行失败", "executed_failure",
                                              state["finished_at"])
                    raise DomainError("execution_uncertain", 503, "执行失败但缺少完整时间证据")
                if state["state"] == "succeeded":
                    require(state["started_at"] and state["finished_at"], "execution_uncertain", 503,
                            "成功状态缺少完整时间证据")
                    files = adapter.collect(prompt_id)
                    candidates = []
                    staging = store.data_root / "staging"
                    staging.mkdir(parents=True, exist_ok=True)
                    for file in files:
                        suffix = Path(file["filename"]).suffix.lower()
                        kind = _media_kind(file["filename"], file["content"])
                        fd, path = tempfile.mkstemp(prefix="comfy-", suffix=suffix, dir=staging)
                        try:
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(file["content"])
                            asset = store.import_asset(job["project_id"], Path(path), kind,
                                                       {"source": "comfy", "job_id": job_id,
                                                        "prompt_id": prompt_id, "episode_id": job["episode_id"],
                                                        "shot_id": job["shot_id"], "status": "candidate",
                                                        "media_qc": "pending_decode"})
                            candidates.append(asset)
                        finally:
                            Path(path).unlink(missing_ok=True)
                    return {"prompt_id": prompt_id, "candidate_assets": candidates,
                            "execution_finished_at": state["finished_at"]}
                if time.monotonic() >= deadline:
                    raise DomainError("execution_timeout", 503, "等待 ComfyUI 执行超时，需核对")
                time.sleep(poll_interval_s)
        except ProviderFailure:
            raise
        except DomainError as error:
            if error.code == "stale_claim":
                raise
            if not attempted:
                raise ProviderFailure(error.code, str(error), "preflight") from error
            if error.code == "provider_rejected":
                from datetime import datetime, timezone
                raise ProviderFailure(error.code, str(error), "rejected", datetime.now(timezone.utc).isoformat()) from error
            raise ProviderFailure(error.code, str(error), "uncertain") from error

    return handle
