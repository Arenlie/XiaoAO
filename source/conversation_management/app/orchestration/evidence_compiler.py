"""Bound each evidence source independently; never slice a serialized JSON stream."""
import json

DROP = {"all_rows", "_audit", "operator_error", "state_updates", "raw_payload", "data_base64", "waveform_base64"}


def project(value, *, rows=12, text=1800, depth=0):
    if depth>9: return {"omitted":True,"reason":"嵌套明细未展开"}
    if isinstance(value,dict):
        return {str(k):project(v,rows=rows,text=text,depth=depth+1) for k,v in value.items() if k not in DROP}
    if isinstance(value,list):
        items=[project(x,rows=rows,text=text,depth=depth+1) for x in value[:rows]]
        return items if len(value)<=rows else {"items":items,"total_items":len(value),"omitted_items":len(value)-rows,"complete":False}
    if isinstance(value,str) and len(value)>text:
        return value[:text]+f"〔另有 {len(value)-text} 字未展开，不能视为全文〕"
    return value


def compile_evidence(observations, budget=48000):
    sources=[]
    for item in observations:
        if not isinstance(item,dict): continue
        result=item.get("tool_result") or {}
        payload=result.get("structured_content") if isinstance(result,dict) else None
        if not payload:
            evidence=item.get("evidence") or []
            payload=[x.get("content") for x in evidence if isinstance(x,dict)] or item.get("answer_markdown") or ""
        sources.append({"tool_id":item.get("tool_id") or item.get("agent_id"),
            "status":item.get("status"),"error_code":item.get("error_code"),
            "error_message":item.get("error_message"),"can_support_final_answer":item.get("can_support_final_answer"),
            "evidence":payload})
    per=max(800,budget//max(1,len(sources)))
    compiled=[]
    for source in sources:
        candidate=project(source)
        for n,t in ((8,1000),(4,600),(2,300),(1,150)):
            if len(json.dumps(candidate,ensure_ascii=False,default=str))<=per: break
            candidate=project(source,rows=n,text=t)
        if len(json.dumps(candidate,ensure_ascii=False,default=str))>per:
            # Keep each source's status even for unusually wide dictionaries.
            candidate={k:v for k,v in candidate.items() if k!="evidence"}
            candidate["evidence"]={"omitted":True,"reason":"明细超过单项预算，请使用已输出的确定性事实表；不能判定无数据"}
        compiled.append(candidate)
    return json.dumps(compiled,ensure_ascii=False,default=str)


def compile_attachments(results):
    return json.dumps([project({k:x.get(k) for k in ("filename","kind","extraction_status","summary",
        "extracted_text","attachment_report","warnings","tables","sheets") if x.get(k)},rows=6,text=3500)
        for x in results],ensure_ascii=False,default=str)
