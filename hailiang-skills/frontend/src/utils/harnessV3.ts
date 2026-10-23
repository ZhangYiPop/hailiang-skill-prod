import type { HarnessRun, HarnessSnapshot } from '@/types/harness';
export type Entity = { id: string; version: number; run_id?: string; [key: string]: unknown };
export type V3Snapshot = { protocol: 'hailiang.sse.v3'; session_id: string; profile_id: string | null; cursor: number; runtime_mode: string; runs: Entity[]; nodes: Entity[]; input_requests: Entity[]; input_groups?: Entity[]; messages: Entity[] };
/** Public V3 wire frame. Like V2 state, `snapshot` replaces all local state. */
export type V3Event = { protocol: string; seq: number; session_id: string; profile_id: string | null; run_id: string | null; status: string; outcome?: string | null; reason: string; snapshot: V3Snapshot };
export function reduceV3(state: V3Snapshot, event: V3Event): V3Snapshot {
  if (event.protocol !== 'hailiang.sse.v3' || event.session_id !== state.session_id || event.profile_id !== state.profile_id) throw new Error('V3_SCOPE_MISMATCH');
  if (event.seq <= state.cursor) return state;
  if (event.reason === 'resync_required') throw new Error('V3_RESYNC_REQUIRED');
  return event.snapshot;
}
export function legacyView(snapshot: V3Snapshot): HarnessSnapshot {
  return {runtime_mode: snapshot.runtime_mode, cursor: snapshot.cursor, runs: snapshot.runs.map(entity => {
    const message = snapshot.messages.find(m => m.run_id === entity.id && m.kind === 'final');
    return {...entity, run_id: entity.id,
      nodes: snapshot.nodes.filter(n => n.run_id === entity.id),
      inputs: snapshot.input_requests.filter(q => q.run_id === entity.id).map(q => ({...q, version: q.request_version})),
      input_groups: (snapshot.input_groups || []).filter(group => group.run_id === entity.id),
      result: message?.content || entity.result || '',
      message_status: message?.status,
    } as unknown as HarnessRun;
  })};
}
export function subscribeV3(load: () => Promise<V3Snapshot>, url: string,
  onState: (state: V3Snapshot) => void, onConnection: (state: string) => void): () => void {
  let stopped = false, syncing = false;
  let current: V3Snapshot | undefined, source: EventSource | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const types = ['state', 'done_suspended', 'done_timeout', 'done'];
  const synchronize = async () => {
    if (stopped || syncing) return;
    syncing = true; source?.close();
    try {
      const snapshot = await load();
      if (stopped) return;
      current = snapshot; onState(current);
      const target = new URL(url, window.location.href);
      target.searchParams.set('after', String(current.cursor));
      source = new EventSource(target.toString());
      const receive = (raw: Event) => {
        if (stopped || !current) return;
        try { current = reduceV3(current, JSON.parse((raw as MessageEvent).data)); onState(current); }
        catch { void synchronize(); }
      };
      for (const type of types) source.addEventListener(type, receive);
      source.onopen = () => { if (!stopped) onConnection('connected'); };
      source.onerror = () => { if (!stopped) onConnection('reconnecting'); };
    } catch {
      if (!stopped) { onConnection('reconnecting'); timer = setTimeout(() => void synchronize(), 1500); }
    } finally { syncing = false; }
  };
  void synchronize();
  return () => { stopped = true; source?.close(); if (timer) clearTimeout(timer); };
}
