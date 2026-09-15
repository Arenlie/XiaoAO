from __future__ import annotations

import json
import re
from typing import Any, Dict

import requests


class OpenAICompatibleConflictJudge:
    """Optional LLM arbiter isolated from the deterministic RPM algorithm."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_s: float):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key or ""
        self.model = model
        self.timeout_s = timeout_s
        self.session = requests.Session()

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        try:
            return json.loads(text)
        except Exception:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                raise ValueError("LLM response does not contain a JSON object")
            return json.loads(match.group(0))

    def __call__(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        system_prompt = (
            "你是工业转频冲突仲裁器。只能在 velocity 和 acceleration 两个结果中二选一，禁止输出第三个频率。"
            "优先级：1X直接峰 > 由2X/3X/4X反推的频率。只输出JSON："
            '{"winner_source":"velocity或acceleration","winner_freq_hz":数值,'
            '"confidence":0到1,"reason":"简短原因","evidence":["证据1","证据2"]}'
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        response = self.session.post(self.url, headers=headers, json=body, timeout=self.timeout_s)
        response.raise_for_status()
        data = response.json()
        return self._extract_json(data["choices"][0]["message"]["content"])

    def close(self) -> None:
        self.session.close()
