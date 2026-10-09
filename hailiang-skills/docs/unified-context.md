# Unified conversation context

Candidate tests and deployed conversations share context contract version 3.
`HAILIANG_UNIFIED_CONTEXT_ENABLED=false` restores version 2 behavior without
changing the conversation API. Version 3 is enabled by default.

The Expert chooses an authorized Skill once from its capability catalog.
Skill execution cannot inherit an earlier Expert draft. Final messages carry
their turn, source user message, reply source and generation status.

SessionContext fact records are authoritative. Runtime fact dictionaries and
confirmed fact lists are projections. Planner facts require a user source;
normalized values must include an exact `evidence` excerpt. Unverified legacy
runtime values are not promoted to confirmed facts. User corrections take
precedence over earlier evidence.

The question ledger stores pending questions and answered evidence. Ordinal
answers refer to the most recent unresolved set of alternatives. Switching
Skills carries that evidence forward. Native questionnaires retain their
declared question IDs and schema validation. Free-text planner stage/topic
labels remain diagnostics and do not independently advance workflow.

Reply validation retries empty or exactly repeated text once. Similar text,
repeated confirmations and script-result wording produce diagnostics. A failed
retry returns `本轮回复生成失败，请重试`; it does not invent a missing fact or
register a new question. Final prose is buffered until validation, so first
visible prose may arrive later than before; runtime progress events continue.

Workbench exports retain full plaintext prompts in `prompt_trace`, including
combined-planner and reply-retry requests. Execution trace omits duplicate
prompt bodies. Generation events correlate drafts, final output and failures.
A missing provider finish reason leaves truncation unknown.

Verification covers ordinal answers, fact provenance, normalized evidence,
Skill handoff, the reported seven-turn state sequence, exact-repeat retries
and interrupted streams. These deterministic tests do not establish live-model
quality; repeat the candidate conversation after restarting the backend.
