"""Bounded self-hosted ComfyUI HTTP adapter."""

import json
import math
import re
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from workbench.domain import DomainError, require


MAX_JSON = 4 * 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _endpoint(endpoint, *, loopback_only):
    require(isinstance(endpoint, str) and bool(endpoint), "invalid_endpoint", 400, "服务地址未配置")
    parsed = urlsplit(endpoint)
    require(parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment, "invalid_endpoint", 400, "服务地址无效")
    local = parsed.hostname in ("127.0.0.1", "::1")
    require(not loopback_only or local, "invalid_endpoint", 400, "ComfyUI 仅允许显式回环地址")
    require(parsed.scheme == "https" or local, "insecure_endpoint", 400, "远程文本服务需要 HTTPS")
    return endpoint.rstrip("/")


def _request(url, timeout_s, *, payload=None, raw_body=None, headers=None, limit=MAX_JSON):
    require(isinstance(timeout_s, (int, float)) and math.isfinite(timeout_s) and 0 < timeout_s <= 60,
            "invalid_timeout", 400, "请求超时范围无效")
    body = raw_body if raw_body is not None else (None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    request = Request(url, data=body, headers={"Accept": "application/json", **(headers or {}),
                                               **({"Content-Type": "application/json"} if payload is not None else {})},
                      method="POST" if body is not None else "GET")
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout_s) as response:
            raw = response.read(limit + 1)
            require(len(raw) <= limit, "provider_oversize", 502, "服务响应超过限制")
            return raw, response.headers.get_content_type()
    except HTTPError as error:
        error.close()
        raise DomainError("provider_rejected" if body is not None and error.code in (401, 403, 404) else "provider_http",
                          502, f"服务返回 HTTP {error.code}", {"http_status": error.code}) from None
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as error:
        raise DomainError("provider_timeout" if isinstance(error, (TimeoutError, socket.timeout)) or
                          isinstance(getattr(error, "reason", None), socket.timeout) else "provider_unavailable",
                          503, "服务请求未完成") from None


def _json_request(url, timeout_s, *, payload=None, headers=None):
    raw, _ = _request(url, timeout_s, payload=payload, headers=headers)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise DomainError("provider_json", 502, "服务返回非 JSON") from None


def _safe_name(value, *, subfolder=False):
    require(isinstance(value, str) and (value or subfolder), "unsafe_filename", 502, "输出文件名无效")
    if subfolder and not value:
        return ""
    parts = value.replace("\\", "/").split("/") if subfolder else [value]
    require(all(part not in ("", ".", "..") and _NAME.fullmatch(part) for part in parts),
            "unsafe_filename", 502, "输出文件路径不安全")
    return "/".join(parts)


def _stamp(milliseconds):
    require(type(milliseconds) in (int, float) and math.isfinite(milliseconds) and milliseconds >= 0,
            "invalid_history", 502, "历史事件缺少时间戳")
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()


class ComfyAdapter:
    def __init__(self, endpoint: str, timeout_s: float):
        self.endpoint = _endpoint(endpoint, loopback_only=True)
        self.timeout_s = timeout_s

    def check(self) -> dict:
        response = _json_request(self.endpoint + "/queue", self.timeout_s)
        require(isinstance(response, dict) and isinstance(response.get("queue_running"), list) and
                isinstance(response.get("queue_pending"), list), "invalid_queue", 502, "ComfyUI 队列响应无效")
        return {"busy": bool(response["queue_running"] or response["queue_pending"]),
                "running": len(response["queue_running"]), "pending": len(response["queue_pending"])}

    def object_info(self) -> dict:
        response = _json_request(self.endpoint + "/object_info", self.timeout_s)
        require(isinstance(response, dict), "invalid_object_info", 502, "节点信息无效")
        return response

    def upload_image(self, content: bytes, filename: str) -> str:
        """Upload an already scoped first frame without disclosing local paths."""
        filename = _safe_name(filename)
        require(isinstance(content, bytes) and 0 < len(content) <= 16 * 1024 * 1024,
                "invalid_image", 400, "首帧图片大小无效")
        boundary = "workbench" + uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        raw, _ = _request(self.endpoint + "/upload/image", self.timeout_s, raw_body=body,
                          headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DomainError("provider_json", 502, "图片上传响应无效") from None
        require(isinstance(response, dict) and response.get("type") == "input", "invalid_upload", 502, "图片上传结果无效")
        return _safe_name(response.get("name"))

    def submit(self, workflow: dict, client_id: str, *, before_post=None) -> str:
        require(isinstance(workflow, dict) and bool(workflow), "invalid_workflow", 400, "工作流未配置")
        require(isinstance(client_id, str) and bool(client_id.strip()), "invalid_client_id", 400, "客户端 ID 无效")
        require(not self.check()["busy"], "provider_busy", 409, "ComfyUI 队列正忙")
        if before_post is not None:
            before_post()
        try:
            response = _json_request(self.endpoint + "/prompt", self.timeout_s,
                                     payload={"prompt": workflow, "client_id": client_id})
        except DomainError as error:
            if error.code != "provider_rejected":
                raise DomainError("submission_uncertain", 503, "提交结果不明，需人工核对") from error
            raise
        if isinstance(response, dict) and response.get("error") and not response.get("prompt_id"):
            raise DomainError("provider_rejected", 502, "ComfyUI 拒绝了工作流")
        if not isinstance(response, dict) or not isinstance(response.get("prompt_id"), str) or not response["prompt_id"]:
            raise DomainError("submission_uncertain", 503, "提交响应缺少 prompt_id，需人工核对")
        return response["prompt_id"]

    def status(self, prompt_id: str) -> dict:
        require(isinstance(prompt_id, str) and _ID.fullmatch(prompt_id), "invalid_prompt_id", 400, "任务 ID 无效")
        response = _json_request(self.endpoint + "/history/" + prompt_id, self.timeout_s)
        require(isinstance(response, dict), "invalid_history", 502, "执行历史无效")
        if prompt_id not in response:
            return {"state": "pending", "started_at": None, "finished_at": None}
        entry = response[prompt_id]
        require(isinstance(entry, dict) and isinstance(entry.get("status"), dict), "invalid_history", 502, "执行历史无效")
        status = entry["status"]
        require(isinstance(status.get("messages"), list), "invalid_history", 502, "执行事件缺失")
        events = {}
        for message in status["messages"]:
            require(isinstance(message, list) and len(message) == 2 and isinstance(message[1], dict),
                    "invalid_history", 502, "执行事件无效")
            if message[1].get("prompt_id") == prompt_id and message[0] in (
                    "execution_start", "execution_success", "execution_error", "execution_interrupted"):
                events[message[0]] = message[1]
        started = _stamp(events["execution_start"]["timestamp"]) if "execution_start" in events else None
        failure = next((events[name] for name in ("execution_error", "execution_interrupted") if name in events), None)
        if failure:
            return {"state": "failed", "started_at": started,
                    "finished_at": _stamp(failure["timestamp"]) if "timestamp" in failure else None}
        if status.get("status_str") == "success" and status.get("completed") is True:
            finished = events.get("execution_success")
            return {"state": "succeeded", "started_at": started,
                    "finished_at": _stamp(finished["timestamp"]) if finished and "timestamp" in finished else None}
        return {"state": "pending", "started_at": started, "finished_at": None}

    def collect(self, prompt_id: str) -> list[dict]:
        state = self.status(prompt_id)
        require(state["state"] == "succeeded", "execution_unverified", 409, "执行尚未成功")
        response = _json_request(self.endpoint + "/history/" + prompt_id, self.timeout_s)
        outputs = response[prompt_id].get("outputs")
        require(isinstance(outputs, dict), "invalid_history", 502, "输出缺失")
        files = []
        for node_output in outputs.values():
            require(isinstance(node_output, dict), "invalid_history", 502, "输出结构无效")
            for items in node_output.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    filename = _safe_name(item["filename"])
                    subfolder = _safe_name(item.get("subfolder", ""), subfolder=True)
                    require(item.get("type") in ("output", "temp"), "unsafe_filename", 502, "输出类型无效")
                    query = urlencode({"filename": filename, "subfolder": subfolder, "type": item["type"]})
                    raw, _ = _request(self.endpoint + "/view?" + query, self.timeout_s, limit=MAX_BINARY)
                    files.append({"filename": filename, "subfolder": subfolder, "type": item["type"], "content": raw})
        require(bool(files), "missing_output", 502, "执行成功但没有可收集文件")
        return files
