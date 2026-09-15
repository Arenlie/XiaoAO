"""Fixed, parameterized alarm aggregation for a verified equipment set."""
from collections import defaultdict
from datetime import datetime, timezone
from app.alarm.constants import TABLE_BY_TYPE


def period(start, end):
    if bool(start) != bool(end):
        raise ValueError("时间范围必须同时提供开始和结束时间")
    if not start: return None, None
    a, b = (datetime.fromisoformat(str(x).replace("Z", "+00:00")) for x in (start, end))
    if a >= b: raise ValueError("开始时间必须早于结束时间")
    if a.tzinfo or b.tzinfo:
        from zoneinfo import ZoneInfo
        a, b = (x.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None) for x in (a,b))
    return a.strftime("%Y-%m-%d %H:%M:%S"), b.strftime("%Y-%m-%d %H:%M:%S")


def query_collection(repo, equip_nos, *, operation="count", metric="count_records", alarm_types=None,
                     alarm_state="active", start_time=None, end_time=None,
                     comparison_start=None, comparison_end=None, order="desc", limit=50000):
    if operation not in {"count","list","rank","group","compare"}:
        raise ValueError("暂不支持该报警集合操作")
    if metric not in {"count_records","sum_occurrences","distinct_equipment"}:
        raise ValueError("请明确按报警记录数、累计次数或报警设备数统计")
    if alarm_state not in {"all","active","ended"} or order not in {"asc","desc"}:
        raise ValueError("报警状态或排序条件不正确")
    if not isinstance(equip_nos,list) or not 1 <= len(equip_nos) <= 50000:
        raise ValueError("设备集合应包含1至50000个真实编码")
    codes=list(dict.fromkeys(str(x).strip() for x in equip_nos))
    if any(not x or len(x)>128 for x in codes) or not 1<=int(limit)<=50000:
        raise ValueError("设备编码或展示数量无效")
    types=list(dict.fromkeys(alarm_types or TABLE_BY_TYPE))
    if any(x not in TABLE_BY_TYPE for x in types): raise ValueError("报警类型无效")
    current=period(start_time,end_time); previous=period(comparison_start,comparison_end)
    if operation=="compare" and (not all(current) or not all(previous)):
        raise ValueError("比较统计需要两个完整时间范围")
    if operation=="compare" and alarm_state!="all":
        raise ValueError("历史期间比较应使用全部报警；当前结束状态不能还原历史时点状态")
    def aggregate(window):
        values=defaultdict(int)
        for offset in range(0,len(codes),500):
            batch=codes[offset:offset+500]
            clauses=["equip_no IN ("+",".join(["%s"]*len(batch))+")"]
            params=list(batch)
            if alarm_state!="all": clauses.append("latest_end_time IS "+("NOT " if alarm_state=="ended" else "")+"NULL")
            if window[0]:
                clauses.extend(["latest_start_time >= %s","latest_start_time < %s"]);params.extend(window)
            expression="COALESCE(SUM(total_num),0)" if metric=="sum_occurrences" else "COUNT(*)"
            for kind in types:
                sql=f"SELECT equip_no, {expression} AS metric_value FROM {TABLE_BY_TYPE[kind]} WHERE "+" AND ".join(clauses)+" GROUP BY equip_no"
                for row in repo.query(sql,params):
                    code=str(row["equip_no"])
                    if code not in batch: raise ValueError("报警来源返回了查询范围之外的设备")
                    values[code]+=int(row.get("metric_value") or 0)
        # Deduplicate across alarm types after merging, not by adding per-table distinct counts.
        return {c: int(values[c]>0) if metric=="distinct_equipment" else values[c] for c in codes}
    now=aggregate(current); before=aggregate(previous) if operation=="compare" else {}
    items=[{"equip_no":c,"alarm_count":now[c],**({"previous_alarm_count":before[c],"difference":now[c]-before[c],
        "change_percent":round((now[c]-before[c])*100/before[c],2) if before[c] else None} if before else {})} for c in codes]
    if operation=="rank": items.sort(key=lambda r:((-r["alarm_count"] if order=="desc" else r["alarm_count"]),r["equip_no"]))
    total=sum(now.values())
    return {"success":True,"items":items[:limit],"total":total,"requested_count":len(codes),
        "complete":True,"truncated":len(items)>limit,"metric":metric,"alarm_state":alarm_state,
        "queried_at":datetime.now(timezone.utc).isoformat(),
        "notes":["按报警汇总表统计；记录数、累计次数和发生报警的设备数采用不同口径。",
                 "时间范围按最近发生时间筛选，含开始、不含结束；这是汇总记录统计，不能还原每次历史报警事件。",
                 "各查询批次在本轮读取，持续变化的数据可能跨越多个采集时刻。"]}
