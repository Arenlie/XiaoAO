from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.schemas.resolve import ResolveEntityRequest
from app.services.query_understanding import QueryUnderstandingService
from app.errors import AssetError,ErrorCode
from test_v11_asset import settings,resolver


@pytest.mark.asyncio
async def test_explicit_no_lookup_does_not_probe_model_version_as_equipment(settings):
    service=resolver(settings)
    service.understanding=QueryUnderstandingService(SimpleNamespace(extract_asset_parameters=AsyncMock(side_effect=AssertionError("LLM must not run"))))
    service.catalog.lookup_code_tokens=AsyncMock(side_effect=AssertionError("code query must not run"))
    response=await service.resolve(ResolveEntityRequest(query="解释Qwen3模型",semantic_hints={"needs_asset_lookup":False}))
    assert response.status=="NO_LOOKUP" and not response.entity
    service.catalog.lookup_code_tokens.assert_not_called()
    service.embedding.embed.assert_not_called()


def test_old_unrelated_user_phrase_cannot_become_current_identity():
    service=QueryUnderstandingService(None)
    with pytest.raises(AssetError) as e:
        service._from_semantic_hints(query="现在查新的车间",required_entity_level="equipment",user_profile=None,
            semantic_hints={"needs_asset_lookup":True,"equipment":{"raw_text":"旧水泵","retrieval_text":"旧水泵"}},
            conversation_context={"recent_messages":[{"role":"user","content":"查旧水泵"}]})
    assert e.value.code==ErrorCode.INVALID_ARGUMENT


def test_valid_parent_reference_may_use_previous_phrase_and_attachment_literal():
    service=QueryUnderstandingService(None)
    got=service._from_semantic_hints(query="这个设备的驱动端",required_entity_level="point",user_profile=None,
        semantic_hints={"needs_asset_lookup":True,"reference_target_level":"equipment","equipment":{"raw_text":"旧水泵","retrieval_text":"旧水泵"},
                        "position":{"raw_text":"驱动端","retrieval_text":"驱动端"}},
        conversation_context={"recent_messages":[{"role":"user","content":"查旧水泵"}]})
    assert got.context_reference and got.position_keyword=="驱动端"


def test_skip_ignores_malformed_irrelevant_terms():
    service=QueryUnderstandingService(None)
    got=service._from_semantic_hints(query="换成表格",required_entity_level="any",user_profile=None,
        semantic_hints={"needs_asset_lookup":False,"equipment":"bad shape"})
    assert not got.needs_asset_lookup
