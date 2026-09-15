"""Additional structural review for the legacy alarm SELECT interface."""
from app.services.readonly_snapshot import sqlglot, exp
from app.alarm.constants import ALLOWED_TABLES
from app.alarm.fast_sql import DETAIL_FIELDS, _filters

SAFE_FUNCTIONS={"AND","OR","COUNT","SUM","AVG","MIN","MAX","ROUND","ABS","COALESCE","NULLIF","CAST","TIMESTAMPDIFF",
 "TIMESTAMP_DIFF","DATEDIFF","DATE_DIFF","DATE","DATE_FORMAT","TIME_TO_STR","STDDEV","STDDEV_POP","VARIANCE","VAR_POP"}


def tree(sql):
    trees=sqlglot.parse(sql,read="mysql")
    if len(trees)!=1 or not isinstance(trees[0],(exp.Select,exp.Union)):raise ValueError("只允许单条SELECT查询")
    node=trees[0]
    if len(sql)>16000 or sum(1 for _ in node.walk())>800: raise ValueError("统计查询复杂度超限")
    for n in node.walk():
        if n.comments: raise ValueError("查询中不接受注释")
    for name in ("Join","With","Into","Lock","Command","Parameter","Placeholder","Window"):
        kind=getattr(exp,name,None)
        if kind and node.find(kind):raise ValueError("查询包含未开放结构")
    for f in node.find_all(exp.Func):
        key=f.name.upper() if isinstance(f,exp.Anonymous) else f.sql_name().upper()
        if key not in SAFE_FUNCTIONS:raise ValueError("查询包含未开放函数")
    tables=list(node.find_all(exp.Table))
    if not tables or any(t.name not in ALLOWED_TABLES or t.db or t.catalog for t in tables):raise ValueError("查询引用未允许的数据表")
    return node


def validate(sql,expected):
    node=tree(sql)
    if {t.name for t in node.find_all(exp.Table)}!=expected:raise ValueError("查询的数据表集合与本轮条件不一致")
    allowed=set(DETAIL_FIELDS)|{"point_no","alarm_type","alarm_type_name"}|{a.alias for a in node.find_all(exp.Alias)}
    if any(c.name not in allowed for c in node.find_all(exp.Column)):raise ValueError("查询使用了未经确认的字段")


def validate_filters(sql,spec):
    node=tree(sql)
    clauses,params=_filters(spec)
    # Build the required AST using literal values solely for comparison; never execute this text.
    position=iter(params)
    expected=[]
    for clause in clauses:
        while "%s" in clause:
            value=next(position)
            literal=exp.Literal.number(str(value)) if isinstance(value,(int,float)) else exp.Literal.string(str(value))
            clause=clause.replace("%s",literal.sql(dialect="mysql"),1)
        expected.append(sqlglot.parse_one(clause,read="mysql"))
    def canonical(value):
        value=value.copy()
        for c in value.find_all(exp.Column):c.set("table",None)
        return value.sql(dialect="mysql",normalize=True)
    def conjuncts(value):
        if isinstance(value,exp.Paren):return conjuncts(value.this)
        if isinstance(value,exp.And):return conjuncts(value.this)+conjuncts(value.expression)
        return [value]
    for table in node.find_all(exp.Table):
        select=table.find_ancestor(exp.Select)
        where=select.args.get("where")
        conditions={canonical(x) for x in conjuncts(where.this)} if where else set()
        if any(canonical(x) not in conditions for x in expected):raise ValueError("某个报警表分支遗漏了已确认的范围、状态或时间条件")
