#!/usr/bin/env bash
set -Eeuo pipefail

BASE="/home/chaos/program"
APP="$BASE/conversation_management"
PY="$APP/.venv/bin/python"

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "用法: sudo /home/chaos/program/collect_task_trace.sh <task-id>" >&2
  echo "示例: sudo /home/chaos/program/collect_task_trace.sh 6296700d-facf-4169-8253-e82d58687f2e" >&2
  exit 2
fi

if [[ ! -x "$PY" ]]; then
  echo "错误：找不到对话后端 Python 环境：$PY" >&2
  echo "请确认 /home/chaos/program/conversation_management/.venv 已存在。" >&2
  exit 3
fi

export PHM_COLLECT_BASE="$BASE"
export PHM_COLLECT_MODE="task"
export PHM_COLLECT_ID="$1"
cd "$APP"

"$PY" - <<'PY'
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import traceback
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

COLLECTOR_VERSION = "2026-09-12.2"
BASE = Path(os.environ.get("PHM_COLLECT_BASE", "/home/chaos/program")).resolve()
APP = BASE / "conversation_management"
TARGET = os.environ["PHM_COLLECT_ID"].strip()
MODE = os.environ["PHM_COLLECT_MODE"].strip().lower()
EXPORT_ROOT = BASE / "phm_debug_exports"
COMPONENTS = [
    "conversation_management",
    "phm_assert_mcp",
    "phm_data_mcp",
    "phm_feature_mcp",
    "phm_diagnosis_mcp",
]
CODE_SUFFIXES = {
    ".py", ".sh", ".toml", ".ini", ".cfg", ".json", ".yaml", ".yml",
    ".html", ".js", ".css", ".sql", ".service", ".md", ".txt",
}
EXCLUDED_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", "logs", "log",
    "vendor", "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "phm_upgrade_backups", "phm_debug_exports",
}
SENSITIVE_EXACT = {
    "authorization", "api_key", "apikey", "password", "passwd", "secret",
    "client_secret", "access_token", "refresh_token", "data_access_token",
    "minio_access_key", "minio_secret_key", "asr_api_key", "agent_api_key",
    "supervisor_model_api_key", "quick_model_api_key", "multimodal_api_key",
    "dify_knowledge_api_key", "entity_workflow_api_key",
}
TOKENISH_EXACT = {"user_token"}

if MODE not in {"task", "conversation"}:
    raise SystemExit(f"unsupported mode: {MODE}")
try:
    TARGET_UUID = UUID(TARGET)
except Exception as exc:
    raise SystemExit(f"ID 不是合法 UUID: {TARGET}: {exc}")

if not APP.exists():
    raise SystemExit(f"找不到对话后端目录: {APP}")

os.chdir(APP)
sys.path.insert(0, str(APP))

from sqlalchemy import text  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.security_context import rls_bypass_scope  # noqa: E402

settings = get_settings()


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def _key_norm(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_")


def _is_secret_key(key: Any) -> bool:
    k = _key_norm(key)
    return (
        k in SENSITIVE_EXACT
        or k.endswith("_password")
        or k.endswith("_passwd")
        or k.endswith("_secret")
        or k.endswith("_api_key")
        or k.endswith("_access_key")
        or (k.endswith("_token") and k != "user_token")
    )


def hash_token(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return f"<sha256:{hashlib.sha256(raw.encode('utf-8', errors='ignore')).hexdigest()[:16]}>"


def sanitize_url(value: str) -> str:
    # Hide passwords in URLs while retaining host/port/database for diagnosis.
    return re.sub(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@", r"\g<scheme>\g<user>:***@", value)


def redact(value: Any, key: Any = None, depth: int = 0) -> Any:
    if depth > 30:
        return "<max-depth>"
    if key is not None and _is_secret_key(key):
        return "***REDACTED***"
    if key is not None and _key_norm(key) in TOKENISH_EXACT:
        return hash_token(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, bytes):
        # Do not dump arbitrary binary blobs. Hash + length are enough unless decoded by checkpointer.
        return {"_bytes_len": len(value), "_sha256": hashlib.sha256(value).hexdigest()}
    if hasattr(value, "model_dump"):
        try:
            return redact(value.model_dump(mode="json"), key=key, depth=depth + 1)
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(k): redact(v, key=k, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v, depth=depth + 1) for v in value]
    if isinstance(value, str):
        if key is not None and _key_norm(key).endswith("_url"):
            return sanitize_url(value)
        return value
    return str(value)


def redact_text(text_value: str) -> str:
    text_value = sanitize_url(text_value)
    patterns = [
        (r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;]+", r"\1***REDACTED***"),
        (r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|client[_-]?secret|data[_-]?access[_-]?token)\s*[:=]\s*)[^\s,;]+", r"\1***REDACTED***"),
    ]
    for pat, repl in patterns:
        text_value = re.sub(pat, repl, text_value)
    return text_value


def json_default(value: Any) -> Any:
    return redact(value)


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")


def dump_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_text(value), encoding="utf-8", errors="replace")


def run_cmd(args: list[str], timeout: int = 60, cwd: Path | None = None) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return cp.returncode, cp.stdout
    except Exception as exc:
        return 999, f"COMMAND_FAILED: {args!r}: {type(exc).__name__}: {exc}\n"


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_code_snapshot(outdir: Path) -> dict[str, Any]:
    snap = outdir / "code_snapshot"
    copied: list[dict[str, Any]] = []
    errors: list[str] = []
    for component in COMPONENTS:
        src_root = BASE / component
        if not src_root.exists():
            errors.append(f"missing component: {src_root}")
            continue
        for src in src_root.rglob("*"):
            try:
                if not src.is_file():
                    continue
                rel = src.relative_to(src_root)
                if any(part in EXCLUDED_DIRS for part in rel.parts):
                    continue
                if src.name == ".env" or src.name.startswith(".env."):
                    continue
                if src.suffix.lower() not in CODE_SUFFIXES and src.name not in {"Dockerfile", "Makefile"}:
                    continue
                # Do not export private keys/certificates even if misnamed as text.
                lower = src.name.lower()
                if lower.endswith((".key", ".pem", ".p12", ".pfx")):
                    continue
                dest = snap / component / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied.append({
                    "path": f"{component}/{rel.as_posix()}",
                    "size": src.stat().st_size,
                    "sha256": sha256_file(src),
                })
            except Exception as exc:
                errors.append(f"{src}: {type(exc).__name__}: {exc}")
    dump_json(outdir / "runtime" / "code_manifest.json", {"files": copied, "errors": errors})
    return {"file_count": len(copied), "errors": errors}


def sanitize_env_file(outdir: Path) -> None:
    src = APP / ".env"
    if not src.exists():
        dump_text(outdir / "runtime" / "conversation_management.env.sanitized", "<missing .env>\n")
        return
    lines: list[str] = []
    for raw in src.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            lines.append(raw)
            continue
        key, val = raw.split("=", 1)
        k = key.strip()
        if _is_secret_key(k) or _key_norm(k) in TOKENISH_EXACT:
            val = "***REDACTED***"
        elif _key_norm(k).endswith("_url") or _key_norm(k) in {"database_url", "redis_url"}:
            val = sanitize_url(val)
        lines.append(f"{key}={val}")
    dump_text(outdir / "runtime" / "conversation_management.env.sanitized", "\n".join(lines) + "\n")


def discover_units() -> list[str]:
    candidates: set[str] = set()
    for cmd in (
        ["systemctl", "list-units", "--all", "--type=service", "--no-legend", "--no-pager"],
        ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"],
    ):
        rc, output = run_cmd(cmd, timeout=20)
        if rc not in {0, 1}:
            continue
        for line in output.splitlines():
            unit = line.split(None, 1)[0] if line.strip() else ""
            if re.search(r"(^conversation[-_]|^phm[-_](?:asset|data|feature|diagnosis)|sensor[-_]detect)", unit, re.I):
                candidates.add(unit)
    # Known production names should be checked even if a template instance is currently inactive.
    candidates.update({
        "conversation-api.service",
        "phm-asset-mcp.service",
        "phm-data-mcp.service",
        "phm-feature-mcp.service",
        "phm_feature_mcp.service",
        "phm-diagnosis-mcp.service",
    })
    valid = []
    for unit in sorted(candidates):
        rc, output = run_cmd(["systemctl", "show", unit, "--property=LoadState", "--value"], timeout=8)
        if rc == 0 and output.strip() not in {"", "not-found"}:
            valid.append(unit)
    return valid


def collect_runtime(outdir: Path, relevant_ids: list[str]) -> list[str]:
    runtime = outdir / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    settings_dump = settings.model_dump(mode="json") if hasattr(settings, "model_dump") else {}
    settings_dump["app_version"] = getattr(settings, "app_version", "unknown")
    dump_json(runtime / "settings.sanitized.json", settings_dump)
    sanitize_env_file(outdir)

    system_info = {
        "collector_version": COLLECTOR_VERSION,
        "mode": MODE,
        "target_id": TARGET,
        "collected_at_utc": now_utc(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version,
        "cwd": str(Path.cwd()),
        "base": str(BASE),
    }
    dump_json(runtime / "system_info.json", system_info)

    units = discover_units()
    dump_json(runtime / "service_units.json", units)
    status_parts = []
    cat_dir = runtime / "systemd_units"
    cat_dir.mkdir(parents=True, exist_ok=True)
    props = "Id,LoadState,ActiveState,SubState,MainPID,ExecMainStatus,ExecMainStartTimestamp,FragmentPath,User,Group,WorkingDirectory,ExecStart"
    for unit in units:
        rc, output = run_cmd(["systemctl", "show", unit, f"--property={props}"], timeout=12)
        status_parts.append(f"===== {unit} =====\n{output}\n")
        rc2, cat = run_cmd(["systemctl", "cat", unit], timeout=12)
        dump_text(cat_dir / f"{safe_name(unit)}.txt", cat)
    dump_text(runtime / "service_status.txt", "\n".join(status_parts))

    # Readiness snapshots. Failures are recorded, never fatal to collection.
    endpoints = {
        "conversation_ready": f"http://127.0.0.1:{settings.app_port}/ready",
        "asset_ready": settings.phm_asset_mcp_ready_url,
        "data_ready": settings.phm_data_mcp_ready_url,
        "diagnosis_ready": settings.phm_diagnosis_mcp_ready_url,
    }
    feature = str(getattr(settings, "phm_feature_mcp_url", "") or "")
    if feature:
        endpoints["feature_ready"] = feature[:-4] + "/ready" if feature.rstrip("/").endswith("/mcp") else feature.rstrip("/") + "/ready"
    health: dict[str, Any] = {}
    for name, url in endpoints.items():
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=4) as resp:
                body = resp.read(1024 * 1024).decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = body
                health[name] = {"url": sanitize_url(url), "status": resp.status, "body": parsed}
        except urllib.error.HTTPError as exc:
            body = exc.read(1024 * 1024).decode("utf-8", errors="replace")
            health[name] = {"url": sanitize_url(url), "status": exc.code, "error": body}
        except Exception as exc:
            health[name] = {"url": sanitize_url(url), "error": f"{type(exc).__name__}: {exc}"}
    dump_json(runtime / "health.json", health)

    # Installed dependency snapshots from every component venv that exists.
    dep_dir = runtime / "dependencies"
    dep_dir.mkdir(parents=True, exist_ok=True)
    for component in COMPONENTS:
        py = BASE / component / ".venv" / "bin" / "python"
        if py.exists():
            rc, output = run_cmd([str(py), "-m", "pip", "freeze"], timeout=40)
            dump_text(dep_dir / f"{component}.pip-freeze.txt", output)

    # Exact runtime source is intentionally included. This lets code fixes target what actually ran.
    copy_code_snapshot(outdir)
    return units


async def fetch_all(session, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    result = await session.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings().all()]


async def fetch_one(session, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = await fetch_all(session, sql, params)
    return rows[0] if rows else None


async def table_exists(session, table_name: str) -> bool:
    row = await fetch_one(session, """
        SELECT EXISTS (
          SELECT 1 FROM information_schema.tables
          WHERE table_schema='public' AND table_name=:name
        ) AS ok
    """, {"name": table_name})
    return bool(row and row.get("ok"))


async def collect_db(outdir: Path) -> dict[str, Any]:
    db = Database(settings)
    meta: dict[str, Any] = {"mode": MODE, "target_id": TARGET}
    try:
        with rls_bypass_scope():
            async with db.session_factory() as session:
                if MODE == "task":
                    target_task = await fetch_one(session, "SELECT * FROM generation_tasks WHERE id=:id", {"id": TARGET_UUID})
                    if not target_task:
                        raise RuntimeError(f"generation_tasks 中找不到 task_id={TARGET}")
                    conversation_id = str(target_task["conversation_id"])
                    conversation_uuid = UUID(conversation_id)
                    target_created = target_task.get("created_at")
                    target_end = target_task.get("completed_at") or target_task.get("heartbeat_at") or now_utc()
                    all_tasks = await fetch_all(session, "SELECT * FROM generation_tasks WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid})
                    causal_tasks = [row for row in all_tasks if not target_created or not row.get("created_at") or row["created_at"] <= target_created]
                    relevant_tasks = causal_tasks
                    messages_all = await fetch_all(session, "SELECT * FROM messages WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid})
                    causal_ids = {str(row.get("user_message_id")) for row in causal_tasks} | {str(row.get("assistant_message_id")) for row in causal_tasks if row.get("assistant_message_id")}
                    messages_causal = [m for m in messages_all if str(m.get("id")) in causal_ids or (not target_created or not m.get("created_at") or m["created_at"] <= target_created)]
                else:
                    conversation_id = TARGET
                    conversation_uuid = TARGET_UUID
                    target_task = None
                    target_created = None
                    target_end = None
                    all_tasks = await fetch_all(session, "SELECT * FROM generation_tasks WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid})
                    relevant_tasks = all_tasks
                    messages_all = await fetch_all(session, "SELECT * FROM messages WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid})
                    messages_causal = messages_all

                conversation = await fetch_one(session, "SELECT * FROM conversations WHERE id=:cid", {"cid": conversation_uuid})
                if not conversation:
                    raise RuntimeError(f"conversations 中找不到 conversation_id={conversation_id}")

                branches = await fetch_all(session, "SELECT * FROM conversation_branches WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_id})
                pending = await fetch_all(session, "SELECT * FROM pending_entity_selections WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid}) if await table_exists(session, "pending_entity_selections") else []
                snapshots = await fetch_all(session, "SELECT * FROM context_snapshots WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid}) if await table_exists(session, "context_snapshots") else []
                outbox = await fetch_all(session, "SELECT * FROM outbox_events WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid}) if await table_exists(session, "outbox_events") else []
                attachments = await fetch_all(session, "SELECT * FROM attachments WHERE conversation_id=:cid ORDER BY created_at,id", {"cid": conversation_uuid}) if await table_exists(session, "attachments") else []
                manifests = await fetch_all(session, """
                    SELECT m.* FROM attachment_content_manifests m
                    JOIN attachments a ON a.id=m.attachment_id
                    WHERE a.conversation_id=:cid ORDER BY m.created_at,m.id
                """, {"cid": conversation_uuid}) if await table_exists(session, "attachment_content_manifests") else []
                artifacts = await fetch_all(session, """
                    SELECT x.* FROM attachment_content_artifacts x
                    JOIN attachments a ON a.id=x.attachment_id
                    WHERE a.conversation_id=:cid ORDER BY x.created_at,x.sequence_no,x.id
                """, {"cid": conversation_uuid}) if await table_exists(session, "attachment_content_artifacts") else []
                chunks = await fetch_all(session, """
                    SELECT x.* FROM attachment_content_chunks x
                    JOIN attachments a ON a.id=x.attachment_id
                    WHERE a.conversation_id=:cid ORDER BY x.created_at,x.sequence_no,x.id
                """, {"cid": conversation_uuid}) if await table_exists(session, "attachment_content_chunks") else []

                # Events for the causal tasks (task collector) or all tasks (conversation collector).
                if MODE == "task":
                    events = await fetch_all(session, """
                        SELECT e.* FROM task_execution_events e
                        JOIN generation_tasks t ON t.id=e.task_id
                        WHERE t.conversation_id=:cid AND t.created_at <= :cutoff
                        ORDER BY t.created_at,e.sequence_no,e.id
                    """, {"cid": conversation_uuid, "cutoff": target_created})
                else:
                    events = await fetch_all(session, """
                        SELECT e.* FROM task_execution_events e
                        JOIN generation_tasks t ON t.id=e.task_id
                        WHERE t.conversation_id=:cid
                        ORDER BY t.created_at,e.sequence_no,e.id
                    """, {"cid": conversation_uuid})

                agent_runtime = await fetch_all(session, "SELECT * FROM agent_runtime_configs ORDER BY agent_id") if await table_exists(session, "agent_runtime_configs") else []
                user_profile = []
                if await table_exists(session, "user_profiles") and conversation.get("user_token"):
                    user_profile = await fetch_all(session, "SELECT * FROM user_profiles WHERE user_token=:ut", {"ut": conversation["user_token"]})

                schema = await fetch_all(session, """
                    SELECT table_name,column_name,data_type,udt_name,is_nullable,ordinal_position
                    FROM information_schema.columns
                    WHERE table_schema='public'
                    ORDER BY table_name,ordinal_position
                """)

                dbdir = outdir / "database"
                dump_json(dbdir / "conversation.json", conversation)
                dump_json(dbdir / "branches.json", branches)
                dump_json(dbdir / "messages_all.json", messages_all)
                dump_json(dbdir / "messages_causal.json", messages_causal)
                dump_json(dbdir / "generation_tasks_all.json", all_tasks)
                dump_json(dbdir / "generation_tasks_relevant.json", relevant_tasks)
                if target_task:
                    dump_json(dbdir / "target_task.json", target_task)
                dump_json(dbdir / "task_execution_events.json", events)
                dump_json(dbdir / "pending_entity_selections.json", pending)
                dump_json(dbdir / "context_snapshots.json", snapshots)
                dump_json(dbdir / "outbox_events.json", outbox)
                dump_json(dbdir / "attachments.json", attachments)
                dump_json(dbdir / "attachment_content_manifests.json", manifests)
                dump_json(dbdir / "attachment_content_artifacts.json", artifacts)
                dump_json(dbdir / "attachment_content_chunks.json", chunks)
                dump_json(dbdir / "agent_runtime_configs.json", agent_runtime)
                dump_json(dbdir / "user_profile.json", user_profile)
                dump_json(dbdir / "schema_columns.json", schema)

                # Evidence Fabric state is exported in dedicated directories so a
                # trace can answer: which Topic, which Evidence, what lineage, and
                # which requirements/plan were used for this task/conversation.
                evidence_objects = await fetch_all(session,
                    "SELECT * FROM evidence_objects WHERE conversation_id=:cid ORDER BY created_at,evidence_id",
                    {"cid": conversation_uuid},
                ) if await table_exists(session, "evidence_objects") else []
                evidence_lineage = await fetch_all(session, """
                    SELECT l.* FROM evidence_lineage_edges l
                    JOIN evidence_objects e ON e.evidence_id=l.child_evidence_id
                    WHERE e.conversation_id=:cid ORDER BY l.created_at,l.parent_evidence_id,l.child_evidence_id
                """, {"cid": conversation_uuid}) if await table_exists(session, "evidence_lineage_edges") else []
                task_evidence_refs = await fetch_all(session, """
                    SELECT r.* FROM task_evidence_refs r
                    JOIN generation_tasks t ON t.id=r.task_id
                    WHERE t.conversation_id=:cid ORDER BY r.created_at,r.task_id,r.direction
                """, {"cid": conversation_uuid}) if await table_exists(session, "task_evidence_refs") else []
                topics = await fetch_all(session,
                    "SELECT * FROM conversation_topics WHERE conversation_id=:cid ORDER BY updated_at DESC,topic_id",
                    {"cid": conversation_uuid},
                ) if await table_exists(session, "conversation_topics") else []
                hot_slots = await fetch_all(session,
                    "SELECT * FROM conversation_context_slots WHERE conversation_id=:cid ORDER BY slot_no",
                    {"cid": conversation_uuid},
                ) if await table_exists(session, "conversation_context_slots") else []
                dump_json(outdir / "evidence" / "evidence_objects.json", evidence_objects)
                dump_json(outdir / "evidence" / "evidence_lineage.json", evidence_lineage)
                dump_json(outdir / "evidence" / "task_evidence_refs.json", task_evidence_refs)
                dump_json(outdir / "context" / "topics.json", topics)
                dump_json(outdir / "context" / "hot_slots.json", hot_slots)

                def event_payloads(*event_types: str) -> list[dict[str, Any]]:
                    wanted = set(event_types)
                    output = []
                    for event in events:
                        if str(event.get("event_type") or "") not in wanted:
                            continue
                        output.append({
                            "task_id": event.get("task_id"),
                            "sequence_no": event.get("sequence_no"),
                            "event_type": event.get("event_type"),
                            "created_at": event.get("created_at"),
                            "payload": event.get("payload") or {},
                        })
                    return output

                dump_json(outdir / "planning" / "task_delta.json", event_payloads("context.topic.resolved"))
                dump_json(outdir / "planning" / "evidence_requirements.json", event_payloads("planner.requirements.created"))
                dump_json(outdir / "planning" / "execution_plan.json", event_payloads("planner.capability.selected", "completion.evidence.checked"))

                # Human-readable timeline avoids having to inspect a huge JSON first.
                timeline_path = dbdir / "timeline.tsv"
                timeline_path.parent.mkdir(parents=True, exist_ok=True)
                with timeline_path.open("w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh, delimiter="\t")
                    writer.writerow(["task_id", "sequence", "created_at", "event_type", "stage", "actor", "status", "duration_ms", "error_code", "error_message", "payload_preview"])
                    for e in events:
                        payload = redact(e.get("payload") or {})
                        preview = json.dumps(payload, ensure_ascii=False, default=json_default)
                        if len(preview) > 1400:
                            preview = preview[:1400] + "…"
                        writer.writerow([
                            e.get("task_id"), e.get("sequence_no"), iso(e.get("created_at")), e.get("event_type"),
                            e.get("stage"), f"{e.get('actor_type')}:{e.get('actor_id')}", e.get("status"),
                            e.get("duration_ms"), e.get("error_code"), e.get("error_message"), preview,
                        ])

                meta.update({
                    "conversation_id": conversation_id,
                    "target_task": target_task,
                    "tasks": relevant_tasks,
                    "all_tasks": all_tasks,
                    "messages": messages_causal if MODE == "task" else messages_all,
                    "messages_all": messages_all,
                    "messages_causal": messages_causal,
                    "events": events,
                    "attachments": attachments,
                    "target_created": target_created,
                    "target_end": target_end,
                    "conversation": conversation,
                })
    finally:
        await db.close()
    return meta


async def collect_redis(outdir: Path, meta: dict[str, Any]) -> None:
    redis_dir = outdir / "redis"
    redis_dir.mkdir(parents=True, exist_ok=True)
    try:
        from redis.asyncio import Redis
        client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=min(5.0, float(settings.redis_socket_timeout_seconds)),
            socket_connect_timeout=min(3.0, float(settings.redis_connect_timeout_seconds)),
        )
        await client.ping()
        rows: dict[str, Any] = {}
        conversation_id = str(meta["conversation_id"])
        tasks = meta.get("tasks") or []
        # Exact keys only: never use KEYS/SCAN over the production database.
        exact_keys: list[str] = [f"chat:lock:conversation:{conversation_id}"]
        for task in tasks:
            tid = str(task.get("id"))
            exact_keys.extend([f"chat:task_context:{tid}", f"chat:events:{tid}", f"chat:cancel:{tid}"])
        for key in exact_keys:
            try:
                typ = await client.type(key)
                if typ == "none":
                    continue
                ttl = await client.ttl(key)
                entry: dict[str, Any] = {"type": typ, "ttl": ttl}
                if typ == "string":
                    raw = await client.get(key)
                    try:
                        entry["value"] = json.loads(raw) if raw else raw
                    except Exception:
                        entry["value"] = raw
                elif typ == "stream":
                    length = await client.xlen(key)
                    entry["length"] = length
                    # Exact task event streams are safe to read; cap only absurdly large streams.
                    limit = min(max(1, int(length)), 10000)
                    entry["entries"] = await client.xrange(key, min="-", max="+", count=limit)
                    if length > limit:
                        entry["truncated"] = length - limit
                elif typ == "hash":
                    entry["value"] = await client.hgetall(key)
                elif typ == "zset":
                    entry["value"] = await client.zrange(key, 0, -1, withscores=True)
                elif typ == "list":
                    entry["value"] = await client.lrange(key, 0, 9999)
                else:
                    entry["note"] = f"value collection not implemented for redis type={typ}"
                rows[key] = entry
            except Exception as exc:
                rows[key] = {"error": f"{type(exc).__name__}: {exc}"}
        # Queue/group state is useful without reading the entire shared generation stream.
        queue_state: dict[str, Any] = {}
        for stream in ["chat:generation:queue", "chat:title:queue", "chat:summary:queue", "chat:dify-cleanup:queue"]:
            try:
                queue_state[stream] = {
                    "length": await client.xlen(stream),
                    "groups": await client.xinfo_groups(stream),
                }
            except Exception as exc:
                queue_state[stream] = {"error": f"{type(exc).__name__}: {exc}"}
        dump_json(redis_dir / "exact_keys.json", rows)
        dump_json(redis_dir / "queue_state.json", queue_state)
        await client.aclose()
    except Exception as exc:
        dump_text(redis_dir / "ERROR.txt", f"Redis collection failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")


async def collect_checkpoints(outdir: Path, meta: dict[str, Any]) -> None:
    cpdir = outdir / "checkpoints"
    cpdir.mkdir(parents=True, exist_ok=True)
    if not bool(getattr(settings, "langgraph_checkpoint_enabled", False)):
        dump_json(cpdir / "status.json", {"enabled": False})
        return
    task_ids = [str(row.get("id")) for row in (meta.get("tasks") or []) if row.get("id")]
    if not task_ids:
        dump_json(cpdir / "status.json", {"enabled": True, "note": "no relevant tasks"})
        return

    db = Database(settings)
    pairs: list[dict[str, str]] = []
    try:
        with rls_bypass_scope():
            async with db.session_factory() as session:
                if not await table_exists(session, "checkpoints"):
                    dump_json(cpdir / "status.json", {"enabled": True, "table_exists": False})
                    return
                for tid in task_ids:
                    rows = await fetch_all(session, """
                        SELECT DISTINCT thread_id, checkpoint_ns
                        FROM checkpoints
                        WHERE thread_id LIKE :prefix
                        ORDER BY thread_id, checkpoint_ns
                    """, {"prefix": tid + ":%"})
                    for row in rows:
                        pairs.append({"thread_id": str(row.get("thread_id")), "checkpoint_ns": str(row.get("checkpoint_ns") or "")})
                # Raw checkpoint metadata is useful even when the Python saver cannot decode old serializer data.
                raw_rows: list[dict[str, Any]] = []
                for pair in pairs:
                    raw_rows.extend(await fetch_all(session, """
                        SELECT thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata
                        FROM checkpoints
                        WHERE thread_id=:thread_id AND checkpoint_ns=:checkpoint_ns
                        ORDER BY checkpoint_id
                    """, pair))
                dump_json(cpdir / "raw_checkpoints.json", raw_rows)
    finally:
        await db.close()

    decoded: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        from app.orchestration.checkpoint import open_checkpointer
        async with open_checkpointer(settings) as saver:
            if saver is not None:
                for pair in pairs:
                    config = {"configurable": pair}
                    try:
                        count = 0
                        async for item in saver.alist(config, limit=200):
                            decoded.append({
                                "config": getattr(item, "config", None),
                                "checkpoint": getattr(item, "checkpoint", None),
                                "metadata": getattr(item, "metadata", None),
                                "parent_config": getattr(item, "parent_config", None),
                                "pending_writes": getattr(item, "pending_writes", None),
                            })
                            count += 1
                            if count >= 200:
                                break
                    except Exception as exc:
                        errors.append(f"{pair}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        errors.append(f"open_checkpointer: {type(exc).__name__}: {exc}")
    dump_json(cpdir / "decoded_checkpoints.json", decoded)
    dump_json(cpdir / "status.json", {"enabled": True, "pairs": pairs, "decoded_count": len(decoded), "errors": errors})


def journal_window(task: dict[str, Any]) -> tuple[datetime, datetime]:
    start = task.get("created_at") or task.get("started_at") or now_utc()
    end = task.get("completed_at") or task.get("heartbeat_at") or now_utc()
    if not isinstance(start, datetime):
        start = now_utc() - timedelta(minutes=10)
    if not isinstance(end, datetime):
        end = now_utc()
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return start.astimezone(UTC) - timedelta(minutes=2), end.astimezone(UTC) + timedelta(minutes=2)


def journal_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def journal_epoch(dt: datetime) -> str:
    # systemd.time accepts @<unix-seconds>.  Epoch bounds avoid the server-local
    # timezone ambiguity that previously produced empty journal windows.
    return f"@{dt.astimezone(UTC).timestamp():.6f}"


def collect_journals(outdir: Path, meta: dict[str, Any], units: list[str]) -> None:
    jdir = outdir / "journals"
    jdir.mkdir(parents=True, exist_ok=True)
    if not units:
        dump_text(jdir / "ERROR.txt", "No matching systemd service units discovered.\n")
        return
    unit_args: list[str] = []
    for unit in units:
        unit_args.extend(["-u", unit])
    tasks = meta.get("tasks") or []
    # Task collector: write one complete window for target and causal tasks.
    # Conversation collector: one file per task prevents a long conversation from producing a huge unrelated journal span.
    for task in tasks:
        tid = str(task.get("id"))
        start, end = journal_window(task)
        args = ["journalctl", "--utc", "--no-pager", "-o", "short-iso-precise", "--since", journal_epoch(start), "--until", journal_epoch(end), *unit_args]
        rc, output = run_cmd(args, timeout=90)
        # Older systemd builds occasionally reject fractional epoch syntax. Retry
        # with integer epoch seconds before declaring the journal empty.
        retry_rc = None
        retry_output = ""
        if rc != 0:
            retry_args = ["journalctl", "--utc", "--no-pager", "-o", "short-iso-precise", "--since", f"@{int(start.timestamp())}", "--until", f"@{int(end.timestamp())}", *unit_args]
            retry_rc, retry_output = run_cmd(retry_args, timeout=90)
            if retry_rc == 0:
                rc, output = retry_rc, retry_output
        header = (
            f"# task_id={tid}\n"
            f"# since_utc={journal_time(start)}\n"
            f"# until_utc={journal_time(end)}\n"
            f"# since_epoch={start.timestamp():.6f}\n"
            f"# until_epoch={end.timestamp():.6f}\n"
            f"# units={','.join(units)}\n"
            f"# journalctl_rc={rc}\n"
            f"# retry_rc={retry_rc}\n\n"
        )
        dump_text(jdir / f"task_{safe_name(tid)}.log", header + output)


def collect_file_log_matches(outdir: Path, meta: dict[str, Any]) -> None:
    ids: set[str] = {str(meta.get("conversation_id") or "")}
    for task in meta.get("tasks") or []:
        for key in ("id", "request_id", "entity_workflow_run_id", "dify_task_id", "user_message_id", "assistant_message_id"):
            val = task.get(key)
            if val:
                ids.add(str(val))
    ids = {x for x in ids if x and len(x) >= 8}
    ldir = outdir / "file_logs"
    ldir.mkdir(parents=True, exist_ok=True)
    patterns = ldir / "patterns.txt"
    patterns.write_text("\n".join(sorted(ids)) + "\n", encoding="utf-8")
    roots = []
    for component in COMPONENTS:
        for cand in (BASE / component / "logs", BASE / component / "log"):
            if cand.exists() and cand.is_dir():
                roots.append(cand)
    if not roots:
        dump_text(ldir / "matches.txt", "No component log directories found. Journald files remain authoritative for this bundle.\n")
        return
    cmd = ["grep", "-RInaF", "-C", "2", "-f", str(patterns), *[str(p) for p in roots]]
    rc, output = run_cmd(cmd, timeout=90)
    dump_text(ldir / "matches.txt", f"# grep_rc={rc}\n# roots={roots}\n\n" + output)


def collect_category_resolution_trace(outdir: Path, meta: dict[str, Any]) -> None:
    """Extract the asset-category decision chain needed for taxonomy debugging."""
    adir = outdir / "analysis"
    adir.mkdir(parents=True, exist_ok=True)
    related = []
    for event in meta.get("events") or []:
        text_value = json.dumps(event, ensure_ascii=False, default=str)
        if (
            "mcp.phm_asset.query_asset_collection" in text_value
            or "CATEGORY_UNSUPPORTED" in text_value
            or "CATEGORY_AMBIGUOUS" in text_value
            or "category_resolution" in text_value
        ):
            related.append(event)
    dump_json(adir / "asset_category_events.json", related)

    found = []
    def walk(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            if "category_resolution" in value:
                found.append({"path": path + ".category_resolution", "value": value.get("category_resolution")})
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
    for name in ("messages", "events", "tasks"):
        walk(meta.get(name) or [], f"$.{name}")
    dump_json(adir / "category_resolution_structures.json", found)

    journal_matches = []
    for logfile in sorted((outdir / "journals").glob("*.log")) if (outdir / "journals").exists() else []:
        try:
            for lineno, line in enumerate(logfile.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if "asset_category_fallback_resolved" in line or "CATEGORY_UNSUPPORTED" in line or "CATEGORY_AMBIGUOUS" in line:
                    journal_matches.append({"file": logfile.name, "line": lineno, "text": line})
        except OSError:
            pass
    dump_json(adir / "category_resolution_journal_matches.json", journal_matches)


def make_summary(outdir: Path, meta: dict[str, Any], runtime_units: list[str]) -> None:
    tasks = meta.get("tasks") or []
    messages = meta.get("messages") or []
    events = meta.get("events") or []
    errors = [e for e in events if e.get("error_code") or str(e.get("status") or "").upper() in {"FAILED", "ERROR", "TIMEOUT"} or str(e.get("event_type") or "").endswith(".failed")]
    tool_events = [e for e in events if str(e.get("event_type") or "").startswith("tool.")]
    target_task = meta.get("target_task")
    lines = [
        "PHM 对话诊断采集包",
        "=" * 80,
        f"collector_version: {COLLECTOR_VERSION}",
        f"mode: {MODE}",
        f"target_id: {TARGET}",
        f"conversation_id: {meta.get('conversation_id')}",
        f"collected_at_utc: {now_utc().isoformat()}",
        f"task_count_in_bundle: {len(tasks)}",
        f"message_count_in_conversation: {len(messages)}",
        f"execution_event_count: {len(events)}",
        f"error_like_event_count: {len(errors)}",
        f"tool_event_count: {len(tool_events)}",
        f"systemd_units: {', '.join(runtime_units) if runtime_units else '<none>'}",
    ]
    if target_task:
        lines.extend([
            "",
            "TARGET TASK",
            "-" * 80,
            f"id: {target_task.get('id')}",
            f"status: {target_task.get('status')}",
            f"execution_mode: {target_task.get('execution_mode')}",
            f"operation: {target_task.get('operation')}",
            f"request_id: {target_task.get('request_id')}",
            f"user_message_id: {target_task.get('user_message_id')}",
            f"assistant_message_id: {target_task.get('assistant_message_id')}",
            f"error_code: {target_task.get('error_code')}",
            f"error_message: {target_task.get('error_message')}",
            f"created_at: {iso(target_task.get('created_at'))}",
            f"started_at: {iso(target_task.get('started_at'))}",
            f"completed_at: {iso(target_task.get('completed_at'))}",
        ])
    lines.extend(["", "TASKS", "-" * 80])
    for task in tasks:
        lines.append(
            f"{task.get('created_at')}  {task.get('id')}  status={task.get('status')} mode={task.get('execution_mode')} "
            f"request_id={task.get('request_id')} error={task.get('error_code') or '-'}"
        )
    lines.extend(["", "MESSAGES", "-" * 80])
    for m in messages:
        content = str(m.get("content") or "").replace("\r", " ").replace("\n", " ")
        if len(content) > 600:
            content = content[:600] + "…"
        lines.append(f"{m.get('created_at')}  {m.get('role')}  id={m.get('id')} status={m.get('status')} :: {content}")
    lines.extend(["", "ERROR-LIKE EVENTS", "-" * 80])
    for e in errors[:200]:
        lines.append(
            f"task={e.get('task_id')} seq={e.get('sequence_no')} type={e.get('event_type')} stage={e.get('stage')} "
            f"status={e.get('status')} code={e.get('error_code')} message={e.get('error_message')}"
        )
    dump_text(outdir / "SUMMARY.txt", "\n".join(lines) + "\n")


def write_bundle_manifest(outdir: Path) -> None:
    rows = []
    for path in sorted(outdir.rglob("*")):
        if path.is_file() and path.name != "BUNDLE_SHA256SUMS.txt":
            rows.append(f"{sha256_file(path)}  {path.relative_to(outdir).as_posix()}")
    dump_text(outdir / "BUNDLE_SHA256SUMS.txt", "\n".join(rows) + "\n")


async def main() -> None:
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    prefix = "task" if MODE == "task" else "conversation"
    workdir = EXPORT_ROOT / f"{prefix}_{safe_name(TARGET)}_{stamp}"
    archive = EXPORT_ROOT / f"phm_{prefix}_debug_{safe_name(TARGET)}_{stamp}.tar.gz"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    meta: dict[str, Any] | None = None
    units: list[str] = []
    try:
        meta = await collect_db(workdir)
    except Exception as exc:
        dump_text(workdir / "database" / "ERROR.txt", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        failures.append(f"database: {type(exc).__name__}: {exc}")

    if meta is not None:
        try:
            relevant_ids = [str(row.get("id")) for row in meta.get("tasks") or []]
            units = collect_runtime(workdir, relevant_ids)
        except Exception as exc:
            dump_text(workdir / "runtime" / "ERROR.txt", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            failures.append(f"runtime: {type(exc).__name__}: {exc}")
        try:
            await collect_redis(workdir, meta)
        except Exception as exc:
            failures.append(f"redis: {type(exc).__name__}: {exc}")
        try:
            await collect_checkpoints(workdir, meta)
        except Exception as exc:
            dump_text(workdir / "checkpoints" / "ERROR.txt", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            failures.append(f"checkpoints: {type(exc).__name__}: {exc}")
        try:
            collect_journals(workdir, meta, units)
        except Exception as exc:
            dump_text(workdir / "journals" / "ERROR.txt", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            failures.append(f"journals: {type(exc).__name__}: {exc}")
        try:
            collect_file_log_matches(workdir, meta)
        except Exception as exc:
            dump_text(workdir / "file_logs" / "ERROR.txt", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            failures.append(f"file_logs: {type(exc).__name__}: {exc}")
        try:
            collect_category_resolution_trace(workdir, meta)
        except Exception as exc:
            dump_text(workdir / "analysis" / "ERROR.txt", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            failures.append(f"category_resolution_trace: {type(exc).__name__}: {exc}")
        try:
            make_summary(workdir, meta, units)
        except Exception as exc:
            failures.append(f"summary: {type(exc).__name__}: {exc}")
    else:
        # Still include runtime/source information when the target id was not found.
        try:
            units = collect_runtime(workdir, [TARGET])
        except Exception as exc:
            failures.append(f"runtime: {type(exc).__name__}: {exc}")

    dump_json(workdir / "COLLECTION_STATUS.json", {"collector_version": COLLECTOR_VERSION, "failures": failures, "partial": bool(failures)})
    write_bundle_manifest(workdir)
    with tarfile.open(archive, "w:gz", compresslevel=6) as tf:
        tf.add(workdir, arcname=workdir.name)
    digest = sha256_file(archive)
    (archive.with_suffix(archive.suffix + ".sha256")).write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    shutil.rmtree(workdir, ignore_errors=True)

    print("=" * 80)
    print("PHM 诊断采集完成" if not failures else "PHM 诊断采集完成（存在部分采集失败，包内有 ERROR/STATUS 说明）")
    print(f"模式: {MODE}")
    print(f"目标: {TARGET}")
    print(f"输出: {archive}")
    print(f"SHA256: {digest}")
    if failures:
        print("部分失败:")
        for item in failures:
            print(f"  - {item}")
    print("把上述 .tar.gz 文件直接发给我即可。")


if __name__ == "__main__":
    asyncio.run(main())

PY
