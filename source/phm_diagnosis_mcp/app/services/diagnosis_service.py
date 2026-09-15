from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.mechanism.rules import assess_abnormality, build_fault_hypotheses
from app.model.gateway import ModelGateway
from app.services.chart_service import ChartService

if TYPE_CHECKING:
    from app.repositories.audit import AuditRepository


class DiagnosisService:
    def __init__(self, chart_service: ChartService, model: ModelGateway, audit: AuditRepository | None):
        self.chart_service = chart_service
        self.model = model
        self.audit = audit

    async def diagnose_point(
        self,
        point_no: str,
        alarm: dict[str, Any] | None,
        waveform: dict[str, Any] | None,
        feature_trends: list[dict[str, Any]] | None,
        temperature_trends: list[dict[str, Any]] | None,
        speed_rpm: float | None,
        use_model: bool,
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(
            self._diagnose_point_deterministic,
            point_no,
            waveform,
            feature_trends or [],
            temperature_trends or [],
            speed_rpm,
        )
        return await self._apply_model(result, {"alarm": alarm or {}, "point_no": point_no}, use_model)

    async def diagnose_device(
        self,
        device_code: str,
        points: list[dict[str, Any]],
        speed_rpm: float | None,
        use_model: bool,
    ) -> dict[str, Any]:
        if not points:
            raise ValueError("points不能为空")

        point_results: list[dict[str, Any]] = []
        for point in points:
            point_no = str(point.get("point_no") or "").strip()
            if not point_no:
                raise ValueError("每个测点都必须提供point_no")
            point_result = await asyncio.to_thread(
                self._diagnose_point_deterministic,
                point_no,
                point.get("waveform"),
                point.get("feature_trends") or [],
                point.get("temperature_trends") or [],
                speed_rpm,
            )
            point_results.append(point_result)

        status = self._device_status(point_results)
        hypotheses = self._merge_hypotheses(point_results)
        summary = self._device_summary(status, point_results, hypotheses)
        recommendations = self._recommendations(hypotheses)
        limitations = self._device_limitations(point_results)
        result = {
            "success": True,
            "status": status,
            "summary": summary,
            "fault_hypotheses": hypotheses,
            "point_results": point_results,
            "recommendations": recommendations,
            "limitations": limitations,
        }
        return await self._apply_model(result, {"device_code": device_code}, use_model)

    def _diagnose_point_deterministic(
        self,
        point_no: str,
        waveform: dict[str, Any] | None,
        feature_trends: list[dict[str, Any]],
        temperature_trends: list[dict[str, Any]],
        speed_rpm: float | None,
    ) -> dict[str, Any]:
        if waveform is None and not feature_trends and not temperature_trends:
            raise ValueError(f"测点 {point_no} 没有可分析的波形或趋势数据")

        waveform_analysis: dict[str, Any] | None = None
        feature_analysis: list[dict[str, Any]] = []
        temperature_analysis: list[dict[str, Any]] = []
        charts: list[dict[str, Any]] = []

        if waveform is not None:
            time_result = self.chart_service.analyze("time_waveform", waveform, None, speed_rpm, None)
            freq_result = self.chart_service.analyze("frequency_spectrum", waveform, None, speed_rpm, None)
            envelope_result = self.chart_service.analyze("envelope_spectrum", waveform, None, speed_rpm, None)
            power_result = self.chart_service.analyze("power_spectrum", waveform, None, speed_rpm, None)
            cepstrum_result = self.chart_service.analyze("cepstrum", waveform, None, speed_rpm, None)
            waveform_analysis = {
                "point_no": point_no,
                "time_domain": time_result["metrics"],
                "frequency_spectrum": freq_result["metrics"],
                "envelope_spectrum": envelope_result["metrics"],
                "power_spectrum": power_result["metrics"],
                "cepstrum": cepstrum_result["metrics"],
            }
            charts.extend([self._chart_only(time_result, point_no), self._chart_only(freq_result, point_no), self._chart_only(envelope_result, point_no)])
            if speed_rpm and speed_rpm > 0:
                order_result = self.chart_service.analyze("order_spectrum", waveform, None, speed_rpm, None)
                envelope_order_result = self.chart_service.analyze("envelope_order_spectrum", waveform, None, speed_rpm, None)
                waveform_analysis["order_spectrum"] = order_result["metrics"]
                waveform_analysis["envelope_order_spectrum"] = envelope_order_result["metrics"]
                charts.extend([self._chart_only(order_result, point_no), self._chart_only(envelope_order_result, point_no)])

        for trend in feature_trends:
            trend_result = self.chart_service.analyze("feature_trend", None, trend, speed_rpm, None)
            feature_analysis.append({"point_id": trend_result.get("point_id"), "series": trend_result["metrics"]["series"]})
            charts.append(self._chart_only(trend_result, point_no))

        for trend in temperature_trends:
            trend_result = self.chart_service.analyze("temperature_trend", None, trend, speed_rpm, None)
            temperature_analysis.append({"point_id": trend_result.get("point_id"), "series": trend_result["metrics"]["series"]})
            charts.append(self._chart_only(trend_result, point_no))

        abnormal_evidence = assess_abnormality(waveform_analysis, feature_analysis, temperature_analysis)
        hypotheses = build_fault_hypotheses(waveform_analysis, abnormal_evidence)
        status = "fault" if hypotheses else ("attention" if abnormal_evidence else "normal")
        analysis = {
            "waveform": waveform_analysis,
            "feature_trends": feature_analysis,
            "temperature_trends": temperature_analysis,
        }
        limitations = self._point_limitations(waveform, feature_trends, temperature_trends, speed_rpm)

        return {
            "success": True,
            "point_no": point_no,
            "status": status,
            "summary": self._point_summary(status, hypotheses, abnormal_evidence),
            "fault_hypotheses": hypotheses,
            "analysis": analysis,
            "charts": charts,
            "recommendations": self._recommendations(hypotheses),
            "limitations": limitations,
        }

    async def _apply_model(self, result: dict[str, Any], context: dict[str, Any], use_model: bool) -> dict[str, Any]:
        # 只有形成明确故障假设后才调用模型，避免模型把正常或待关注状态解释成故障。
        if not use_model or result["status"] != "fault" or not self.model.configured():
            return result

        payload = {
            **context,
            "diagnosis_status": result["status"],
            "fault_hypotheses": result["fault_hypotheses"],
            "analysis": result.get("analysis") or result.get("point_results"),
            "limitations": result["limitations"],
        }
        model_result = await self._call_model(payload)
        if not model_result:
            return result

        result["summary"] = str(model_result.get("summary") or result["summary"])
        model_recommendations = model_result.get("recommendations")
        model_limitations = model_result.get("limitations")
        if isinstance(model_recommendations, list) and model_recommendations:
            result["recommendations"] = [str(item) for item in model_recommendations if str(item).strip()]
        if isinstance(model_limitations, list):
            result["limitations"] = list(dict.fromkeys(result["limitations"] + [str(item) for item in model_limitations if str(item).strip()]))
        return result

    @staticmethod
    def _chart_only(result: dict[str, Any], point_no: str) -> dict[str, Any]:
        data = {"chart_type": result["chart_type"], "point_no": point_no, "chart": result["chart"]}
        if result.get("point_id"):
            data["point_id"] = result["point_id"]
        return data

    @staticmethod
    def _point_summary(status: str, hypotheses: list[dict[str, Any]], abnormal_evidence: list[str]) -> str:
        if status == "fault":
            top = hypotheses[0]
            return f"当前数据存在异常，机理证据更支持{top['fault_name']}，置信度约{float(top['confidence']):.0%}。"
        if status == "attention":
            return "当前数据存在异常变化，但证据不足以确定具体故障类型，建议继续关注并结合工况复核。"
        return "当前数据未发现明确异常或故障机理证据。"

    @staticmethod
    def _device_status(point_results: list[dict[str, Any]]) -> str:
        if any(item["status"] == "fault" for item in point_results):
            return "fault"
        if any(item["status"] == "attention" for item in point_results):
            return "attention"
        return "normal"

    @staticmethod
    def _merge_hypotheses(point_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for point in point_results:
            for item in point["fault_hypotheses"]:
                key = str(item["fault_type"])
                current = merged.get(key)
                if current is None:
                    merged[key] = {
                        "fault_type": key,
                        "fault_name": item["fault_name"],
                        "confidence": float(item["confidence"]),
                        "evidence": list(item.get("evidence") or []),
                    }
                else:
                    current["confidence"] = max(float(current["confidence"]), float(item["confidence"]))
                    current["evidence"].extend(e for e in item.get("evidence") or [] if e not in current["evidence"])
        return sorted(merged.values(), key=lambda item: float(item["confidence"]), reverse=True)

    @staticmethod
    def _device_summary(status: str, point_results: list[dict[str, Any]], hypotheses: list[dict[str, Any]]) -> str:
        fault_points = [item["point_no"] for item in point_results if item["status"] == "fault"]
        attention_points = [item["point_no"] for item in point_results if item["status"] == "attention"]
        if status == "fault":
            top = hypotheses[0]
            return f"共分析{len(point_results)}个测点，其中{len(fault_points)}个测点存在明确故障机理证据，当前更支持{top['fault_name']}。"
        if status == "attention":
            return f"共分析{len(point_results)}个测点，未形成明确故障结论，但{len(attention_points)}个测点存在异常变化，建议持续关注。"
        return f"共分析{len(point_results)}个测点，当前均未发现明确异常或故障机理证据。"

    @staticmethod
    def _recommendations(hypotheses: list[dict[str, Any]]) -> list[str]:
        if not hypotheses:
            return []
        mapping = {
            "rotor_unbalance": ["检查叶轮、转子积灰、质量偏心及动平衡状态。"],
            "misalignment": ["检查联轴器对中、轴系安装和基础变形。"],
            "mechanical_looseness": ["检查地脚、轴承座、连接件和结构松动。"],
            "bearing_impact": ["检查轴承润滑、游隙、滚道和滚动体状态，并结合轴承特征频率复核。"],
        }
        recommendations: list[str] = []
        for item in hypotheses[:3]:
            recommendations.extend(mapping.get(str(item.get("fault_type")), []))
        return list(dict.fromkeys(recommendations))

    @staticmethod
    def _point_limitations(
        waveform: dict[str, Any] | None,
        feature_trends: list[dict[str, Any]],
        temperature_trends: list[dict[str, Any]],
        speed_rpm: float | None,
    ) -> list[str]:
        items: list[str] = []
        if waveform is None:
            items.append("缺少波形数据，无法进行时域和频域分析。")
        if not feature_trends and not temperature_trends:
            items.append("缺少趋势数据，无法判断近期变化方向。")
        if waveform is not None and not speed_rpm:
            items.append("缺少转速，未执行阶比谱和解调阶比谱。")
        return items

    @staticmethod
    def _device_limitations(point_results: list[dict[str, Any]]) -> list[str]:
        items: list[str] = []
        for result in point_results:
            for limitation in result["limitations"]:
                text = f"{result['point_no']}：{limitation}"
                if text not in items:
                    items.append(text)
        return items

    async def _call_model(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        started = datetime.now(timezone.utc)
        begin = time.perf_counter()
        try:
            result, meta = await self.model.diagnose(payload)
            if self.audit:
                await asyncio.to_thread(
                    self.audit.log_model,
                    started,
                    int((time.perf_counter() - begin) * 1000),
                    True,
                    meta.get("model"),
                    meta.get("input_tokens"),
                    meta.get("output_tokens"),
                    None,
                )
            return result
        except Exception as exc:
            if self.audit:
                try:
                    await asyncio.to_thread(
                        self.audit.log_model,
                        started,
                        int((time.perf_counter() - begin) * 1000),
                        False,
                        self.model.settings.llm_model,
                        None,
                        None,
                        str(exc),
                    )
                except Exception:
                    pass
            return None
