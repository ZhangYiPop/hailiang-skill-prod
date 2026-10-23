import { describe, it, expect } from 'vitest';
import { reduceV3, type V3Snapshot, type V3Event } from './harnessV3';
const state: V3Snapshot = {protocol:'hailiang.sse.v3',session_id:'s',profile_id:null,cursor:1,runtime_mode:'harness_v1',runs:[],nodes:[],input_requests:[],messages:[{id:'m',version:1,revision:1,content:'中😀'}]};
const replacement: V3Snapshot = {...state,cursor:2,messages:[{id:'m',version:2,revision:1,content:'中😀文'}]};
const event: V3Event = {protocol:'hailiang.sse.v3',session_id:'s',profile_id:null,run_id:'r',seq:2,status:'running',reason:'updated',snapshot:replacement};
describe('v3 full-state reducer', () => {
  it('replaces state and ignores duplicate frames', () => {
    const next=reduceV3(state,event);
    expect(next).toEqual(replacement);
    expect(reduceV3(next,event)).toBe(next);
  });
  it('rejects scope mismatches and asks caller to reload after resync', () => {
    expect(()=>reduceV3(state,{...event,profile_id:'other'})).toThrow('SCOPE');
    expect(()=>reduceV3(state,{...event,reason:'resync_required'})).toThrow('RESYNC');
  });
  it('accepts a new full snapshot without delta or Unicode offset logic', () => {
    const restarted: V3Snapshot = {...replacement,cursor:3,messages:[{id:'m',version:3,revision:2,content:'重新生成'}]};
    const next=reduceV3(replacement,{...event,seq:3,status:'running',snapshot:restarted});
    expect(next.messages[0].content).toBe('重新生成');
    expect(next.messages[0].revision).toBe(2);
  });
});
