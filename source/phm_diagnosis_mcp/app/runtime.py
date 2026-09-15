from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.model.admission import ModelAdmission
from app.model.gateway import ModelGateway
from app.repositories.audit import AuditRepository
from app.services.chart_service import ChartService
from app.services.diagnosis_service import DiagnosisService
from app.services.tool_executor import ToolExecutor
from app.performance import instrument_object


@dataclass
class Runtime:
    settings: Settings
    audit: AuditRepository | None
    model: ModelGateway
    admission: ModelAdmission
    chart: ChartService
    diagnosis: DiagnosisService
    executor: ToolExecutor

    @classmethod
    def create(cls) -> "Runtime":
        settings = get_settings()
        audit = AuditRepository(settings.audit_database_url, settings.audit_retention_days) if settings.audit_enabled else None
        model = ModelGateway(settings)
        chart = ChartService()
        admission = ModelAdmission(model)
        diagnosis = DiagnosisService(chart, model, audit)
        instrument_object(model, component_code="diagnosis_llm", component_cn="Diagnosis 大模型", category="llm")
        instrument_object(chart, component_code="chart", component_cn="诊断图谱算法", category="algorithm")
        if audit is not None:
            instrument_object(audit, component_code="audit", component_cn="诊断审计 PostgreSQL", category="database")
        return cls(
            settings=settings, audit=audit, model=model, admission=admission, chart=chart,
            diagnosis=diagnosis, executor=ToolExecutor(audit, settings),
        )

    def start(self) -> None:
        if self.audit:
            self.audit.start()

    def close(self) -> None:
        if self.audit:
            self.audit.close()
