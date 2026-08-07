from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class MCPCallResult:
    payload: dict[str, Any] | None = None
    error: str = ""


def call_streamable_http_tool(
    server_config: dict[str, Any],
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> MCPCallResult:
    server_url = str(server_config.get("server_url") or "").strip()
    label = str(server_config.get("label") or server_config.get("name") or "MCP").strip()
    timeout_s = int(server_config.get("timeout_s") or 30)
    headers = _build_headers(server_config)
    if not server_url:
        return MCPCallResult(error=f"{label} 缺少 server_url。")

    session_headers: dict[str, str] = {}
    if bool(server_config.get("initialize_first", True)):
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": str(server_config.get("protocol_version") or "2025-11-25"),
                "capabilities": {},
                "clientInfo": {
                    "name": "skill-runtime",
                    "version": "0.1.0",
                },
            },
        }
        _, init_headers, init_error = post_json_rpc_request(
            server_url,
            init_payload,
            headers=headers,
            timeout_s=timeout_s,
            label=label,
        )
        if init_error:
            return MCPCallResult(error=init_error)
        session_id = extract_mcp_session_id(init_headers)
        if session_id:
            session_headers["Mcp-Session-Id"] = session_id
        post_json_rpc_notification(
            server_url,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            headers={**headers, **session_headers},
            timeout_s=timeout_s,
        )

    call_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    response_data, _, error = post_json_rpc_request(
        server_url,
        call_payload,
        headers={**headers, **session_headers},
        timeout_s=timeout_s,
        label=label,
    )
    if error:
        return MCPCallResult(error=error)

    result_payload = extract_json_rpc_result(response_data)
    if isinstance(result_payload, str):
        return MCPCallResult(error=f"{label} MCP 调用失败: {result_payload}")
    return MCPCallResult(payload=result_payload)


def _build_headers(server_config: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    custom_headers = server_config.get("headers", {})
    if isinstance(custom_headers, dict):
        for key, value in custom_headers.items():
            if str(key).strip() and str(value).strip():
                headers[str(key).strip()] = str(value).strip()
    api_key_env = str(server_config.get("api_key_env") or "").strip()
    if api_key_env:
        import os

        api_key = os.getenv(api_key_env, "").strip()
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
    return headers


def post_json_rpc_request(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_s: int,
    label: str,
) -> tuple[dict[str, Any] | None, dict[str, str], str]:
    request_body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=request_body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw_body = response.read().decode("utf-8")
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace").strip()
        return None, {}, f"{label} MCP 调用失败: HTTP {exc.code}: {error_body or 'empty body'}"
    except urllib.error.URLError as exc:
        return None, {}, f"{label} MCP 调用失败: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return None, {}, f"{label} MCP 调用失败: {exc}"

    decoded_body = decode_json_or_sse_payload(raw_body)
    try:
        data = json.loads(decoded_body)
    except json.JSONDecodeError as exc:
        return None, response_headers, f"{label} MCP 返回不是合法 JSON: {exc.msg}"
    if not isinstance(data, dict):
        return None, response_headers, f"{label} MCP 返回顶层不是 JSON 对象。"
    return data, response_headers, ""


def post_json_rpc_notification(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_s: int,
) -> None:
    request_body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=request_body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s):
            return
    except Exception:  # noqa: BLE001
        return


def decode_json_or_sse_payload(raw_body: str) -> str:
    stripped = raw_body.strip()
    if not stripped:
        return "{}"
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped
    data_chunks: list[str] = []
    for line in stripped.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                data_chunks.append(chunk)
    return data_chunks[-1] if data_chunks else stripped


def extract_json_rpc_result(payload: dict[str, Any]) -> dict[str, Any] | str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "unknown error").strip()
        code = error.get("code")
        detail = f"{code} {message}".strip() if code is not None else message
        return detail or "unknown error"
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return "result 缺失或格式非法"


def extract_mcp_session_id(headers: dict[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "mcp-session-id":
            return str(value).strip()
    return ""
