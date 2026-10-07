"""Explicit compatible chat-completions endpoint for optional text suggestions."""

import json

from workbench.domain import DomainError, require
from .comfy import _endpoint, _json_request


class TextAdapter:
    def __init__(self, endpoint: str | None = None, model: str | None = None,
                 secret: str | None = None, timeout_s: float = 20):
        self.endpoint = _endpoint(endpoint, loopback_only=False) if endpoint else None
        self.model = model
        self.secret = secret
        self.timeout_s = timeout_s

    def generate(self, messages: list[dict], schema: dict | None = None) -> dict:
        require(self.endpoint is not None and isinstance(self.model, str) and bool(self.model.strip()),
                "text_unconfigured", 503, "文本服务未配置")
        require(isinstance(messages, list) and bool(messages) and all(
            isinstance(message, dict) and message.get("role") in ("system", "user", "assistant") and
            isinstance(message.get("content"), str) for message in messages),
            "invalid_messages", 400, "消息格式无效")
        require(schema is None or isinstance(schema, dict), "invalid_schema", 400, "建议结构无效")
        headers = {"Authorization": "Bearer " + self.secret} if self.secret else {}
        payload = {"model": self.model, "messages": messages}
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "suggestion", "schema": schema}}
        response = _json_request(self.endpoint, self.timeout_s, payload=payload, headers=headers)
        try:
            content = response["choices"][0]["message"]["content"]
            require(isinstance(content, str), "invalid_text_response", 502, "文本响应无效")
            suggestion = json.loads(content) if schema is not None else content
            require(schema is None or isinstance(suggestion, dict), "invalid_text_response", 502, "文本建议格式无效")
            return {"suggestion": suggestion, "status": "pending_adoption"}
        except (KeyError, IndexError, TypeError, ValueError):
            raise DomainError("invalid_text_response", 502, "文本响应无效") from None
