from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
import threading

from hailiang_skills.core.context import SessionContext
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState, ToolCallRequest, ToolCapability, ToolRegistry
from hailiang_skills.skill_runtime.tools import execute_tool_call, run_local_rag, run_web_search


class HarnessIntegration:
    def __init__(self, orchestrator, store=None):
        self.orchestrator = orchestrator
        self.store = store
        self._clients = {}
        self._clients_lock = threading.Lock()

    def context(self, run):
        return SessionContext(session_id=run["session_id"], user_id=run["user_id"], profile_id=run["profile_id"],
                              session_meta={"configuration_snapshot": run["policy"].get("snapshot", {})})

    def client(self, run):
        frozen = run["policy"].get("model_config")
        if frozen:
            from hailiang_skills.skill_runtime.models import LLMConfig
            from hailiang_skills.skill_runtime.llm_client import OpenAICompatibleChatClient
            key = json.dumps(frozen, sort_keys=True)
            with self._clients_lock:
                if key not in self._clients:
                    self._clients[key] = OpenAICompatibleChatClient(LLMConfig(**frozen, api_key=os.getenv(frozen["api_key_env"], "")))
                return self._clients[key]
        client = self.orchestrator._runtime_client_for_context(self.context(run))
        if client is None:
            raise ValueError("LLM_CLIENT_UNAVAILABLE")
        return client

    def bundle(self, run, skill_id):
        context = self.context(run)
        frozen = run["policy"]["skills"][skill_id]
        if frozen.get("assets"):
            from .assets import materialize_assets
            from hailiang_skills.skill_runtime.skill_loader import load_skill_bundle_from_directory
            bundle = load_skill_bundle_from_directory(materialize_assets(frozen["assets"]))
            registry_data = frozen["tool_registry"]
            return replace(bundle, _skill_markdown=frozen["rules"], tool_registry=ToolRegistry(capabilities=tuple(ToolCapability(**c) for c in registry_data["capabilities"]), settings=deepcopy(registry_data["settings"])))
        entries = run["policy"].get("snapshot", {}).get("entries")
        if entries:
            from hailiang_skills.workbench.catalog import build_runtime_registries
            registry, _, _ = build_runtime_registries(entries)
        else:
            registry = self.orchestrator.runtime_registry
        bundle = registry.get(skill_id)
        if bundle is None:
            raise ValueError("SKILL_UNAVAILABLE")
        bundle = self.orchestrator._configured_bundle(context, bundle)
        frozen = run["policy"]["skills"][skill_id]
        # Use captured content/config rather than hot-reloaded role instructions.
        registry_data = frozen["tool_registry"]
        return replace(bundle, _skill_markdown=frozen["rules"],
                       tool_registry=ToolRegistry(capabilities=tuple(ToolCapability(**c) for c in registry_data["capabilities"]), settings=deepcopy(registry_data["settings"])))

    def tool(self, run, task, tool):
        skill_ids = [task["skill_id"]] if task["kind"] == "skill" else (
            run["policy"]["independent_skills"] if task["kind"] == "main" else run["policy"]["experts"][task["expert_id"]]["skills"])
        if task["kind"] != "skill" and tool["name"] in {"rag_search", "web_search"}:
            skill_ids = list(run["policy"]["skills"])
        persisted = deepcopy(task.get("runtime_state", {}))
        persisted.pop("messages", None)
        persisted.pop("session_id", None)
        state = SessionState(session_id=task["id"], messages=[ChatMessage(role="user", content=task["goal"])], **persisted)
        facts = deepcopy(run.get("seed_facts", {}))
        if self.store:
            facts.update(self.store.fact_records(run, task))
        state.global_facts = {record["key"]: record["value"] for record in facts.values() if record.get("status") == "confirmed"}
        state.collected_info = {**state.collected_info, **state.global_facts}
        state.status_flags["harness_idempotency_key"] = tool["id"]
        result = []
        for skill_id in skill_ids:
            if skill_id not in run["policy"]["skills"]:
                continue
            name = "rag" if tool["name"] == "rag_search" else tool["name"]
            frozen = run["policy"]["skills"][skill_id]
            if name not in {s["name"] for s in frozen["specs"]}:
                continue
            bundle = self.bundle(run, skill_id)
            state.active_skill_id = skill_id
            if name == "rag":
                result.extend(asdict(item) for item in run_local_rag(bundle, state, query_override=tool["arguments"].get("query")))
            elif name == "web_search":
                return asdict(run_web_search(bundle, state, query_override=tool["arguments"].get("query")))
            else:
                response = asdict(execute_tool_call(bundle, state, ToolCallRequest(tool["call_id"], name, tool["arguments"])))
                response["_harness_runtime_state"] = asdict(state)
                return response
        return {"results": result} if tool["name"] == "rag_search" else {"error": "TOOL_UNAVAILABLE"}
