from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from hailiang_skills.skill_runtime.config import load_llm_config
from hailiang_skills.skill_runtime.intent_tracker import apply_intent_update, track_user_intent
from hailiang_skills.skill_runtime.llm_client import OpenAICompatibleChatClient
from hailiang_skills.skill_runtime.models import ChatMessage, ResponsePolicyConfig, ToolCallRequest, ToolCallResult
from hailiang_skills.skill_runtime.route_router import choose_route
from hailiang_skills.skill_runtime.runtime_logger import RuntimeLogger, default_log_file, preview_text
from hailiang_skills.skill_runtime.runtime_router import classify_tool_routing
from hailiang_skills.skill_runtime.session import (
    build_model_messages,
    default_session_file,
    load_session_state,
    run_status_hook_if_present,
    save_session_state,
)
from hailiang_skills.skill_runtime.skill_registry import SkillRegistry, load_local_skill_registry
from hailiang_skills.skill_runtime.skill_loader import default_cache_dir, load_skill_bundle
from hailiang_skills.skill_runtime.state_tracker import ensure_runtime_state, mark_route_interruption
from hailiang_skills.skill_runtime.tools import build_tool_specs, execute_tool_call

MAX_TOOL_CALLS_PER_TURN = 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        cache_dir = Path(args.cache_dir).expanduser().resolve()
        bundle = load_skill_bundle(args.skill, cache_dir=cache_dir)
        config = load_llm_config(args.llm_config, require_api_key=not args.dry_run)

        session_path = (
            Path(args.session_file).expanduser().resolve()
            if args.session_file
            else default_session_file(bundle, cache_dir)
        )
        state = load_session_state(session_path)
        save_session_state(state, session_path)
        logger = RuntimeLogger(default_log_file(session_path), state.session_id)

        _print_bundle_summary(bundle, session_path, logger.file_path)
        if args.dry_run:
            logger.log(
                "session.dry_run",
                skill=bundle.metadata.get("name") or bundle.root_name,
                source=bundle.source,
                session_path=str(session_path),
            )
            print("dry-run 完成，未发起模型请求。")
            return 0

        client = OpenAICompatibleChatClient(config)
        skills_root = Path(__file__).resolve().parent.parent / "local_skill"
        registry = load_local_skill_registry(skills_root)
        logger.log(
            "session.start",
            skill=bundle.metadata.get("name") or bundle.root_name,
            source=bundle.source,
            session_path=str(session_path),
            log_path=str(logger.file_path),
        )
        return _interactive_loop(bundle, state, session_path, client, logger, registry)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地运行 SKILL.md 风格的 .skill 包")
    parser.add_argument(
        "--skill",
        required=True,
        help="本地 .skill 文件路径，或可公开访问的 .skill URL",
    )
    parser.add_argument(
        "--llm-config",
        required=True,
        help="llm/llm_config.json 路径",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(default_cache_dir()),
        help="skill 下载、解包和会话状态的缓存目录",
    )
    parser.add_argument(
        "--session-file",
        help="会话状态文件路径；默认保存在缓存目录下",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验 skill 与配置是否可读取，不进入交互会话",
    )
    return parser


def _interactive_loop(
    bundle,
    state,
    session_path: Path,
    client: OpenAICompatibleChatClient,
    logger: RuntimeLogger,
    registry: SkillRegistry | None = None,
) -> int:
    print("进入交互模式，输入 exit 或 quit 结束。")
    if _needs_auto_greeting(state):
        logger.log("turn.auto_greeting.start")
        reply = _generate_and_persist_assistant_reply(bundle, state, session_path, client, logger, registry)
        print(f"assistant> {reply}")
        print()

    while True:
        try:
            user_input = _sanitize_user_input(input("user> "))
        except EOFError:
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", ":q"}:
            logger.log("session.exit", reason="user_exit")
            break

        state.messages.append(ChatMessage(role="user", content=user_input))
        save_session_state(state, session_path)
        logger.log("turn.user_input", content=user_input, message_count=len(state.messages))
        reply = _generate_and_persist_assistant_reply(bundle, state, session_path, client, logger, registry)

        print(f"assistant> {reply}")
        print()

    save_session_state(state, session_path)
    return 0


def _print_bundle_summary(bundle, session_path: Path, log_path: Path) -> None:
    skill_name = bundle.metadata.get("name") or bundle.root_name
    print(f"skill: {skill_name}")
    print(f"source: {bundle.source}")
    print(f"archive: {bundle.archive_path}")
    print(f"references: {len(bundle.references)}")
    print(f"scripts: {len(bundle.scripts)}")
    print(f"session: {session_path}")
    print(f"log: {log_path}")
    print()


def _needs_auto_greeting(state) -> bool:
    return not any(message.role == "assistant" for message in state.messages)


def _generate_and_persist_assistant_reply(
    bundle,
    state,
    session_path: Path,
    client: OpenAICompatibleChatClient,
    logger: RuntimeLogger,
    registry: SkillRegistry | None = None,
) -> str:
    reply = _resolve_assistant_reply(bundle, state, client, logger, registry)
    state.messages.append(ChatMessage(role="assistant", content=reply))
    save_session_state(state, session_path)
    logger.log("turn.assistant_reply.persisted", reply_preview=preview_text(reply), message_count=len(state.messages))
    try:
        current_bundle = _current_bundle(bundle, state, registry)
        run_status_hook_if_present(current_bundle, state, logger=logger)
        logger.log("status_hook.completed", stage=state.stage, collected_info=state.collected_info)
    except Exception as exc:  # noqa: BLE001
        logger.log("status_hook.failed", error=str(exc))
        raise
    save_session_state(state, session_path)
    return reply


def _resolve_assistant_reply(bundle, state, client, logger: RuntimeLogger | None = None, registry: SkillRegistry | None = None) -> str:
    ensure_runtime_state(state, bundle)
    current_bundle = _current_bundle(bundle, state, registry)
    intent_update = track_user_intent(current_bundle, state)
    apply_intent_update(state, intent_update)
    _apply_route_if_needed(bundle, state, registry, logger)
    current_bundle = _current_bundle(bundle, state, registry)
    tool_results: tuple[ToolCallResult, ...] = ()
    transient_messages: tuple[ChatMessage, ...] = ()
    preferred_mode = "native"
    used_tool_names: list[str] = []
    if logger:
        logger.log(
            "turn.resolve.start",
            latest_user_message=next((item.content for item in reversed(state.messages) if item.role == "user"), ""),
            stage=state.stage,
            active_skill_id=state.active_skill_id,
        )
    routing_decision = classify_tool_routing(current_bundle, state, client, logger=logger)
    for tool_index in range(MAX_TOOL_CALLS_PER_TURN + 1):
        current_bundle = _current_bundle(bundle, state, registry)
        tool_specs = build_tool_specs(current_bundle, state, routing_decision=routing_decision, logger=logger)
        messages = build_model_messages(
            current_bundle,
            state,
            tool_results=tool_results,
            tool_mode=preferred_mode,
            available_tool_specs=tool_specs,
            max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
            transient_messages=transient_messages,
            routing_decision=routing_decision,
        )
        try:
            turn_result = client.complete_with_tools(messages, tool_specs, preferred_mode=preferred_mode, logger=logger)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.log("llm.turn.failed", preferred_mode=preferred_mode, error=str(exc))
            raise
        if not turn_result.tool_calls:
            if logger:
                logger.log(
                    "turn.resolve.final_text",
                    tool_mode=turn_result.tool_mode,
                    final_text_preview=preview_text(turn_result.final_text),
                )
            return _sanitize_assistant_reply(
                turn_result.final_text,
                response_policy=current_bundle.runtime_metadata.response_policy,
            )

        if tool_index >= MAX_TOOL_CALLS_PER_TURN:
            if logger:
                logger.log("turn.resolve.limit_reached", max_tool_calls=MAX_TOOL_CALLS_PER_TURN)
            return "本轮工具调用次数已达上限；如果你愿意，我可以先基于现有信息给出一个保守建议。"

        next_transient_messages: list[ChatMessage] = list(transient_messages)
        next_tool_results: list[ToolCallResult] = list(tool_results)
        for call in turn_result.tool_calls:
            if call.name in used_tool_names:
                result = ToolCallResult(
                    id=call.id,
                    name=call.name,
                    ok=False,
                    content="",
                    error=f"工具 {call.name} 在本轮已调用过，不能重复循环调用。",
                )
                if logger:
                    logger.log("tool.call.rejected", reason="duplicate", tool_name=call.name, call_id=call.id)
            elif _is_empty_tool_call(call):
                result = ToolCallResult(
                    id=call.id,
                    name=call.name,
                    ok=False,
                    content="",
                    error=f"工具 {call.name} 缺少必要参数。",
                )
                if logger:
                    logger.log("tool.call.rejected", reason="empty_query", tool_name=call.name, call_id=call.id)
            else:
                result = execute_tool_call(current_bundle, state, call, routing_decision=routing_decision, logger=logger)
                used_tool_names.append(call.name)
            next_transient_messages.extend(_tool_exchange_messages(call, result, tool_mode=turn_result.tool_mode))
            next_tool_results.append(result)
        tool_results = tuple(next_tool_results)
        transient_messages = tuple(next_transient_messages)
        preferred_mode = turn_result.tool_mode

    if logger:
        logger.log("turn.resolve.unstable")
    return "当前无法稳定完成工具调用流程；如果你愿意，我先基于现有信息给出方向性建议。"


def _current_bundle(bundle, state: SessionState, registry: SkillRegistry | None = None):
    ensure_runtime_state(state, bundle)
    if not registry:
        return bundle
    return registry.get(state.active_skill_id) or bundle


def _apply_route_if_needed(bundle, state: SessionState, registry: SkillRegistry | None, logger: RuntimeLogger | None) -> None:
    current_bundle = _current_bundle(bundle, state, registry)
    if state.status_flags.get("resume_to_skill_id"):
        target_skill_id = str(state.status_flags.get("resume_to_skill_id") or "").strip()
        if registry and registry.get(target_skill_id):
            state.route_history.append(
                {"from": state.active_skill_id, "to": target_skill_id, "scene": "resume", "reason": "用户要求回到之前的 skill"}
            )
            state.active_skill_id = target_skill_id
            state.status_flags["resume_to_skill_id"] = ""
            if logger:
                logger.log("route.resume", target_skill_id=target_skill_id)
        return
    available_bundles = (
        registry.enabled_bundles()
        if registry
        else {current_bundle.contract.skill_id: current_bundle}
    )
    route_decision = choose_route(current_bundle, state, available_bundles=available_bundles)
    if not route_decision.should_route:
        return
    if registry and registry.get(route_decision.target_skill_id):
        mark_route_interruption(state, target_skill_id=route_decision.target_skill_id)
        state.route_history.append(
            {
                "from": state.active_skill_id,
                "to": route_decision.target_skill_id,
                "scene": route_decision.scene,
                "reason": route_decision.reason,
            }
        )
        state.active_skill_id = route_decision.target_skill_id
        target_bundle = registry.get(route_decision.target_skill_id)
        if target_bundle and target_bundle.contract.stages:
            state.stage = target_bundle.contract.stages[0].id
        state.status_flags["pending_route_scene"] = ""
        if logger:
            logger.log(
                "route.switch",
                target_skill_id=route_decision.target_skill_id,
                scene=route_decision.scene,
                reason=route_decision.reason,
            )


def _tool_exchange_messages(call: ToolCallRequest, result: ToolCallResult, *, tool_mode: str) -> list[ChatMessage]:
    if tool_mode == "native":
        return [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=(call,),
            ),
            ChatMessage(
                role="tool",
                content=result.content if result.ok else json.dumps({"error": result.error}, ensure_ascii=False),
                tool_call_id=call.id,
                name=call.name,
            ),
        ]
    return [
        ChatMessage(
            role="assistant",
            content=json.dumps(
                {
                    "action": "call_tool",
                    "tool_name": call.name,
                    "arguments": call.arguments,
                },
                ensure_ascii=False,
            ),
        ),
        ChatMessage(
            role="tool",
            content=result.content if result.ok else json.dumps({"error": result.error}, ensure_ascii=False),
            tool_call_id=call.id,
            name=call.name,
        ),
    ]


def _is_empty_tool_call(call: ToolCallRequest) -> bool:
    if call.name == "status_track":
        return False
    if call.name in {"rag", "web_search"}:
        query = call.arguments.get("query")
        return query is not None and not str(query).strip()
    if call.name == "mcp":
        server_name = call.arguments.get("server_name")
        tool_name = call.arguments.get("tool_name")
        return not str(server_name or "").strip() or not str(tool_name or "").strip()
    return False


def _sanitize_assistant_reply(reply: str, *, response_policy: ResponsePolicyConfig | None = None) -> str:
    cleaned = reply.strip()
    cleaned = re.sub(r"^(?:(?:user|assistant)\>\s*)+", "", cleaned, flags=re.IGNORECASE)
    cleaned = "\n".join(
        re.sub(r"^(?:(?:user|assistant)\>\s*)+", "", line, flags=re.IGNORECASE)
        for line in cleaned.splitlines()
    ).strip()
    if response_policy and response_policy.sanitize_output:
        cleaned = _sanitize_citation_mentions(cleaned, response_policy=response_policy)
    return cleaned or reply.strip()


def _sanitize_citation_mentions(reply: str, *, response_policy: ResponsePolicyConfig) -> str:
    cleaned = reply
    if not response_policy.allow_file_name_mentions:
        cleaned = re.sub(
            r"(?:references/)?\d{2}_[^\s，。！？；：:\"'`（）()]+\.md",
            "平台内知识库",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"[A-Za-z0-9_\-/]+\.md",
            "平台内知识库",
            cleaned,
            flags=re.IGNORECASE,
        )
    if not response_policy.allow_reference_id_mentions:
        cleaned = re.sub(r"参考文献\s*[0-9一二三四五六七八九十、,，和及与 ]+", "平台内知识库", cleaned)
        cleaned = re.sub(r"参考知识库\s*[0-9一二三四五六七八九十、,，和及与 ]*", "平台内知识库", cleaned)
    cleaned = re.sub(
        r"结合(?:平台内知识库\s*(?:和|及|与|&)?\s*)+",
        "结合平台内知识库",
        cleaned,
    )
    cleaned = re.sub(r"平台内知识库(?:\s*[、,，和及与&]\s*平台内知识库)+", "平台内知识库", cleaned)
    cleaned = re.sub(r"平台内知识库的画像规则", "平台内的画像规则", cleaned)
    cleaned = re.sub(r"平台内知识库的相关规则", "平台内的相关规则", cleaned)
    cleaned = re.sub(r"\(\s*平台内知识库\s*\)", "", cleaned)
    return cleaned.strip()


def _sanitize_user_input(user_input: str) -> str:
    raw_text = user_input.strip()
    if not raw_text:
        return ""
    cleaned_lines: list[str] = []
    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if re.match(r"^assistant\>\s*", stripped, flags=re.IGNORECASE):
            continue
        normalized = re.sub(r"^(?:user\>\s*)+", "", stripped, flags=re.IGNORECASE).strip()
        if normalized:
            cleaned_lines.append(normalized)
    if cleaned_lines:
        return "\n".join(cleaned_lines).strip()
    return re.sub(r"^(?:(?:user|assistant)\>\s*)+", "", raw_text, flags=re.IGNORECASE).strip()
