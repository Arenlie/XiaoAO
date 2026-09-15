import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.catalog import FUZZY_ENTITY_AGENT_ID
from app.integrations.dify.entity_identity import build_candidate_id
from app.orchestration.entity_lifecycle import current_turn_entity, has_explicit_different_code
from app.orchestration.sensor_identity import prepare_identity, sensor_verdict, validate_result, synthesis_observations
from app.orchestration.runtime import graph_runtime_scope
from app.orchestration.supervisor.contracts import AgentCall
from app.orchestration.knowledge_enrichment import enrichment_query
from app.services.sensor_references import persist_references, load_references, referenced_fault, code_tokens
from app.tools.contracts import ToolCallResult
from app.tools.dify_knowledge import KNOWLEDGE_TOOL_ID, knowledge_descriptor
from app.tools.phm_asset_mcp import PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID
from app.tools.phm_sensor_context import build_sensor_arguments
from app.tools.phm_sensor_mcp import SENSOR_TOOL_BY_OPERATION, SENSOR_EVIDENCE_TOOL_ID
from app.workflows.contracts import WorkflowIntentClassification
from test_sensor_integration import make_nodes, classification, run
from test_v11_orchestration import make_runtime
from test_v140_diagnosis_confirmation import db, store_context

EQ = 'BBGPG02010160030041'
PT = 'BBGPG0201016003004101G04VA'
PARAM = 'BBGPG0201016003004101G04VT000'
FID = 'FLT-37F2827041399E7EF6E4DD436493E7F4'
QUERY = f'{PT} 温度异常 | 待确认 | 设备 {EQ}，分析这个传感器故障'


def fault(**kwargs):
    return {'fault_id': FID, 'equip_num': EQ, 'point_num': PT, 'param_num': PARAM,
        'fault_type_name': '温度异常', 'fault_status': 'PENDING_CONFIRMATION', 'fault_status_name': '待确认',
        'analysis_result': 'AI复核判断：确认故障。温度持续950.366℃，需核验供电和信号链路。',
        'analysis_complete': True, 'captured_at': datetime.now(timezone.utc).isoformat(),
        'source_message_id': 'original-message', 'display_index': 1, **kwargs}


def probe(*, equipment=True, targets=True):
    return {'success': True, 'identity_resolution': {
        'targets': [{'equip_num': EQ, 'point_num': PT, 'aliases': [PT, PARAM]}] if targets else [],
        'match_count': int(targets), 'registry': {'complete': True, 'returned_by_upstream': 9086},
        'asset_entities': [{'entity_type': 'equipment', 'equip_no': EQ, 'equip_name': '实际设备'}] if equipment else []}}


def obs(row, tool=SENSOR_TOOL_BY_OPERATION['active']):
    return {'tool_id': tool, 'can_support_final_answer': True, 'status': 'SUCCESS',
            'tool_result': {'structured_content': {'records': [row]}}}


def test_optional_null_time_keeps_codes_and_never_silently_uses_start_time():
    c = classification('history', 'point', '温度异常', False)
    c['sensor_query']['time_field'] = None
    c['asset_semantics']['equip_no'] = {'raw_text': EQ, 'retrieval_text': EQ}
    parsed = WorkflowIntentClassification.model_validate(c).model_dump(mode='json')
    assert parsed['asset_semantics']['equip_no']['raw_text'] == EQ
    parsed['time_range'] = {'mode': 'range', 'start_time': '2026-09-01', 'end_time': '2026-09-09'}
    state = {'query': '按结束时间查询', 'business_intent': parsed,
             'sensor_target': {**fault(), 'verified': True}}
    args, missing = build_sensor_arguments(SENSOR_TOOL_BY_OPERATION['history'], state, {})
    assert missing and not args
    parsed['sensor_query']['time_field'] = 'end_time'
    args, missing = build_sensor_arguments(SENSOR_TOOL_BY_OPERATION['history'], state, {})
    assert not missing and args['end_time_from'] == '2026-09-01' and 'start_time_from' not in args


def test_nine_gy000_points_have_nine_stable_ids_and_source_independent_codes():
    rows = [{'entity_type': 'point', 'entity_key': f'point:{n}', 'equip_no': f'DEV-{n}', 'point_no': 'GY000'} for n in range(9)]
    assert len({build_candidate_id(r) for r in rows}) == 9
    for row in rows:
        row.pop('entity_key')
    assert len({build_candidate_id(r) for r in rows}) == 9
    assert code_tokens('设备 DEV-2 点 GY000；故障 '+FID) == ['DEV-2', 'GY000']


def test_same_equipment_different_point_must_switch_and_wrong_selected_entity_cannot_win():
    wrong = {'equip_no': EQ, 'point_no': 'GY000', 'entity_type': 'point'}
    assert has_explicit_different_code(QUERY, wrong)
    assert current_turn_entity({'query': QUERY, 'selected_entity': wrong, 'resolved_entity': wrong}) == {}


@pytest.mark.parametrize('equipment,mode', [(True, 'normal'), (False, 'normal'), (True, 'quick')])
async def test_original_pasted_fault_runs_real_graph_without_unrelated_selection(equipment, mode):
    c = classification('active', 'point', '温度异常', False)
    c['sensor_query']['time_field'] = None
    nodes, asset, model, _ = make_nodes(c)
    asset.call_tool = AsyncMock(return_value=probe(equipment=equipment))
    calls = []
    inflight = peak = 0
    async def execute(*, request, **kwargs):
        nonlocal inflight, peak
        calls.append(request)
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(.005)
        inflight -= 1
        assert request.arguments.get('equip_num', request.arguments.get('equip_no', EQ)) == EQ
        if request.tool_id == SENSOR_EVIDENCE_TOOL_ID:
            assert request.arguments['point_num'] == PT and request.arguments['fault_id'] == FID
            data = {'success': True, 'query_type': 'FAULT_EVIDENCE', 'record': fault()}
        elif request.tool_id == SENSOR_TOOL_BY_OPERATION['active']:
            assert request.arguments['point_num'] == PT
            data = {'success': True, 'filters': request.arguments, 'records': [], 'message': '本次当前列表未匹配记录。'}
        elif request.tool_id == PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID:
            data = {'success': True, 'equipment': {'equip_no': EQ, 'equip_name': '实际设备'}}
        elif request.tool_id == KNOWLEDGE_TOOL_ID:
            assert '温度异常' in request.arguments['query']
            data = {'success': True, 'sources': []}
        else:
            raise AssertionError(request.tool_id)
        return ToolCallResult(tool_id=request.tool_id, status='SUCCESS', structured_content=data)
    nodes.tool_executor.execute = execute
    settings = nodes.supervisor.settings
    settings.dify_knowledge_enabled = True
    settings.dify_knowledge_api_key = 'test-only'
    nodes.tool_registry.register_tool(knowledge_descriptor(settings))
    rt = make_runtime()
    initial = {'query': QUERY,
        'selected_entity': {'equip_no': 'WRONG-1', 'point_no': 'GY000'},
        'memory_context': {'sensor_fault_references': [[fault()]]}}
    if mode == 'quick':
        from app.orchestration.graphs.quick_graph import build_quick_graph
        with graph_runtime_scope(rt):
            result = await build_quick_graph(nodes).ainvoke({'task_id': rt.agent_runtime.task_id,
                'conversation_id': 'C', 'branch_id': 'B', 'execution_mode': 'quick', 'observations': [], **initial})
    else:
        result = await run(nodes, rt, initial)
    assert result['final_status'] == 'COMPLETED'
    assert result['sensor_target']['point_num'] == PT
    assert SENSOR_EVIDENCE_TOOL_ID in [c.tool_id for c in calls]
    assert (PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID in [c.tool_id for c in calls]) == equipment
    assert len({c.tool_id for c in calls}) == len(calls)
    assert peak >= 2
    asset.lookup.assert_not_awaited()
    model.plan_tool_calls.assert_not_awaited()
    assert not any('diagnos' in c.tool_id for c in calls)


async def test_missing_registry_does_not_discard_reference_and_missing_asset_is_explicit():
    nodes, asset, _, _ = make_nodes(classification('active', 'point'))
    asset.call_tool = AsyncMock(side_effect=TimeoutError())
    rt = make_runtime()
    with graph_runtime_scope(rt):
        update = await prepare_identity(nodes, {'query': QUERY, 'memory_context': {'sensor_fault_references': [[fault()]]}}, classification('active', 'point', '温度异常'))
    assert update['sensor_target']['verified'] and not update['resolved_entity']
    args, missing = build_sensor_arguments(SENSOR_EVIDENCE_TOOL_ID, update, {'fault_id': FID})
    assert not missing and args['equip_num'] == EQ


def test_wrong_target_empty_response_is_not_original_target_negative_evidence():
    state = {'sensor_target': {**fault(), 'verified': True}}
    assert not validate_result(state, {'equip_num': 'WRONG-1', 'point_num': 'GY000'}, {'records': []})
    assert not validate_result(state, {'equip_num': EQ, 'point_num': PT}, {'filters': {'equip_num': 'WRONG-1'}, 'records': []})
    assert validate_result(state, {'equip_num': EQ, 'point_num': PT}, {'records': []})
    assert not validate_result(state, {'equip_num': EQ, 'point_num': PT, 'fault_id': FID}, {'record': fault(fault_id='OTHER')})


def test_source_order_is_not_display_order_and_parent_path_scopes_references():
    second = fault(fault_id='FLT-SECOND2', point_num='REAL-POINT2')
    answer = f'1. REAL-POINT2 温度异常\n2. {PT} 温度异常'
    meta = persist_references({'assistant_message_id': 'a', 'observations': [obs(fault()), obs(second)]}, answer)
    loaded = load_references([SimpleNamespace(id='a', metadata_json=meta)])
    assert referenced_fault({'query': '分析第2条', 'memory_context': {'sensor_fault_references': loaded}})[0]['point_num'] == PT
    assert referenced_fault({'query': '分析第2条', 'memory_context': {'sensor_fault_references': []}}) == (None, None)
    old = copy.deepcopy(meta)
    for row in old['sensor_fault_references']:
        row['captured_at'] = (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    assert not load_references([SimpleNamespace(id='a', metadata_json=old)])
    assert referenced_fault({'query': '第3条', 'memory_context': {'sensor_fault_references': loaded}})[1]


def test_switch_code_does_not_inherit_previous_fault_and_brief_conclusion_keeps_fault_terms():
    memory = {'sensor_fault_references': [[fault()]]}
    assert referenced_fault({'query': 'DEVICE-NEW2 在线吗', 'memory_context': memory}) == (None, None)
    selected, error = referenced_fault({'query': '直接给我一个结论', 'memory_context': memory})
    assert selected['fault_id'] == FID and not error
    query = enrichment_query({'query': '直接给我一个结论', 'sensor_target': selected})
    assert '温度异常' in query and '950.366' in query and not query.startswith('直接给')


def test_dedup_uses_bound_identity_but_ignores_changed_objective():
    nodes, _, _, _ = make_nodes()
    c = AgentCall(call_id='a', call_type='tool', tool_id=SENSOR_TOOL_BY_OPERATION['active'], objective='查询故障', arguments={'scope': 'point'})
    state = {'sensor_target': {**fault(), 'verified': True, 'revision': 'a'}, 'business_intent': classification('active', 'point')}
    first = nodes._react_action_signature(c, state)
    assert nodes._react_action_signature(c.model_copy(update={'objective': '再看看故障'}), state) == first
    state['sensor_target'] = {**state['sensor_target'], 'equip_num': 'OTHER-2', 'point_num': 'OTHER-POINT2', 'revision': 'b'}
    assert nodes._react_action_signature(c, state) != first


async def test_one_correction_really_rechecks_and_refuses_a_second_repair():
    nodes, asset, _, _ = make_nodes(classification('active', 'point'))
    asset.call_tool = AsyncMock(return_value=probe())
    call = AgentCall(call_id='fix', agent_id=FUZZY_ENTITY_AGENT_ID, objective='重新定位 '+EQ+' '+PT,
                     arguments={'required_entity_level': 'point'})
    state = {'query': QUERY, 'selected_entity': {'equip_no': 'WRONG-2', 'point_no': 'GY000'},
             'business_intent': classification('active', 'point')}
    with graph_runtime_scope(make_runtime()):
        update = await nodes._execute_call(state, call)
        assert update['sensor_target']['point_num'] == PT and update['sensor_identity_corrections'] == 1
        blocked = await nodes._execute_call({**state, 'sensor_identity_corrections': 1}, call)
    assert asset.call_tool.await_count == 1
    assert blocked['observations'][0]['error_code'] == 'IDENTITY_RECHECK_EXHAUSTED'


def test_model_budget_marks_ai_truncation_and_discards_old_target_payload():
    long = '已记录分析' * 5000
    current = {'tool_id': SENSOR_EVIDENCE_TOOL_ID, 'sensor_target_revision': 'new', 'status': 'SUCCESS',
        'can_support_final_answer': True, 'tool_result': {'structured_content': {'record': fault(analysis_result=long)}}}
    previous = {**obs(fault(equip_num='WRONG-2')), 'sensor_target_revision': 'old'}
    output = synthesis_observations({'sensor_target': {'revision': 'new'}, 'observations': [previous, current]})
    assert len(output) == 1
    result = output[0]['tool_result']['structured_content']['record']
    assert result['analysis_complete'] is False and len(result['analysis_result']) == 16000
    assert current['tool_result']['structured_content']['record']['analysis_result'] == long


async def test_legacy_duplicate_selection_is_conflict_and_new_id_selects_correct_device(db):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from app.models.entity_selection import PendingEntitySelection
    from app.models.branch import ConversationBranch
    from app.domain.exceptions import ConflictError
    row = db.seed(status='WAITING_SELECTION')
    await store_context(db, row)
    choices = [{'candidate_id': 'point:GY000', 'entity_type': 'point', 'entity_key': f'point:{i}',
                'equip_no': f'DEV-{i}', 'point_no': 'GY000'} for i in range(9)]
    with Session(db.engine) as session:
        session.add(PendingEntitySelection(task_id=row.tid, conversation_id=row.cid, original_query='输入轴测点',
            candidates=choices, status='PENDING', expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)))
        session.commit()
    def aware(value, context):
        if value.expires_at.tzinfo is None:
            value.expires_at = value.expires_at.replace(tzinfo=timezone.utc)
    event.listen(PendingEntitySelection, 'load', aware)
    try:
        with pytest.raises(ConflictError) as caught:
            await db.service.select_entity(row.tid, 'A', 'point:GY000')
        assert caught.value.code == 'ENTITY_SELECTION_AMBIGUOUS'
        assert not db.queue.enqueue_generation.await_count
        pending = await db.service.get_pending_selection(row.tid, 'A')
        assert len({r['candidate_id'] for r in pending['candidates']}) == 9
        task = await db.service.select_entity(row.tid, 'A', pending['candidates'][8]['candidate_id'])
        assert task.status == 'QUEUED'
        with Session(db.engine) as session:
            assert session.get(ConversationBranch, row.bid).active_entity['equip_no'] == 'DEV-8'
    finally:
        event.remove(PendingEntitySelection, 'load', aware)


async def test_context_builder_loads_references_before_text_compaction(monkeypatch):
    from app.services.context_builder import ContextBuilder
    from app.config import Settings
    from app.services import asset_collections
    monkeypatch.setattr(asset_collections, 'load_collections', AsyncMock(return_value=[]))
    source = SimpleNamespace(id='original', role='ASSISTANT', include_in_context=True, status='COMPLETED',
        content=f'1. {PT} 温度异常', entity_result={}, metadata_json={'sensor_fault_references': [fault()]})
    unrelated = SimpleNamespace(id='last', role='USER', include_in_context=True, status='COMPLETED',
                                content='继续', entity_result={}, metadata_json={})
    repo = SimpleNamespace(path_to_leaf=AsyncMock(return_value=[source, unrelated]))
    settings = Settings.model_construct(context_recent_message_limit=1)
    builder = ContextBuilder(settings, repo)
    context = await builder.build(SimpleNamespace(scalar=AsyncMock(return_value=None)),
        SimpleNamespace(id='branch', conversation_id='owned-conversation', active_leaf_message_id='last', summary=''))
    assert context['sensor_fault_references'][0][0]['source_message_id'] == 'original'
    assert repo.path_to_leaf.call_args.kwargs['conversation_id'] == 'owned-conversation'


def test_versions_dates_measurements_and_labeled_models_are_not_identity_tokens():
    assert not code_tokens('qwen3.8-flash，950.366℃，2026-09-09，型号 LT-100，标准 ISO9001')


async def test_legacy_asset_returns_unrelated_candidates_are_filtered_before_selection():
    nodes, asset, _, _ = make_nodes(classification('active', 'point'), rows=[
        {'entity_type': 'point', 'equip_no': 'WRONG-1', 'point_no': 'GY000'},
        {'entity_type': 'point', 'equip_no': 'WRONG-2', 'point_no': 'GY000'}])
    with graph_runtime_scope(make_runtime()):
        outcome = await nodes.entity_resolution_layer.resolve({'query': QUERY}, required_entity_level='point')
    assert outcome.updates['entity_result']['status'] == 'NOT_FOUND'
    assert outcome.updates['entity_result']['matches'] == []


async def test_two_invalid_model_outputs_keep_literal_target_dependency():
    nodes, asset, model, _ = make_nodes(classification('active', 'point'))
    model.complete_json_profile.side_effect = ValueError('not a JSON object')
    result, selection = await nodes.supervisor.classify_business_workflow({'query': QUERY}, nodes.workflow_registry)
    assert result['classification_degraded'] and result['asset_semantics']['needs_asset_lookup']
    assert code_tokens(QUERY) == [PT, EQ]


def test_old_revision_never_reappears_in_fallback_or_persisted_references():
    from app.orchestration.supervisor.agent import SupervisorAgent
    from app.orchestration.sensor_identity import available
    old = {**obs(fault(equip_num='WRONG-2')), 'sensor_target_revision': 'old', 'answer_markdown': '错误设备的结论'}
    current = {**obs(fault()), 'sensor_target_revision': 'new', 'answer_markdown': '原测点已取得的记录'}
    state = {'observations': [old, current], 'sensor_target': {**fault(), 'revision': 'new', 'verified': True,
        'asset_identity_available': True}, 'resolved_entity': {'equip_no': EQ, 'entity_type': 'equipment'},
        'entity_result': {'resolution_source': 'sensor_identity_verification'}, 'query': QUERY}
    assert SupervisorAgent._observation_fallback(state) == '原测点已取得的记录'
    saved = persist_references(state, '')['sensor_fault_references']
    assert len(saved) == 1 and saved[0]['equip_num'] == EQ
    assert current_turn_entity(state)['equip_no'] == EQ and available(state)
    state['query'] = 'OTHER-DEVICE2是否在线'
    assert current_turn_entity(state) == {} and not available(state)
    candidate = {'entity_type': 'point', 'equip_no': EQ, 'point_no': 'GY000'}
    assert build_candidate_id({**candidate, 'source': 'code_exact'}) == build_candidate_id({**candidate, 'source': 'embedding_rerank'})


def test_changed_live_parameter_alias_cannot_confirm_old_mapping():
    state = {'sensor_target': {**fault(), 'verified': True, 'input_tokens': [EQ, PARAM]}}
    args = {'equip_num': EQ, 'point_num': PT}
    data = {'records': [], 'coverage': {'records': [{'equip_num': EQ, 'point_num': PT,
        'temperature_param_num': 'NEW-PARAM2'}]}}
    assert not validate_result(state, args, data)
    data['coverage']['records'][0]['temperature_param_num'] = PARAM
    assert validate_result(state, args, data)


async def test_independent_sensor_codes_keep_separate_scopes_and_save_successful_references():
    from app.tools.independent_queries import IndependentQueryHandler, MULTI_QUERY_TOOL_ID
    nodes, _, _, _ = make_nodes()
    calls = []
    inflight = peak = 0
    async def call_tool(name, args):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(.005)
        inflight -= 1
        if name == 'query_monitored_sensor_points':
            code = args['identity_tokens'][0]
            return {'success': True, 'identity_resolution': {'targets': [{'equip_num': code,
                'point_num': code+'-POINT1', 'aliases': [code+'-POINT1']}], 'match_count': 1}}
        calls.append(args)
        code = args['equip_num']
        return {'success': True, 'filters': args, 'records': [fault(equip_num=code, point_num=code+'-POINT1', fault_id='FLT-'+code)]}
    asset = SimpleNamespace(call_tool=call_tool, resolve_entity=AsyncMock(side_effect=AssertionError('No fuzzy code fallback')))
    handler = IndependentQueryHandler(asset, None, nodes.supervisor.settings)
    queries = [{'target': code, 'query': code+'有温度异常吗', 'operation': 'sensor_active',
                'entity_level': 'equipment', 'fault_type': '温度异常'} for code in ['DEVICE-1', 'DEVICE-2']]
    request = SimpleNamespace(tool_id=MULTI_QUERY_TOOL_ID, task_id='T', arguments={
        'queries': queries, '_verified_request_context': {'query': 'DEVICE-1和DEVICE-2有温度异常吗'}})
    with graph_runtime_scope(make_runtime()):
        result = await handler(request, None)
    assert result.status == 'SUCCESS' and not result.structured_content['partial']
    assert peak >= 2 and {a['equip_num'] for a in calls} == {'DEVICE-1', 'DEVICE-2'}
    assert all(item['status'] == 'SUCCESS' for item in result.structured_content['items'])
    metadata = persist_references({'observations': [{'tool_id': MULTI_QUERY_TOOL_ID,
        'can_support_final_answer': True, 'tool_result': {'structured_content': result.structured_content}}]}, '')
    assert {r['equip_num'] for r in metadata['sensor_fault_references']} == {'DEVICE-1', 'DEVICE-2'}


async def test_real_graph_rechecks_wrong_empty_response_once_and_retries_original_scope():
    nodes, asset, model, _ = make_nodes(classification('active', 'point', '温度异常', False))
    asset.call_tool = AsyncMock(return_value=probe(equipment=False))
    calls = []
    async def execute(*, request, **kwargs):
        calls.append(request)
        data = {'success': True, 'records': [], 'filters': request.arguments}
        if len(calls) == 1:
            data['filters'] = {'equip_num': 'WRONG-2', 'point_num': 'GY000'}
        return ToolCallResult(tool_id=request.tool_id, status='SUCCESS', structured_content=data)
    nodes.tool_executor.execute = execute
    result = await run(nodes, make_runtime(), {'query': QUERY})
    assert result['final_status'] == 'COMPLETED'
    assert result['sensor_identity_corrections'] == 1 and len(calls) == 2
    assert all(c.arguments['equip_num'] == EQ and c.arguments['point_num'] == PT for c in calls)
    assert calls[-1].arguments['refresh'] is True
    assert asset.call_tool.await_count == 2
    filtered = synthesis_observations(result)
    assert not any('WRONG-2' in str(o) for o in filtered)


async def test_explicit_platform_evidence_uses_verified_device_without_physical_point():
    from app.tools.phm_data_mcp import PHM_QUERY_HEALTH_SCORE_TOOL_ID, PHM_QUERY_ALARM_RECORDS_TOOL_ID
    c = classification('active', 'point', '温度异常', False)
    c['goal_frame']['requested_evidence_types'] = ['health', 'alarm']
    nodes, asset, _, _ = make_nodes(c)
    asset.call_tool = AsyncMock(return_value=probe())
    calls = []
    async def execute(*, request, **kwargs):
        calls.append(request)
        if request.tool_id in {PHM_QUERY_HEALTH_SCORE_TOOL_ID, PHM_QUERY_ALARM_RECORDS_TOOL_ID}:
            assert EQ in request.arguments.values()
            assert 'GY000' not in str(request.arguments)
        data = {'success': True, 'records': [], 'filters': request.arguments}
        if request.tool_id == PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID:
            data = {'success': True, 'equipment': {'equip_no': EQ}}
        return ToolCallResult(tool_id=request.tool_id, status='SUCCESS', structured_content=data)
    nodes.tool_executor.execute = execute
    result = await run(nodes, make_runtime(), {'query': QUERY+'，结合设备健康度和报警信息说明'})
    ids = {r.tool_id for r in calls}
    assert {PHM_QUERY_HEALTH_SCORE_TOOL_ID, PHM_QUERY_ALARM_RECORDS_TOOL_ID}.issubset(ids)
    assert result['final_status'] == 'COMPLETED' and not result['sensor_target']['asset_points']
    assert not any('diagnos' in tool for tool in ids)


async def test_first_coded_analysis_reads_full_detail_after_current_result_without_saved_reference():
    nodes, asset, _, _ = make_nodes(classification('active', 'point', '温度异常', False))
    asset.call_tool = AsyncMock(return_value=probe(equipment=False))
    calls = []
    async def execute(*, request, **kwargs):
        calls.append(request)
        data = {'success': True, 'filters': request.arguments}
        if request.tool_id == SENSOR_EVIDENCE_TOOL_ID:
            assert request.arguments['fault_id'] == FID
            data.update(query_type='FAULT_EVIDENCE', record=fault(analysis_result='完整复核内容'*2000))
        else:
            data['records'] = [fault(analysis_result='列表摘要', analysis_complete=False)]
        return ToolCallResult(tool_id=request.tool_id, status='SUCCESS', structured_content=data)
    nodes.tool_executor.execute = execute
    result = await run(nodes, make_runtime(), {'query': QUERY})
    assert result['final_status'] == 'COMPLETED'
    assert [r.tool_id for r in calls] == [SENSOR_TOOL_BY_OPERATION['active'], SENSOR_EVIDENCE_TOOL_ID]
    assert result['sensor_target']['fault_id'] == FID
    evidence = [o for o in synthesis_observations(result) if o.get('tool_id') == SENSOR_EVIDENCE_TOOL_ID]
    assert len(evidence[0]['tool_result']['structured_content']['record']['analysis_result']) > 6000
