"""Exact, bounded reads from an actor's already-authorized Skill documents."""
import json

from .contracts import HarnessError
from .policy import allowed_skills, normalize_skill


def _field_hints(rows):
    """Bounded discovery hints; examples are samples, never an exhaustive enum."""
    fields = {}
    for row in rows[:100]:
        for key, value in row.items():
            if key not in fields and len(fields) >= 20:
                continue
            samples = fields.setdefault(key, [])
            if isinstance(value, (str, int, float, bool)) and value not in samples and len(samples) < 3:
                if len(str(value)) <= 80:
                    samples.append(value)
    return {'fields': fields, 'sampled_records': min(len(rows), 100),
            'notice': '字段值仅为样例，不是全部可选值；请按实际字段筛选，空结果不代表其他字段也没有相关资料。'}


def readable_skills(run, task):
    candidates = [task['skill_id']] if task['kind'] == 'skill' else allowed_skills(run, task)
    return [sid for sid in candidates if any(s['name'] == 'rag' for s in run['policy']['skills'][sid]['specs'])]


def read_asset(run, task, args, bundle_loader):
    from hailiang_skills.skill_runtime.tools import _iter_local_documents
    sid = normalize_skill(args['skill_id'])
    if sid not in readable_skills(run, task):
        raise HarnessError('SKILL_NOT_AUTHORIZED', 403)
    documents = list(_iter_local_documents(bundle_loader(run, sid)))
    source = args.get('source')
    offset, limit = args.get('offset', 0), args.get('limit', 20)
    search = args.get('search')
    context_lines = args.get('context_lines', 5)
    if not source:
        page = documents[offset:offset + limit]
        return {'skill_id': sid, 'documents': [{'title': title, 'source': src, 'chars': len(body)} for title, src, body in page],
                'next_offset': offset + len(page) if offset + len(page) < len(documents) else None}
    matches = [(src, body) for title, src, body in documents if source in {src, title}]
    if len(matches) != 1:
        raise HarnessError('ASSET_SOURCE_NOT_FOUND' if not matches else 'ASSET_SOURCE_AMBIGUOUS')
    source, content = matches[0]
    try:
        payload = json.loads(content)
    except ValueError:
        if search is not None:
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if search.lower() in line.lower():
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    window = '\n'.join(lines[start:end])
                    return {'source': source, 'text': window, 'search': search,
                            'match_line': i, 'context_lines': context_lines,
                            'offset': start, 'total_lines': len(lines)}
        if args.get('filters') or args.get('score') is not None:
            raise HarnessError('ASSET_FILTER_REQUIRES_RECORDS')
        size = min(args.get('max_chars', 4000), 8000)
        return {'source': source, 'text': content[offset:offset + size], 'offset': offset,
                'next_offset': offset + size if offset + size < len(content) else None}
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise HarnessError('ASSET_EXPECTED_RECORD_ARRAY')
    if search is not None:
        matched_indices = []
        for index, row in enumerate(payload):
            for key, value in row.items():
                if isinstance(value, str) and search.lower() in value.lower():
                    matched_indices.append(index)
                    break
        if matched_indices:
            first_match = matched_indices[0]
            start = max(0, first_match - context_lines)
            end = min(len(payload), first_match + context_lines + 1)
            window_records = [
                {'record_index': i, 'record': payload[i], 'search_match': i in matched_indices}
                for i in range(start, end)
            ]
            return {'source': source, 'matched_count': len(matched_indices),
                    'records': window_records, 'search': search,
                    'context_lines': context_lines, 'first_match_index': first_match,
                    'notice': '资料匹配记录，不代表当年录取保证；年份与口径以记录来源为准。'}
    filters = args.get('filters', {})
    score = args.get('score')
    selected = []
    for index, row in enumerate(payload):
        if any(key not in row or row[key] != value for key, value in filters.items()):
            continue
        if score is not None:
            low, high = row.get('min_score'), row.get('max_score')
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)) or not low <= score <= high:
                continue
        selected.append({'record_index': index, 'record': row})
    page = selected[offset:offset + limit]
    result = {'source': source, 'matched_count': len(selected), 'records': page,
            'next_offset': offset + len(page) if offset + len(page) < len(selected) else None,
            'notice': '资料匹配记录，不代表当年录取保证；年份与口径以记录来源为准。'}
    if not selected:
        result['filter_hints'] = _field_hints(payload)
    # 高吞吐提示：剩余记录较多时，建议加大 limit 一次取完，避免逐页翻查浪费调用预算。
    remaining = len(selected) - offset - len(page)
    if remaining > len(page):
        result['notice'] += f' 当前返回{len(page)}条，剩余{remaining}条；可增大 limit（最大10）一次读取更多，减少逐页调用。'
    return result
