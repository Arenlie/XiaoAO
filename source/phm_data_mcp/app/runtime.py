from __future__ import annotations

from dataclasses import dataclass

from app.alarm.service import AlarmService
from app.config import Settings, get_settings
from app.repositories.audit import AuditRepository
from app.repositories.health import HealthRepository
from app.repositories.mongo import MongoRepository
from app.repositories.mysql import MySqlRepository
from app.services.data_service import DataService
from app.services.health_service import HealthService
from app.services.tool_executor import ToolExecutor
from app.performance import instrument_object


@dataclass
class Runtime:
    settings: Settings
    mongo: MongoRepository
    mysql: MySqlRepository
    health_repository: HealthRepository
    audit: AuditRepository
    data: DataService
    health: HealthService
    alarm: AlarmService
    executor: ToolExecutor

    @classmethod
    def create(cls) -> "Runtime":
        settings = get_settings()
        mongo = MongoRepository(settings)
        mysql = MySqlRepository(settings)
        health_repository = HealthRepository(settings)
        audit = AuditRepository(settings)
        data = DataService(settings, mongo)
        health = HealthService(health_repository)
        alarm = AlarmService(settings, mysql)
        instrument_object(mongo, component_code="mongo", component_cn="MongoDB 数据库", category="database")
        instrument_object(mysql, component_code="mysql", component_cn="MySQL 报警数据库", category="database")
        instrument_object(health_repository, component_code="health_store", component_cn="健康度数据源", category="database")
        instrument_object(alarm.llm, component_code="alarm_llm", component_cn="报警 SQL 规划大模型", category="llm")
        instrument_object(audit, component_code="audit", component_cn="审计 PostgreSQL", category="database")
        return cls(
            settings=settings, mongo=mongo, mysql=mysql, health_repository=health_repository,
            audit=audit, data=data, health=health, alarm=alarm, executor=ToolExecutor(audit, settings),
        )

    def start(self) -> None:
        self.audit.start()

    def close(self) -> None:
        self.mongo.close()
        self.health_repository.close()
        self.audit.close()
