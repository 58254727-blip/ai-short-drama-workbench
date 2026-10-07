"""Explicit workflow bindings and scoped H3 job handler."""

import copy
import os
import tempfile
import time
from pathlib import Path

from .domain import DomainError, require
from .worker import ProviderFailure


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
        spec = declared[field]
        require(isinstance(spec, list) and bool(spec), "invalid_binding", 400, "节点输入规格无效")
        datatype = spec[0]
        limits = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        if datatype == "INT":
            require(type(value) is int and limits.get("min", value) <= value <= limits.get("max", value),
                    "invalid_value", 400, "整数输入超出节点范围")
        elif datatype == "FLOAT":
            require(type(value) in (int, float) and limits.get("min", value) <= value <= limits.get("max", value),
                    "invalid_value", 400, "数值输入超出节点范围")
        elif isinstance(datatype, list):
            require(value in datatype or value in uploaded_names, "invalid_value", 400, "输入不在节点选项内")
        elif datatype == "STRING":
            require(isinstance(value, str), "invalid_value", 400, "文本输入无效")
        else:
            raise DomainError("invalid_binding", 400, "此节点输入类型不可绑定")
        node["inputs"][field] = value
    return bound


def _scope(store, job):
    require(job.get("kind") == "h3" and all(isinstance(job.get(key), str) for key in
            ("id", "project_id", "episode_id", "shot_id", "claim_token")),
            "invalid_job", 400, "H3 任务范围无效")
    episode = store.get_episode(job["episode_id"])
    shot = store.get_shot(job["shot_id"])
    require(episode["project_id"] == job["project_id"] and shot["episode_id"] == job["episode_id"],
            "ownership_conflict", 409, "任务素材范围冲突")


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
                        kind = "video" if suffix in (".mp4", ".webm", ".mov", ".mkv") else "image"
                        require(suffix in (".mp4", ".webm", ".mov", ".mkv", ".png", ".jpg", ".jpeg", ".webp"),
                                "unsupported_output", 502, "输出文件格式不支持")
                        fd, path = tempfile.mkstemp(prefix="comfy-", suffix=suffix, dir=staging)
                        try:
                            with os.fdopen(fd, "wb") as stream:
                                stream.write(file["content"])
                            asset = store.import_asset(job["project_id"], Path(path), kind,
                                                       {"source": "comfy", "job_id": job_id,
                                                        "prompt_id": prompt_id, "episode_id": job["episode_id"],
                                                        "shot_id": job["shot_id"], "status": "candidate"})
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
