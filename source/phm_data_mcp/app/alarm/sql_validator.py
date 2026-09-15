from __future__ import annotations

import re

from app.alarm.constants import ALLOWED_TABLES


class SqlValidationError(ValueError):
    pass


def validate_read_only_sql(sql: str, expected_tables: set[str], max_limit: int) -> str:
    """Validate LLM-generated SQL before MySQL execution."""
    cleaned = re.sub(r"^```(?:sql)?\s*|\s*```$", "", (sql or "").strip(), flags=re.I).rstrip(";").strip()
    low = re.sub(r"\s+", " ", cleaned.lower().replace("`", ""))
    if not re.match(r"^select\b", low):
        raise SqlValidationError("只允许SELECT")
    if ";" in cleaned:
        raise SqlValidationError("禁止多语句SQL")
    forbidden = [
        r"\b(insert|update|delete|drop|alter|truncate|create|replace|call|grant|revoke|merge)\b",
        r"\b(information_schema|performance_schema|mysql\.|sys\.)\b",
        r"\b(into\s+outfile|into\s+dumpfile|load_file|sleep|benchmark)\b",
        r"\b(for\s+update|lock\s+in\s+share\s+mode)\b",
        r"(--|/\*|\*/|#)",
        r"\bjoin\b",
    ]
    if any(re.search(pattern, low, flags=re.I) for pattern in forbidden):
        raise SqlValidationError("SQL包含禁止结构")
    without_union_all = re.sub(r"\bunion\s+all\b", "", low, flags=re.I)
    if re.search(r"\bunion\b", without_union_all):
        raise SqlValidationError("跨表只允许UNION ALL")
    actual = {table for table in ALLOWED_TABLES if re.search(r"\b" + re.escape(table) + r"\b", low)}
    if not actual or not actual.issubset(ALLOWED_TABLES):
        raise SqlValidationError("SQL访问了未允许的表")
    if expected_tables and actual != expected_tables:
        raise SqlValidationError("SQL使用的报警表集合与请求不一致")
    other_refs = re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)", low)
    for ref in other_refs:
        if ref.split(".")[-1] not in ALLOWED_TABLES:
            raise SqlValidationError("SQL引用了未允许的表")
    limit = re.search(r"\blimit\s+(\d+)\s*$", cleaned, flags=re.I)
    if limit:
        count = max(1, min(max_limit, int(limit.group(1))))
        cleaned = cleaned[: limit.start()] + f"LIMIT {count}"
    else:
        cleaned += f" LIMIT {max_limit}"
    from app.alarm.ast_review import validate
    try: validate(cleaned,expected_tables)
    except ValueError as exc: raise SqlValidationError(str(exc)) from exc
    return cleaned


def validate_required_filters(sql: str, spec) -> None:
    """Prevent an LLM fallback from silently dropping structured filters."""
    from app.alarm.ast_review import validate_filters
    try: validate_filters(sql,spec)
    except ValueError as exc: raise SqlValidationError(str(exc)) from exc
    low = re.sub(r"\s+", " ", sql.lower().replace("`", ""))

    def require_exact(column: str, value: str | None) -> None:
        if value and not re.search(r"\b" + re.escape(column) + r"\b\s*=\s*'" + re.escape(value.lower().replace("'", "''")) + r"'", low):
            raise SqlValidationError(f"SQL遗漏硬约束：{column}")

    require_exact("equip_no", spec.equip_no)
    require_exact("equip_name", spec.equip_name)
    if spec.equip_name_keyword:
        expected = "%" + spec.equip_name_keyword.lower().replace("'", "''") + "%"
        if not re.search(r"\bequip_name\b\s+like\s+'" + re.escape(expected) + r"'", low):
            raise SqlValidationError("SQL遗漏硬约束：equip_name_keyword")
    require_exact("point_no", spec.point_no)
    require_exact("model_no", spec.model_no)
    if spec.space_link:
        expected = spec.space_link.lower().replace("'", "''") + "%"
        if not re.search(r"\bspace_link\b\s+like\s+'" + re.escape(expected) + r"'", low):
            raise SqlValidationError("SQL遗漏硬约束：space_link")
    if spec.space_name:
        expected = "%" + spec.space_name.lower().replace("'", "''") + "%"
        if not re.search(r"\bspace_name\b\s+like\s+'" + re.escape(expected) + r"'", low):
            raise SqlValidationError("SQL遗漏硬约束：space_name")
    if spec.alarm_state == "active" and not re.search(r"\blatest_end_time\b\s+is\s+null", low):
        raise SqlValidationError("SQL遗漏当前报警约束")
    if spec.alarm_state == "ended" and not re.search(r"\blatest_end_time\b\s+is\s+not\s+null", low):
        raise SqlValidationError("SQL遗漏历史报警约束")
    for column, dt, op in (("latest_start_time", spec.start_time, ">="), ("latest_start_time", spec.end_time, "<=")):
        if dt:
            value = dt.strftime("%Y-%m-%d %H:%M:%S").lower()
            if value not in low or column not in low:
                raise SqlValidationError("SQL遗漏时间范围约束")
