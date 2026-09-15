#!/usr/bin/env python3
"""Run on the deployment host; reports availability/provenance, never credentials."""
import argparse
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from app.config import get_settings
from app.tools.contracts import ToolCallRequest
from app.tools.dify_knowledge import DifyKnowledgeHandler, KNOWLEDGE_NAMES, KNOWLEDGE_TOOL_ID


async def main(query):
    settings=get_settings()
    if not settings.dify_knowledge_api_key or not settings.dify_knowledge_enabled:
        print('知识库未启用或服务端密钥为空。');return 1
    async with httpx.AsyncClient(trust_env=False) as client:
        handler=DifyKnowledgeHandler(client,settings)
        try:
            async with asyncio.timeout(settings.dify_knowledge_timeout_seconds):
                catalog=await handler._datasets()
        except Exception:
            print('知识库目录访问未成功，请检查主机网络、服务地址及服务端凭据。');return 1
        for name in KNOWLEDGE_NAMES:
            print(name+'：'+('已唯一定位' if name in catalog else '缺失、无权限或名称重复'))
        if set(KNOWLEDGE_NAMES)-set(catalog):
            return 1
        if query:
            result=await handler(ToolCallRequest(tool_id=KNOWLEDGE_TOOL_ID,arguments={'query':query},
                task_id='acceptance',conversation_id='acceptance',branch_id='acceptance',user_token='local-acceptance'),None)
            print('检索状态：'+result.status.value)
            for source in result.structured_content.get('sources') or []:
                print(f"{source['knowledge_base']} · 《{source['document_name']}》 · 第 {source.get('position') or '未知'} 段")
            if result.status!='SUCCESS' or result.structured_content.get('warnings'):
                print('有知识库未完成检索，请查看应用运行日志。');return 1
            if not result.structured_content.get('sources'):
                print('本次未命中文档；请使用库中已知存在的问题复测。')
    return 0

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query',default='')
    args=parser.parse_args()
    sys.exit(asyncio.run(main(args.query)))
