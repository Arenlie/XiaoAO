"""Reviewed SELECT over scoped data. Model SQL never reaches a production connection."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"vendor"))
import sqlglot
from sqlglot import exp

FIELDS={"name","equip_no","point_no","area","equipment_type","model","health_score","grade",
        "health_time","alarm_count","previous_alarm_count","difference","change_percent","count"}
FUNCTIONS={"COUNT","SUM","AVG","MIN","MAX","MEDIAN","P95","STDDEV","ROUND","ABS","COALESCE","NULLIF"}


def review(sql, columns, limit=1000):
    if not isinstance(sql,str) or len(sql)>8000: raise ValueError("统计表达式过长")
    trees=sqlglot.parse(sql,read="sqlite")
    if len(trees)!=1 or not isinstance(trees[0],exp.Select): raise ValueError("只允许单条只读统计查询")
    tree=trees[0]
    if any(n.comments for n in tree.walk()): raise ValueError("统计查询不接受注释")
    if sum(1 for _ in tree.walk())>400: raise ValueError("统计查询复杂度超限")
    for name in ("Join","With","Subquery","Union","Intersect","Except","Into","Lock","Window","Placeholder","Parameter","Command"):
        kind=getattr(exp,name,None)
        if kind and tree.find(kind): raise ValueError("统计查询包含未开放结构")
    if tree.args.get("where") or tree.args.get("having"): raise ValueError("数据已按本轮条件筛选，高级统计不得另行改变范围")
    tables=list(tree.find_all(exp.Table))
    if len(tables)!=1 or tables[0].name!="input_rows" or tables[0].db or tables[0].catalog:
        raise ValueError("统计查询只能访问本轮已确认范围的数据快照")
    aliases={a.alias for a in tree.find_all(exp.Alias)}
    for column in tree.find_all(exp.Column):
        if column.name not in columns|aliases: raise ValueError("统计查询包含未提供字段")
        if column.table and column.table not in {"input_rows",tables[0].alias}: raise ValueError("统计查询引用范围不正确")
    for func in tree.find_all(exp.Func):
        name=func.name.upper() if isinstance(func,exp.Anonymous) else func.sql_name().upper()
        if name not in FUNCTIONS: raise ValueError("统计查询包含未开放函数")
    if tree.args.get("offset"): raise ValueError("统计查询不接受隐藏的分页偏移")
    supplied=tree.args.get("limit")
    if supplied and (not isinstance(supplied.expression,exp.Literal) or not supplied.expression.is_int):
        raise ValueError("统计展示上限必须为整数")
    size=min(limit,max(1,int(supplied.expression.this))) if supplied else limit
    tree=tree.transform(lambda n: exp.Anonymous(this="MEDIAN",expressions=[n.this.copy()]) if isinstance(n,exp.Median) else n)
    return tree.limit(size+1).sql(dialect="sqlite"), size


def query(rows, sql, *, complete, limit=1000):
    if complete is not True: raise ValueError("数据范围不完整，不能启动高级统计")
    if not isinstance(rows,list) or not 1<=len(rows)<=50000 or not 1<=limit<=5000:
        raise ValueError("高级统计数据范围或输出数量超限")
    columns=set().union(*(r.keys() for r in rows)) & FIELDS
    projected=[{k:v for k,v in row.items() if k in columns and (v is None or isinstance(v,(str,int,float,bool)))} for row in rows]
    reviewed,size=review(sql,columns,limit)
    payload=json.dumps({"rows":projected,"columns":sorted(columns),"sql":reviewed},ensure_ascii=False,allow_nan=False)
    if len(payload.encode())>24*1024*1024: raise ValueError("高级统计数据量超出隔离执行上限")
    process=subprocess.run([sys.executable,"-I",str(Path(__file__).with_name("snapshot_worker.py"))],
        input=payload,text=True,capture_output=True,timeout=8,env={"LANG":"C.UTF-8"})
    if process.returncode: raise ValueError("高级统计未通过执行校验或超出计算预算")
    result=json.loads(process.stdout)
    return {"success":True,"items":result["rows"][:size],"complete":len(result["rows"])<=size,
        "truncated":len(result["rows"])>size,"input_count":len(rows),"query_hash":hashlib.sha256(reviewed.encode()).hexdigest(),
        "review":{"single_select":True,"scoped_snapshot":True,"syntax_checked":True,"access_checked":True,
                  "cost_bounded":True,"explain_checked":True,"production_sql_executed":False}}
