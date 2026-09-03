import type { MessagePresentation } from "@/utils/api";
import type { SseV2State } from "@/types/streamEvents";

/**
 * The formal chat endpoint and candidate workbench streams use the same
 * display projection. Their transport endpoints intentionally differ, but a
 * message state always becomes one presentation object for shared renderers.
 */
export function presentationFromSseState(state: SseV2State): MessagePresentation {
  return {
    assistant: state.assistant,
    intent: state.intent,
    form: state.form,
    path_options: state.path_options,
    skill_rooms: state.skill_rooms,
    team_handoff: state.team_handoff,
    expert: state.expert,
    skill_transition: state.skill_transition,
    session: state.session,
    risk: state.risk,
    error: state.error,
  };
}

export function isTerminalSseState(status: SseV2State["status"]): boolean {
  return status !== "streaming";
}
