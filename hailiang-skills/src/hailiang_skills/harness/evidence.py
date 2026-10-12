"""Keep raw evidence durable while projecting bounded excerpts into model input."""
from __future__ import annotations
import json


def project_evidence(tool, context_tokens):
    raw = json.dumps(tool['result'], ensure_ascii=False, default=str)
    limit = max(1000, min(24000, int(context_tokens)))
    if len(raw) <= limit:
        return raw, None
    metadata = {'source_tool_id': tool['id'], 'original_chars': len(raw), 'retained_chars': limit, 'truncated': True}
    # The envelope retains the native tool result message/id and points to the
    # full stored evidence. Excerpts explicitly cannot represent unseen material.
    projected = {'evidence_projection': metadata, 'status': tool['status'],
                 'excerpt_start': raw[:limit * 3 // 4], 'excerpt_end': raw[-limit // 4:]}
    return json.dumps(projected, ensure_ascii=False), metadata
