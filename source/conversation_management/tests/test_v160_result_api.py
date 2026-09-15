from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.api.v1.query_results import router
from app.api.dependencies import get_container
from app.domain.exceptions import AppError
from app.models.message import Message
from app.services.query_results import make_result
from test_v140_diagnosis_confirmation import db


@pytest.mark.asyncio
async def test_saved_results_are_owned_paginated_and_export_safe(db):
    row=db.seed(owner='A')
    result=make_result(plan={"operation":"list"},rows=[{"name":"=HYPERLINK(1)","equip_no":"E1"},{"name":"设备二","equip_no":"E2"}],total=2)
    with Session(db.engine) as session:
        m=session.get(Message,row.mid);m.status='COMPLETED';m.metadata_json={"answer_results":[result]};session.commit()
    app=FastAPI();app.include_router(router,prefix='/chat/v1')
    app.dependency_overrides[get_container]=lambda:SimpleNamespace(conversation_service=SimpleNamespace(session_factory=db.Bridge))
    @app.middleware('http')
    async def request_id(request,next):request.state.request_id='test';return await next(request)
    @app.exception_handler(AppError)
    async def error(request,exc):return JSONResponse({'code':exc.code},status_code=exc.status_code)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        url=f'/chat/v1/messages/{row.mid}/query-results'
        good=await client.get(url,params={'limit':1},headers={'X-User-Token':'A'})
        assert good.status_code==200 and good.json()['data'][0]['has_more']
        assert len(good.json()['data'][0]['rows'])==1
        assert (await client.get(url,headers={'X-User-Token':'B'})).status_code==404
        assert (await client.get(url,params={'result_id':'missing'},headers={'X-User-Token':'A'})).json()['data']==[]
        exported=await client.get(url+'/'+result['result_id']+'/export',headers={'X-User-Token':'A'})
        assert exported.status_code==200 and exported.headers['X-Exported-Rows']=='2'
        assert "'=HYPERLINK" in exported.text and '设备二' in exported.text
        assert (await client.get(url+'/'+result['result_id']+'/export',headers={'X-User-Token':'B'})).status_code==404
