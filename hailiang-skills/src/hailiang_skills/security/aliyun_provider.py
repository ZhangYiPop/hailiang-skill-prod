from __future__ import annotations

import json
import os
from typing import Any

from hailiang_skills.security.models import ModerationResult


class AliyunProvider:
    def __init__(self) -> None:
        self.available = False
        self._client: Any = None
        self.failure_reason: str | None = None
        access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip()
        access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip()
        if not access_key_id or not access_key_secret:
            self.failure_reason = "aliyun_credentials_missing"
            return
        try:
            from alibabacloud_green20220302.client import Client
            from alibabacloud_tea_openapi.models import Config

            self._models = __import__("alibabacloud_green20220302.models", fromlist=["models"])
            self._client = Client(
                Config(
                    access_key_id=access_key_id,
                    access_key_secret=access_key_secret,
                    connect_timeout=int(os.getenv("ALIYUN_SECURITY_CONNECT_TIMEOUT_MS", "3000")),
                    read_timeout=int(os.getenv("ALIYUN_SECURITY_READ_TIMEOUT_MS", "6000")),
                    region_id=os.getenv("ALIYUN_SECURITY_REGION", "cn-hangzhou"),
                    endpoint=os.getenv("ALIYUN_SECURITY_ENDPOINT", "green-cip.cn-hangzhou.aliyuncs.com"),
                )
            )
            self.available = True
        except Exception as exc:  # SDK is optional during local development.
            self.failure_reason = f"aliyun_sdk_unavailable:{type(exc).__name__}"

    def check(self, content: str, *, stage: str, chat_id: str | None = None) -> ModerationResult:
        if len(content) > 2000:
            raise ValueError("Aliyun moderation content cannot exceed 2000 characters")
        service = "query_security_check" if stage == "input" else "response_security_check"
        request = self._models.TextModerationPlusRequest(
            service=service,
            service_parameters=json.dumps(
                {"content": content, **({"chatId": chat_id} if chat_id else {})},
                ensure_ascii=False,
            ),
        )
        response = self._client.text_moderation_plus(request)
        status_code = int(getattr(response, "status_code", 0) or 0)
        body = getattr(response, "body", None)
        payload = _to_dict(body)
        if status_code != 200 or int(payload.get("Code", payload.get("code", 0)) or 0) != 200:
            raise RuntimeError(f"aliyun_http_or_api_error:{status_code}:{payload.get('Message', payload.get('message', 'unknown'))}")
        data = payload.get("Data", payload.get("data", {})) or {}
        risk_level = str(data.get("RiskLevel", data.get("riskLevel", "none")) or "none").lower()
        if risk_level not in {"none", "low", "medium", "high"}:
            raise RuntimeError("aliyun_invalid_risk_level")
        result_items = data.get("Result", data.get("result", [])) or []
        labels = sorted({str(item.get("Label", item.get("label", ""))) for item in result_items if isinstance(item, dict) and item.get("Label", item.get("label"))})
        return ModerationResult(
            matched=risk_level != "none",
            risk_level=risk_level,  # type: ignore[arg-type]
            labels=labels,
            provider="aliyun",
            mode="cloud",
            request_id=str(payload.get("RequestId", payload.get("requestId", "")) or "") or None,
            raw=payload,
        )


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_map"):
        mapped = value.to_map()
        return mapped if isinstance(mapped, dict) else {}
    if hasattr(value, "to_map_recursive"):
        mapped = value.to_map_recursive()
        return mapped if isinstance(mapped, dict) else {}
    if hasattr(value, "__dict__"):
        return {key: _to_plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _to_plain(value: Any) -> Any:
    if hasattr(value, "to_map_recursive"):
        return value.to_map_recursive()
    if hasattr(value, "__dict__"):
        return {key: _to_plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value

