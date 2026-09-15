"""Deterministic full-input health ranking, with explicit missing and tie counts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import BoundedSemaphore
import time

_SLOTS=BoundedSemaphore(32)
import math
from app.health_output import format_health_output


def query_collection(service, scope_type, scope_ids, *, operation="rank", order="asc", limit=10, parallel=12, budget_seconds=80):
    if scope_type not in {"device", "space"} or operation not in {"rank", "list", "group", "count"} or order not in {"asc", "desc"}:
        raise ValueError("不支持的健康度集合查询操作")
    if not isinstance(scope_ids, list) or not 1 <= len(scope_ids) <= 50000:
        raise ValueError("健康度集合应包含1至50000个真实对象")
    ids = list(dict.fromkeys(str(x).strip() for x in scope_ids))
    if any(not x or len(x)>128 for x in ids) or not 1<=int(limit)<=50000:
        raise ValueError("对象编码或展示数量无效")
    deadline=time.monotonic()+max(0.01,float(budget_seconds))
    def read(code):
        acquired=False
        try:
            acquired=_SLOTS.acquire(timeout=max(0,deadline-time.monotonic()))
            if not acquired:raise TimeoutError("集合查询达到本轮预算")
            payload = format_health_output(service.query(scope_type, code))
            data = payload.get("data") or {}
            score = next((data[k] for k in ("score", "total_score", "finalScore") if data.get(k) is not None), None)
            grade = data.get("grade")
            valid = isinstance(score,(int,float)) and not isinstance(score,bool) and math.isfinite(score) and grade not in (5,"离线")
            return {"scope_id":code,"health_score":score if valid else None,"grade":grade,
                    "health_time":data.get("ts") or data.get("timestamp"),"available":valid,
                    "dimensions":{k:data[k] for k in ("thresholdScore","trendScore","aiScore","mechanismScore") if k in data}}
        except Exception as exc:
            return {"scope_id":code,"available":False,"error_type":type(exc).__name__}
        finally:
            if acquired:_SLOTS.release()
    # Submit only a bounded window; a timeout never leaves 50,000 queued source reads.
    pool=ThreadPoolExecutor(max_workers=max(1,min(int(parallel),32)))
    pending={}; collected={}; queue=iter(ids)
    def submit():
        code=next(queue,None)
        if code is not None:pending[pool.submit(read,code)]=code
    try:
        for _ in range(max(1,min(int(parallel),32))):submit()
        while pending and time.monotonic()<deadline:
            done,_=wait(pending,timeout=max(0,deadline-time.monotonic()),return_when=FIRST_COMPLETED)
            for future in done:
                code=pending.pop(future);collected[code]=future.result()
                if time.monotonic()<deadline:submit()
        for future in pending:future.cancel()
    finally:pool.shutdown(wait=False,cancel_futures=True)
    rows=[collected.get(code,{"scope_id":code,"available":False,"error_type":"QUERY_BUDGET_EXCEEDED"}) for code in ids]
    valid=[r for r in rows if r["available"]]
    missing=[r for r in rows if not r["available"]]
    ranked=sorted(valid,key=lambda r:((-r["health_score"] if order=="desc" else r["health_score"]),r["scope_id"]))
    chosen=ranked[:limit] if operation=="rank" else rows[:limit] if operation=="list" else []
    ties=sum(r["health_score"]==chosen[-1]["health_score"] for r in ranked) if operation=="rank" and chosen else 0
    shown_ties=sum(r["health_score"]==chosen[-1]["health_score"] for r in chosen) if operation=="rank" and chosen else 0
    groups={}
    for r in valid: groups[str(r.get("grade") or "未提供等级")]=groups.get(str(r.get("grade") or "未提供等级"),0)+1
    return {"success":True,"operation":operation,"scope_type":scope_type,"requested_count":len(ids),
            "valid_count":len(valid),"missing_count":len(missing),"failure_count":sum("error_type" in r for r in missing),
            "complete":not missing,"selection_complete":True,"items":chosen,"missing":missing,
            "groups":groups,"boundary_tie_count":ties,"omitted_ties":max(0,ties-shown_ties),
            "ranking_computed_in_code":True,"metric":"platform_health_score",
            "truncated":operation=="list" and len(rows)>limit}
