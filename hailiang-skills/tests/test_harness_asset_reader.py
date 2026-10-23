import json
from types import SimpleNamespace

import pytest

from hailiang_skills.harness.asset_reader import read_asset
from hailiang_skills.harness.contracts import HarnessError
from test_harness import policy


def fixture():
    p = policy()
    p['skills']['internal']['specs'] = [{'name': 'rag'}]
    bundle = SimpleNamespace(references={}, local_assets={'bands.json': json.dumps([
        {'province': '浙江', 'min_score': 670, 'max_score': 750, 'label': '高分段'},
        {'province': '浙江', 'min_score': 550, 'max_score': 589, 'label': '目标记录'},
        {'province': '江苏', 'min_score': 550, 'max_score': 589, 'label': '另一省'},
    ], ensure_ascii=False)}, asset_domains={}, runtime_metadata=SimpleNamespace(assets=SimpleNamespace(generated_domains=[])))
    return {'policy': p}, {'kind': 'expert', 'expert_id': 'a'}, lambda *_: bundle


def test_exact_score_record_not_first_keyword_match():
    run, task, loader = fixture()
    args = {'skill_id': 'internal', 'source': 'bands.json', 'filters': {'province': '浙江'}, 'score': 580}
    result = read_asset(run, task, args, loader)
    assert result['matched_count'] == 1
    assert result['records'][0]['record']['label'] == '目标记录'
    assert result['records'][0]['record_index'] == 1
    assert result['next_offset'] is None


def test_reader_does_not_bypass_permissions_or_accept_filesystem_paths():
    run, _, loader = fixture()
    with pytest.raises(HarnessError, match='SKILL_NOT_AUTHORIZED'):
        read_asset(run, {'kind': 'main'}, {'skill_id': 'internal'}, loader)
    with pytest.raises(HarnessError, match='ASSET_SOURCE_NOT_FOUND'):
        read_asset(run, {'kind': 'skill', 'skill_id': 'internal'}, {'skill_id': 'internal', 'source': '../../env.local.sh'}, loader)


def test_directory_and_filtered_record_pagination():
    run, task, loader = fixture()
    directory = read_asset(run, task, {'skill_id': 'internal'}, loader)
    assert directory['documents'][0]['source'] == 'local_asset:bands.json'
    first = read_asset(run, task, {'skill_id': 'internal', 'source': 'bands.json', 'filters': {'province': '浙江'}, 'limit': 1}, loader)
    second = read_asset(run, task, {'skill_id': 'internal', 'source': 'bands.json', 'filters': {'province': '浙江'}, 'limit': 1, 'offset': first['next_offset']}, loader)
    assert first['records'][0]['record_index'] == 0
    assert second['records'][0]['record_index'] == 1
    assert second['next_offset'] is None


def test_zero_matches_exposes_bounded_field_samples_without_changing_filters():
    run, task, loader = fixture()
    result = read_asset(run, task, {'skill_id': 'internal', 'source': 'bands.json',
                                  'filters': {'region': '浙江'}}, loader)
    assert result['matched_count'] == 0
    assert result['records'] == []
    assert result['filter_hints']['fields']['province'] == ['浙江', '江苏']
    assert 'region' not in result['filter_hints']['fields']


def test_field_hints_do_not_copy_large_or_nested_values():
    from hailiang_skills.harness.asset_reader import _field_hints
    rows = [{f'field{i}': 'x' * 10000 for i in range(100)},
            {'nested': {'private': 'large'}, 'field0': 'short'}] * 100
    hints = _field_hints(rows)
    assert len(hints['fields']) == 20
    assert hints['sampled_records'] == 100
    assert hints['fields']['field0'] == ['short']
    assert len(json.dumps(hints)) < 2000
