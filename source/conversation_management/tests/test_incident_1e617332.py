"""Replay the point-diagnosis incident through real classification/graph/adapters.

Only external LLM/MCP responses are controlled. Asset identity and raw signals
below are synthetic; the production task-understanding output is in the fixture.
"""
import base64
import asyncio
import copy
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.content.understanding_service import ContentUnderstandingService
from app.orchestration.entity_dependency import identity_dependency
from app.orchestration.entity_resolution_layer import UnifiedEntityResolutionLayer
from app.orchestration.entity_selection_resume import build_selected_entity_result
from app.orchestration.graphs.normal_graph import build_normal_graph
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.runtime import graph_runtime_scope
from app.orchestration.supervisor.agent import SupervisorAgent
from app.schemas.entity import EntityLookupResult
from app.tools.contracts import ToolCallResult
from app.tools.phm_asset_mcp import phm_asset_tool_descriptors
from app.tools.phm_data_mcp import PHM_GET_DATA_SNAPSHOT_TOOL_ID, phm_data_tool_descriptors
from app.tools.phm_diagnosis_mcp import PHM_DIAGNOSIS_POINT_TOOL_ID, phm_diagnosis_tool_descriptors
from app.tools.phm_feature_mcp import PHM_FEATURE_EXTRACT_RPM_TOOL_ID, phm_feature_tool_descriptors
from app.tools.registry import ToolRegistry
from app.workflows.registry import BusinessWorkflowRegistry
from test_v11_orchestration import make_runtime, assert_single_business_classification

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/incident_1e617332.json').read_text())


def point(device='TEST-ROLL-1', number=1):
    return {'entity_type': 'point', 'equip_no': device, 'equip_name': '17架轧机',
            'point_no': f'{device}{number:03d}A', 'point_name': f'减速机驱动端{number}',
            'space_id': 'TEST-AREA', 'space_name': '测试车间', 'space_link': '/TEST/AREA/',
            'similarity': .98, 'candidate_id': f'{device}-POINT-{number}'}


def equipment(device='TEST-ROLL-1'):
    return {k: v for k, v in {**point(device), 'entity_type': 'equipment',
            'candidate_id': f'equipment:{device}'}.items() if not k.startswith('point_')}


def asset_result(rows):
    status = 'UNIQUE' if len(rows) == 1 else 'MULTIPLE' if rows else 'NOT_FOUND'
    return EntityLookupResult(status=status, need_lookup=True, matches=copy.deepcopy(rows),
        match_count=len(rows), need_disambiguation=len(rows) > 1,
        resolved_entity=copy.deepcopy(rows[0]) if len(rows) == 1 else None,
        lookup_scope='point', top_similarity=.98,
        decision={'action': 'SEARCH', 'target_entity_level': 'point'},
        entity_constraints={'component_keyword': '减速机', 'point_keyword': '减速机测点'},
        query_scope={'target_entity_type': 'point'})


def make_nodes(rows=None):
    settings = Settings.model_construct(phm_asset_mcp_enabled=True, phm_asset_mcp_url='http://test/asset',
        phm_data_mcp_enabled=True, phm_data_mcp_url='http://test/data',
        phm_feature_mcp_enabled=True, phm_feature_mcp_url='http://test/feature',
        phm_diagnosis_mcp_enabled=True, phm_diagnosis_mcp_url='http://test/diagnosis')
    class Model:
        complete_json_profile = AsyncMock(return_value=copy.deepcopy(FIXTURE['classification']))
        plan_tool_calls = AsyncMock(side_effect=AssertionError('Registered point recipe must continue'))
        async def stream_profile(self, **kwargs):
            yield ('未找到可确认的测点，尚未执行诊断。' if rows == [] else
                   '该测点本次诊断未发现明确故障证据，结论以本次采集数据为依据。')
    model = Model()
    supervisor = SupervisorAgent(model_client=model, model_registry=SimpleNamespace(get=lambda _:None), settings=settings)
    registry = ToolRegistry()
    for factory in (phm_asset_tool_descriptors, phm_data_tool_descriptors,
                    phm_feature_tool_descriptors, phm_diagnosis_tool_descriptors):
        for descriptor in factory(settings):
            registry.register_tool(descriptor)
    asset = SimpleNamespace(lookup=AsyncMock(return_value=asset_result(rows if rows is not None else [point()])))
    calls = []
    binary = base64.b64encode(struct.pack('<8f', 0, 1, 0, -1, 0, 1, 0, -1)).decode()
    async def execute(*, request, **kwargs):
        calls.append(copy.deepcopy(request))
        if request.tool_id == PHM_GET_DATA_SNAPSHOT_TOOL_ID:
            data = {'success': True, 'data': {'waveform': {'data': {
                'point_no': request.arguments['wave_point_no'], 'sample_rate_hz': 1024,
                'values_base64': binary}}, 'feature_trend': {'series': []}}}
        elif request.tool_id == PHM_FEATURE_EXTRACT_RPM_TOOL_ID:
            assert binary in json.dumps(request.arguments)
            data = {'success': True, 'supported': True, 'rpm': 1480}
        else:
            assert request.tool_id == PHM_DIAGNOSIS_POINT_TOOL_ID
            assert request.arguments['waveform']['data']['values_base64'] == binary
            assert request.arguments['speed_rpm'] == 1480
            data = {'success': True, 'status': 'OK', 'point_no': request.arguments['point_no'],
                    'diagnosis': {'fault_detected': False, 'conclusion': '未发现明确故障证据'}}
        return ToolCallResult(tool_id=request.tool_id, status='SUCCESS', content='测试数据已返回', structured_content=data)
    nodes = ConversationGraphNodes(registry_service=SimpleNamespace(snapshot=AsyncMock(return_value={})),
        content_understanding_service=ContentUnderstandingService(session_factory=None,
            attachment_service=None, parser_registry=None, repository=None), supervisor=supervisor, agent_executor=None,
        tool_registry=registry, tool_executor=SimpleNamespace(execute=execute),
        workflow_registry=BusinessWorkflowRegistry(), entity_resolution_layer=UnifiedEntityResolutionLayer(asset))
    return nodes, asset, model, calls


def initial(runtime):
    return {'task_id': runtime.agent_runtime.task_id, 'conversation_id': 'TEST-C',
            'branch_id': 'TEST-B', 'query': FIXTURE['query'], 'observations': [], 'execution_mode': 'normal',
            'diagnosis_choice': {'source':'user_confirmation','mode':'detailed'}}


async def run(nodes, state, runtime):
    with graph_runtime_scope(runtime):
        return await build_normal_graph(nodes).ainvoke(state)


@pytest.mark.asyncio
async def test_incident_preserves_point_semantics_and_completes_real_recipe_handoff():
    nodes, asset, model, calls = make_nodes()
    rt = make_runtime()
    final = await run(nodes, initial(rt), rt)
    assert final['entity_dependency']['level'] == 'point'
    args = asset.lookup.call_args.kwargs
    assert args['required_entity_level'] == 'point'
    assert args['semantic_hints']['point']['raw_text'] == '减速机测点'
    assert args['semantic_hints']['component']['raw_text'] == '减速机'
    assert final['final_status'] == 'COMPLETED'
    assert [c.tool_id for c in calls] == [PHM_GET_DATA_SNAPSHOT_TOOL_ID, PHM_FEATURE_EXTRACT_RPM_TOOL_ID, PHM_DIAGNOSIS_POINT_TOOL_ID]
    assert calls[0].arguments['wave_point_no'] == calls[2].arguments['point_no'] == point()['point_no']
    assert '未发现明确故障证据' in final['final_answer']
    assert 'point_no' not in final['final_answer'] and '可继续询问' not in final['final_answer']
    assert_single_business_classification(model)
    model.plan_tool_calls.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_device_ambiguous_points_wait_then_resume_without_reclassification():
    nodes, asset, model, calls = make_nodes([point(number=1), point(number=2)])
    rt = make_runtime()
    waiting = await run(nodes, initial(rt), rt)
    assert waiting['final_status'] == 'WAITING_SELECTION' and not calls
    assert {p['point_no'] for p in waiting['entity_result']['matches']} == {point(number=1)['point_no'], point(number=2)['point_no']}
    selected = waiting['entity_result']['matches'][1]
    resume = {**initial(rt), 'business_intent': waiting['business_intent'],
              'business_workflow': waiting['business_workflow'], 'selection_resume': True,
              'selected_entity': selected, 'entity_result': build_selected_entity_result(waiting['entity_result'], selected)}
    final = await run(nodes, resume, rt)
    assert final['final_status'] == 'COMPLETED'
    assert calls[0].arguments['wave_point_no'] == calls[-1].arguments['point_no'] == selected['point_no']
    asset.lookup.assert_awaited_once()
    assert_single_business_classification(model)


@pytest.mark.asyncio
@pytest.mark.parametrize('point_count', [1, 2])
async def test_parent_selection_keeps_only_original_points_under_selected_equipment(point_count):
    rows = [point(number=n) for n in range(1, point_count+1)] + [point('TEST-ROLL-2')]
    nodes, asset, model, calls = make_nodes(rows)
    rt = make_runtime()
    waiting = await run(nodes, initial(rt), rt)
    assert waiting['final_status'] == 'WAITING_SELECTION'
    assert {p['entity_type'] for p in waiting['entity_result']['matches']} == {'equipment'}
    parent = next(p for p in waiting['entity_result']['matches'] if p['equip_no'] == 'TEST-ROLL-1')
    resume = {**initial(rt), 'business_intent': waiting['business_intent'],
              'business_workflow': waiting['business_workflow'], 'selection_resume': True,
              'selected_entity': parent, 'entity_result': build_selected_entity_result(waiting['entity_result'], parent)}
    final = await run(nodes, resume, rt)
    if point_count > 1:
        assert final['final_status'] == 'WAITING_SELECTION' and not calls
        choices = final['entity_result']['matches']
        assert {p['equip_no'] for p in choices} == {'TEST-ROLL-1'}
        assert {p['point_no'] for p in choices} == {point(number=1)['point_no'], point(number=2)['point_no']}
        selected = choices[1]
        resume.update(entity_result=build_selected_entity_result(final['entity_result'], selected), selected_entity=selected)
        final = await run(nodes, resume, rt)
    assert final['final_status'] == 'COMPLETED'
    assert calls[-1].arguments['point_no'] == point(number=point_count)['point_no']
    asset.lookup.assert_awaited_once()
    assert_single_business_classification(model)


@pytest.mark.asyncio
async def test_stored_parent_candidate_does_not_require_point_identity_yet():
    # Independently exposes the second bug even if root lookup is repaired alone.
    registry = BusinessWorkflowRegistry()
    workflow = registry.select_from_classification(FIXTURE['classification']).model_dump(mode='json')
    prior = {'status': 'MULTIPLE', 'resolution_source': 'parent_equipment_disambiguation',
             'point_selection_candidates': [point(), point(number=2), point('TEST-ROLL-2')]}
    nodes, asset, model, calls = make_nodes()
    rt = make_runtime()
    final = await run(nodes, {**initial(rt), 'business_workflow': workflow,
        'business_intent': FIXTURE['classification'], 'selection_resume': True,
        'selected_entity': equipment(), 'entity_result': build_selected_entity_result(prior, equipment())}, rt)
    assert final['final_status'] == 'WAITING_SELECTION' and not calls
    assert {p['equip_no'] for p in final['entity_result']['matches']} == {'TEST-ROLL-1'}
    asset.lookup.assert_not_awaited()
    model.complete_json_profile.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(('rows', 'status'), [([], 'COMPLETED'), ([equipment()], 'FAILED'),
    ([equipment(), equipment('TEST-ROLL-2')], 'FAILED')])
async def test_missing_or_wrong_level_asset_result_never_reaches_data(rows, status):
    nodes, asset, model, calls = make_nodes(rows)
    rt = make_runtime()
    final = await run(nodes, initial(rt), rt)
    # A real no-match retains the existing general-answer fallback; an invalid
    # service identity remains a hard failure. Neither may read another device.
    assert final['final_status'] == status and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('pool', [None, [], [point('TEST-ROLL-2')], [equipment()]])
async def test_missing_or_incompatible_stored_child_candidates_cannot_bypass_guard(pool):
    nodes, asset, model, calls = make_nodes()
    rt = make_runtime()
    workflow = BusinessWorkflowRegistry().select_from_classification(FIXTURE['classification']).model_dump(mode='json')
    prior = {'resolution_source': 'parent_equipment_disambiguation', 'point_selection_candidates': pool}
    final = await run(nodes, {**initial(rt), 'business_workflow': workflow,
        'business_intent': FIXTURE['classification'], 'selection_resume': True,
        'selected_entity': equipment(), 'entity_result': build_selected_entity_result(prior, equipment())}, rt)
    assert final['final_status'] == 'FAILED' and not calls
    assert final['entity_result']['error']['code'] == 'ENTITY_SELECTION_LEVEL_MISMATCH'
    asset.lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_turn_can_switch_point_target_and_does_not_reuse_previous_device():
    nodes, asset, model, calls = make_nodes([point('TEST-ROLL-2')])
    new_intent = copy.deepcopy(FIXTURE['classification'])
    new_intent['asset_semantics']['equipment'] = {'raw_text': '18架轧机', 'retrieval_text': '18架轧机'}
    model.complete_json_profile.return_value = new_intent
    rt = make_runtime()
    state = {**initial(rt), 'query': '诊断分析一下18架轧机的减速机测点',
             'active_entity': point(), 'selected_entity': {}, 'resolved_entity': {}}
    final = await run(nodes, state, rt)
    assert final['final_status'] == 'COMPLETED'
    args = asset.lookup.call_args.kwargs
    assert args['active_entity'] is None and args['allow_context_reuse'] is False
    assert args['required_entity_level'] == 'point'
    assert calls[0].arguments['device_code'] == 'TEST-ROLL-2'
    assert calls[-1].arguments['point_no'] == point('TEST-ROLL-2')['point_no']


@pytest.mark.asyncio
async def test_concurrent_diagnoses_keep_selected_point_and_raw_payload_per_task():
    async def one(device):
        nodes, _, _, calls = make_nodes([point(device)])
        rt = make_runtime(device)
        final = await run(nodes, initial(rt), rt)
        assert final['final_status'] == 'COMPLETED'
        assert calls[0].arguments['device_code'] == device
        assert calls[-1].arguments['point_no'] == point(device)['point_no']
        assert calls[-1].arguments['waveform']['data']['point_no'] == point(device)['point_no']
        return rt
    a, b = await asyncio.gather(one('TEST-ROLL-1'), one('TEST-ROLL-2'))
    assert a.transient_tool_payloads is not b.transient_tool_payloads


@pytest.mark.parametrize('anchor', ['space', 'equipment', 'point'])
def test_every_registered_recipe_uses_its_declared_input_level(anchor):
    registry = BusinessWorkflowRegistry()
    for definition in registry.list_definitions():
        for variant in definition.variants:
            if variant.required_entity_level == 'none':
                continue
            dep = identity_dependency({'business_intent': {'goal_frame': {'anchor_entity_level': anchor}},
                'business_workflow': {'required_entity_level': variant.required_entity_level}})
            assert dep['level'] == variant.required_entity_level, (definition.workflow_id, variant.variant_id)


@pytest.mark.parametrize(('anchor', 'target'), [('space', 'equipment'), ('equipment', 'point')])
def test_dynamic_collection_keeps_parent_scope(anchor, target):
    dep = identity_dependency({'business_intent': {'goal_frame': {'anchor_entity_level': anchor,
        'target_entity_level': target, 'evidence_types': ['asset']}, 'asset_semantics': {'needs_asset_lookup': True,
        'descendant_collection_requested': True}}})
    assert dep['level'] == ('area' if anchor == 'space' else anchor)
