"""Isolated SQLite worker; no service imports or database credentials."""
import json
import math
import sqlite3
import statistics
import sys
import time


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU,(5,5))
        resource.setrlimit(resource.RLIMIT_FSIZE,(0,0))
    except ImportError: pass
    payload=json.load(sys.stdin)
    db=sqlite3.connect(":memory:");db.enable_load_extension(False)
    columns=payload["columns"]
    db.execute("CREATE TABLE input_rows ("+", ".join('"'+x+'"' for x in columns)+")")
    db.executemany("INSERT INTO input_rows VALUES ("+",".join("?" for _ in columns)+")",[tuple(row.get(k) for k in columns) for row in payload["rows"]])
    class Median:
        def __init__(self): self.values=[]
        def step(self,x):
            if x is not None:
                if not isinstance(x,(float,int)) or not math.isfinite(x): raise ValueError("统计字段包含非数值")
                self.values.append(x)
        def finalize(self): return statistics.median(self.values) if self.values else None
    class P95(Median):
        def finalize(self):
            v=sorted(self.values)
            if not v:return None
            rank=(len(v)-1)*0.95;a=math.floor(rank);b=math.ceil(rank)
            return v[a]+(v[b]-v[a])*(rank-a)
    class Stddev(Median):
        def finalize(self):return statistics.pstdev(self.values) if self.values else None
    for name,cls in (("median",Median),("p95",P95),("stddev",Stddev)): db.create_aggregate(name,1,cls)
    db.execute("PRAGMA query_only=ON")
    functions={"count","sum","avg","min","max","median","p95","stddev","round","abs","coalesce","nullif"}
    def authorize(action,a,b,c,d):
        if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ and a=="input_rows":return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_FUNCTION and str(b or a).lower() in functions:return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    db.set_authorizer(authorize)
    deadline=time.monotonic()+3
    db.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
    db.execute("EXPLAIN QUERY PLAN "+payload["sql"]).fetchall()
    cursor=db.execute(payload["sql"])
    keys=[x[0] for x in cursor.description]
    if len(keys)!=len(set(keys)):raise ValueError("重复输出列名")
    print(json.dumps({"rows":[dict(zip(keys,row)) for row in cursor.fetchall()]},ensure_ascii=False,allow_nan=False))

if __name__=="__main__":main()
