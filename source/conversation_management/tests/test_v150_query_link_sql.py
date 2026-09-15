"""SQL/RLS integration; ONLY an explicit empty disposable test database.

Set both PHM_TEST_PG_DSN and PHM_TEST_DISPOSABLE_DB=1. Never use a production DSN.
This suite creates a minimal conversation schema in an otherwise empty database.
"""
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from app.services.asset_collections import load_collections, remember_collection

pytestmark=pytest.mark.skipif(not os.environ.get('PHM_TEST_PG_DSN') or os.environ.get('PHM_TEST_DISPOSABLE_DB')!='1',reason='explicit disposable PostgreSQL database required')


@pytest_asyncio.fixture
async def sql_env():
    import asyncpg
    async def init(conn):
        await conn.set_type_codec('jsonb',encoder=lambda v:v if isinstance(v,str) else json.dumps(v),decoder=json.loads,schema='pg_catalog')
    pool=await asyncpg.create_pool(os.environ['PHM_TEST_PG_DSN'],min_size=1,max_size=1,init=init)
    async with pool.acquire() as conn:
        existing=await conn.fetchval("SELECT to_regclass('public.conversations')")
        marker=await conn.fetchval("SELECT to_regclass('public.phm_test_fixture_marker')")
        if existing and not marker:
            pool.terminate()
            raise RuntimeError('Refusing to touch an existing conversation database')
        await conn.execute('''CREATE TABLE IF NOT EXISTS phm_test_fixture_marker(id int);
            DROP TABLE IF EXISTS asset_query_links,generation_tasks,messages,conversation_branches,conversations CASCADE;
            CREATE TABLE conversations(id uuid PRIMARY KEY,user_token text);
            CREATE TABLE conversation_branches(id uuid PRIMARY KEY,conversation_id uuid);
            CREATE TABLE messages(id uuid PRIMARY KEY,conversation_id uuid,branch_id uuid);
            CREATE TABLE generation_tasks(id uuid PRIMARY KEY,conversation_id uuid,branch_id uuid,assistant_message_id uuid);
            CREATE SCHEMA IF NOT EXISTS conversation_security;
            CREATE OR REPLACE FUNCTION conversation_security.current_user_token() RETURNS text LANGUAGE sql STABLE AS $$SELECT current_setting('app.user_token',true)$$;
            CREATE OR REPLACE FUNCTION conversation_security.bypass_rls() RETURNS bool LANGUAGE sql STABLE AS $$SELECT false$$;
            DO $$BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='phm_test_actor') THEN CREATE ROLE phm_test_actor; END IF; END$$;''')
        file=Path(__file__).parents[1]/'migrations/versions/0002_asset_query_links.py'
        spec=importlib.util.spec_from_file_location('fixture_migration',file);migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        statements=[];migration.op=SimpleNamespace(execute=statements.append);migration.upgrade()
        for statement in statements:await conn.execute(statement)
        await conn.execute('GRANT USAGE ON SCHEMA public,conversation_security TO phm_test_actor; GRANT SELECT,INSERT,DELETE ON ALL TABLES IN SCHEMA public TO phm_test_actor')
        cid,bid,other_branch,tid,mid,other_mid=[uuid4() for _ in range(6)]
        await conn.execute("INSERT INTO conversations VALUES($1,'A');",cid)
        await conn.execute('INSERT INTO conversation_branches VALUES($1,$3),($2,$3)',bid,other_branch,cid)
        await conn.execute('INSERT INTO messages VALUES($1,$3,$4),($2,$3,$5)',mid,other_mid,cid,bid,other_branch)
        await conn.execute('INSERT INTO generation_tasks VALUES($1,$2,$3,$4)',tid,cid,bid,mid)
    class Adapter:
        def __init__(self,user):self.user=user
        async def __aenter__(self):
            self.conn=await pool.acquire();self.tx=self.conn.transaction();await self.tx.start()
            await self.conn.execute('SET LOCAL ROLE phm_test_actor')
            await self.conn.execute("SELECT set_config('app.user_token',$1,true)",self.user)
            return self
        async def __aexit__(self,typ,*rest):
            await (self.tx.rollback() if typ else self.tx.commit())
            await pool.release(self.conn)
        @asynccontextmanager
        async def begin(self):yield self
        async def execute(self,statement,values):
            params=[];indexes={}
            def bind(match):
                key=match[1]
                if key not in indexes:indexes[key]=len(params)+1;params.append(values[key])
                return '$'+str(indexes[key])
            sql=re.sub(r'(?<!:):([A-Za-z_][A-Za-z0-9_]*)',bind,str(statement))
            records=await self.conn.fetch(sql,*params)
            return SimpleNamespace(mappings=lambda:SimpleNamespace(all=lambda:[dict(r) for r in records]))
    settings=SimpleNamespace(phm_asset_query_context_secret='x'*64,phm_asset_unified_query_enabled=True)
    factory=lambda user:lambda:Adapter(user)
    request=SimpleNamespace(task_id=str(tid),conversation_id=str(cid),branch_id=str(bid))
    runtime=SimpleNamespace(settings=settings,user_token='A',event_service=SimpleNamespace(session_factory=factory('A')))
    yield SimpleNamespace(pool=pool,settings=settings,factory=factory,request=request,runtime=runtime,
        cid=cid,bid=bid,other_branch=other_branch,mid=mid,other_mid=other_mid)
    pool.terminate()


@pytest.mark.asyncio
async def test_message_chain_ownership_and_real_rls(sql_env):
    e=sql_env
    result={'query_id':str(uuid4()),'scope_name':'总部钢铁','count':115,'unknown_count':0}
    await remember_collection(e.runtime,e.request,result)
    await remember_collection(e.runtime,e.request,result)
    async with e.pool.acquire() as conn:
        assert await conn.fetchval('SELECT count(*) FROM asset_query_links')==1
    branch=SimpleNamespace(conversation_id=e.cid,id=e.bid)
    path=[SimpleNamespace(id=e.mid,role='ASSISTANT',status='COMPLETED')]
    async with e.factory('A')() as session:
        own=await load_collections(session,branch,path,e.settings)
        assert own[0]['count']==115 and own[0]['source_message_id']==str(e.mid)
        assert await load_collections(session,branch,[SimpleNamespace(id=e.other_mid,role='ASSISTANT',status='COMPLETED')],e.settings)==[]
    async with e.factory('B')() as session:
        assert await load_collections(session,branch,path,e.settings)==[]
    # A fork that actually contains the ancestor can inherit it; unrelated newer
    # messages remain excluded by the actual parent path.
    async with e.factory('A')() as session:
        inherited=await load_collections(session,SimpleNamespace(conversation_id=e.cid,id=e.other_branch),path,e.settings)
        assert inherited[0]['query_id']==result['query_id']
