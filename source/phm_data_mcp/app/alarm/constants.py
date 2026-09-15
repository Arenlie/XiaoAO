TABLE_BY_TYPE = {
    "threshold": "t_threshold_warning_record_summary",
    "trend": "t_trend_warning_record_summary",
    "diagnosis": "t_diagnosis_warning_record_summary",
    "ai": "t_ai_warning_record_summary",
}

TYPE_NAME = {
    "threshold": "阈值报警",
    "trend": "趋势报警",
    "diagnosis": "机理/诊断报警",
    "ai": "AI报警",
}

ALLOWED_TABLES = set(TABLE_BY_TYPE.values())
