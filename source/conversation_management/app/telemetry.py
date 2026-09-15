from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode

from app.config import Settings

_provider: TracerProvider | None = None
_httpx_instrumented = False
_redis_instrumented = False
_sqlalchemy_engines: set[int] = set()
_fastapi_apps: set[int] = set()


def _parse_headers(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    result: dict[str, str] = {}
    for item in value.split(","):
        key, separator, raw = item.partition("=")
        if separator and key.strip():
            result[key.strip()] = raw.strip()
    return result or None


def configure_tracing(settings: Settings) -> TracerProvider | None:
    global _provider
    if not settings.otel_enabled:
        return None
    if _provider is not None:
        return _provider

    ratio = min(1.0, max(0.0, settings.otel_trace_sample_ratio))
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": settings.otel_service_version,
                "deployment.environment": settings.app_env,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )
    if settings.otel_exporter_otlp_endpoint:
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            headers=_parse_headers(settings.otel_exporter_otlp_headers),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    if settings.otel_console_exporter:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def instrument_fastapi(app: Any, settings: Settings) -> None:
    provider = configure_tracing(settings)
    if provider is None or id(app) in _fastapi_apps:
        return
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=settings.otel_excluded_urls,
    )
    _fastapi_apps.add(id(app))


def instrument_clients(*, settings: Settings, sqlalchemy_engine: Any) -> None:
    global _httpx_instrumented, _redis_instrumented
    provider = configure_tracing(settings)
    if provider is None:
        return
    if not _httpx_instrumented:
        HTTPXClientInstrumentor().instrument(tracer_provider=provider)
        _httpx_instrumented = True
    if not _redis_instrumented:
        RedisInstrumentor().instrument(tracer_provider=provider)
        _redis_instrumented = True
    engine_key = id(sqlalchemy_engine)
    if engine_key not in _sqlalchemy_engines:
        SQLAlchemyInstrumentor().instrument(
            engine=sqlalchemy_engine.sync_engine,
            tracer_provider=provider,
        )
        _sqlalchemy_engines.add(engine_key)


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def inject_trace_context(carrier: dict[str, str] | None = None) -> dict[str, str]:
    target = carrier if carrier is not None else {}
    propagate.inject(target)
    return target


def extract_trace_context(carrier: Mapping[str, str] | None):
    return propagate.extract(dict(carrier or {}))


def current_trace_ids() -> tuple[str, str]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return "", ""
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


def set_attributes(span: Span, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            span.set_attribute(key, value)
        else:
            span.set_attribute(key, str(value))


def mark_span_error(span: Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


@contextmanager
def use_extracted_context(carrier: Mapping[str, str] | None) -> Iterator[None]:
    context = extract_trace_context(carrier)
    token = otel_context.attach(context)
    try:
        yield
    finally:
        otel_context.detach(token)


def shutdown_tracing() -> None:
    global _provider
    if _provider is None:
        return
    try:
        _provider.force_flush(timeout_millis=5000)
        _provider.shutdown()
    finally:
        _provider = None
