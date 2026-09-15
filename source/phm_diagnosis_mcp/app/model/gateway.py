from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import Settings


class ModelGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.llm_enabled and self.settings.llm_base_url and self.settings.llm_model)

    async def healthy(self) -> bool:
        if not self.configured():
            return False
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"} if self.settings.llm_api_key else {}
        try:
            async with httpx.AsyncClient(timeout=min(self.settings.llm_timeout_seconds, 10.0)) as client:
                response = await client.get(self.settings.llm_base_url.rstrip("/") + "/models", headers=headers)
                return 200 <= response.status_code < 300
        except Exception:
            return False

    async def diagnose(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        body = {
            "model": self.settings.llm_model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是工业设备故障诊断工程师。只能基于输入的确定性算法证据进行综合解释。"
                        "不要重新计算或改写RMS、频率、趋势数值，也不要新增输入中不存在的故障类型。"
                        "diagnosis_status=attention表示存在异常但尚不能确认具体故障，不得写成已确认故障。"
                        "diagnosis_status=fault时只能解释fault_hypotheses中已有的故障假设。"
                        "输出JSON，只包含summary、recommendations、limitations。"
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
        }

        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            response = await client.post(self.settings.llm_base_url.rstrip("/") + "/chat/completions", headers=headers, json=body)
            response.raise_for_status()
            raw = response.json()

        content = raw["choices"][0]["message"]["content"]
        result = self._parse_json(content)
        usage = raw.get("usage") or {}
        meta = {
            "model": self.settings.llm_model,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        }
        return result, meta

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        text = str(text or "").strip()
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("模型没有返回有效JSON")
        value = json.loads(match.group(0))
        if not isinstance(value, dict):
            raise ValueError("模型JSON必须是对象")
        return value
