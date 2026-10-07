"""Explicit compatible chat-completions endpoint for optional text suggestions."""

import json

from workbench.domain import DomainError, require
from .comfy import _endpoint, _json_request


_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum"}


def _check_schema(schema, depth=0):
    require(depth <= 12 and isinstance(schema, dict) and isinstance(schema.get("type"), str) and
            schema["type"] in _TYPES and
            set(schema) <= _KEYS, "invalid_schema", 400, "JSON 结构规则不受支持")
    kind = schema["type"]
    if "enum" in schema:
        require(isinstance(schema["enum"], list) and bool(schema["enum"]),
                "invalid_schema", 400, "enum 必须是非空列表")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        require(isinstance(properties, dict) and all(isinstance(key, str) for key in properties) and
                isinstance(required, list) and all(isinstance(key, str) for key in required) and
                len(required) == len(set(required)) and set(required) <= set(properties) and
                type(schema.get("additionalProperties", True)) is bool,
                "invalid_schema", 400, "对象结构规则无效")
        for child in properties.values():
            _check_schema(child, depth + 1)
    elif kind == "array":
        require("items" in schema, "invalid_schema", 400, "数组缺少 items 规则")
        _check_schema(schema["items"], depth + 1)
    for key in ("properties", "required", "additionalProperties"):
        require(kind == "object" or key not in schema, "invalid_schema", 400, "结构规则与类型不符")
    require(kind == "array" or "items" not in schema, "invalid_schema", 400, "items 仅用于数组")
    if "enum" in schema:
        base = {key: value for key, value in schema.items() if key != "enum"}
        require(all(_matches(item, base) for item in schema["enum"]),
                "invalid_schema", 400, "enum 值与类型不符")


def _matches(value, schema):
    kind = schema["type"]
    typed = ((kind == "object" and isinstance(value, dict)) or
             (kind == "array" and isinstance(value, list)) or
             (kind == "string" and isinstance(value, str)) or
             (kind == "integer" and type(value) is int) or
             (kind == "number" and type(value) in (int, float)) or
             (kind == "boolean" and type(value) is bool) or
             (kind == "null" and value is None))
    if not typed or ("enum" in schema and value not in schema["enum"]):
        return False
    if kind == "object":
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= set(value):
            return False
        if schema.get("additionalProperties") is False and not set(value) <= set(properties):
            return False
        return all(key not in properties or _matches(item, properties[key]) for key, item in value.items())
    if kind == "array":
        return all(_matches(item, schema["items"]) for item in value)
    return True


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
        if schema is not None:
            _check_schema(schema)
            require(schema["type"] == "object", "invalid_schema", 400, "建议顶层必须是对象")
        headers = {"Authorization": "Bearer " + self.secret} if self.secret else {}
        payload = {"model": self.model, "messages": messages}
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "suggestion", "schema": schema}}
        response = _json_request(self.endpoint, self.timeout_s, payload=payload, headers=headers)
        try:
            content = response["choices"][0]["message"]["content"]
            require(isinstance(content, str), "invalid_text_response", 502, "文本响应无效")
            suggestion = json.loads(content) if schema is not None else content
            require(schema is None or _matches(suggestion, schema), "invalid_text_response", 502,
                    "文本建议不符合结构规则")
            return {"suggestion": suggestion, "status": "pending_adoption"}
        except (KeyError, IndexError, TypeError, ValueError):
            raise DomainError("invalid_text_response", 502, "文本响应无效") from None
