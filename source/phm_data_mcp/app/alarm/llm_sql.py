from __future__ import annotations

import json

import httpx

from app.alarm.constants import TABLE_BY_TYPE
from app.config import Settings
from app.models import AlarmQuerySpec


SYSTEM_PROMPT = """你是MySQL 8工业报警数据库的只读SQL生成器。只输出一条SELECT SQL，不要Markdown、注释或解释。

允许的报警表：
- t_threshold_warning_record_summary：阈值报警，有point_no。
- t_trend_warning_record_summary：趋势报警，有point_no。
- t_diagnosis_warning_record_summary：机理/诊断报警，无point_no。
- t_ai_warning_record_summary：AI报警，无point_no。

稳定字段：id, equip_no, space_link, space_name, equip_id, equip_name, model_no, model_name,
warn_level, warn_level_ch, total_num, deal_status_ch, confirm_status_ch, start_time,
latest_start_time, latest_end_time, duration, restrain_flag, restrain_reason。
不得引用company_id、warm_description、source_type、tag、eval_level_ch等不稳定扩展字段。

规则：
1. 当前报警：latest_end_time IS NULL；已结束报警：latest_end_time IS NOT NULL。
2. 时间范围默认过滤latest_start_time。
3. 报警记录数用COUNT(*)；累计报警次数用SUM(total_num)；设备数用COUNT(DISTINCT equip_no)。
4. 区域及下级设备用space_link LIKE 'prefix%'。
5. 设备编码、测点编码、模型编码必须精确匹配；名称包含匹配仅使用显式equip_name_keyword；设备类型必须先由资产类别查询取得真实设备范围，禁止把类型改为名称包含。
6. 多报警类型只能UNION ALL，禁止JOIN、WITH和普通UNION。
7. 只能访问允许的四张表，只能生成单条SELECT。
8. 外层必须LIMIT，且不能超过请求的limit。
"""


class AlarmSqlPlanner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, spec: AlarmQuerySpec) -> str:
        if not self.settings.alarm_llm_enabled:
            raise RuntimeError("当前查询超出固定SQL能力，且ALARM_LLM_ENABLED=false")
        if not self.settings.alarm_llm_base_url or not self.settings.alarm_llm_model:
            raise RuntimeError("报警SQL模型未配置")

        constraints = {
            "alarm_tables": [TABLE_BY_TYPE[x] for x in spec.alarm_types],
            "equip_no": spec.equip_no,
            "equip_name": spec.equip_name,
            "equip_name_keyword": spec.equip_name_keyword,
            "point_no": spec.point_no,
            "model_no": spec.model_no,
            "space_link": spec.space_link,
            "space_name": spec.space_name,
            "alarm_state": spec.alarm_state,
            "start_time": spec.start_time.isoformat(sep=" ", timespec="seconds") if spec.start_time else None,
            "end_time": spec.end_time.isoformat(sep=" ", timespec="seconds") if spec.end_time else None,
            "limit": spec.limit,
        }
        user_prompt = f"用户问题：{spec.query}\n结构化硬约束：{json.dumps(constraints, ensure_ascii=False)}"
        headers = {"Content-Type": "application/json"}
        if self.settings.alarm_llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.alarm_llm_api_key}"
        url = self.settings.alarm_llm_base_url.rstrip("/") + "/chat/completions"
        response = httpx.post(
            url,
            headers=headers,
            json={
                "model": self.settings.alarm_llm_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "max_tokens": 2200,
            },
            timeout=self.settings.alarm_llm_timeout_seconds,
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"]).strip()
