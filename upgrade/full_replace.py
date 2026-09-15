#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from urllib.request import ProxyHandler, build_opener

BUNDLE = Path(__file__).resolve().parents[1]
SOURCE = BUNDLE / "source"
SUPPORT = BUNDLE / "support"
MANIFEST = BUNDLE / "upgrade" / "full_replace_manifest.json"
VERSION = "phm-1.7.0-issue1-global-collection-hotfix-v1-20260915"

COMPONENTS = (
    "conversation_management",
    "phm_assert_mcp",
    "phm_data_mcp",
    "phm_feature_mcp",
    "phm_diagnosis_mcp",
)
PRESERVE = {".env", ".venv"}
SUPPORT_FILES = ("collect_task_trace.sh", "collect_conversation_trace.sh", "verify_evidence_fabric.sh")

EXACT_SERVICES = {
    "conversation-api.service",
    "phm-asset-mcp.service",
    "phm-data-mcp.service",
    "phm-feature-mcp.service",
    "phm-diagnosis-mcp.service",
}
WORKER_RE = re.compile(r"^conversation-worker@[^\s]+\.service$")


class DeployError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args: list[str], *, cwd: Path | None = None, timeout: int = 90, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        cmd = Path(args[0]).name
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f"：{detail[-1][:500]}" if detail else ""
        raise DeployError(f"命令失败 {cmd}，退出码 {result.returncode}{suffix}")
    return result


def verify_bundle() -> dict:
    if not MANIFEST.is_file():
        raise DeployError(f"缺少部署清单：{MANIFEST}")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION:
        raise DeployError("部署清单版本不匹配")
    expected_components = set(COMPONENTS)
    if set(manifest.get("components") or {}) != expected_components:
        raise DeployError("部署清单组件不完整")
    for component in COMPONENTS:
        base = SOURCE / component
        if not base.is_dir():
            raise DeployError(f"部署包缺少组件：{component}")
        for item in manifest["components"][component]["files"]:
            rel = Path(item["path"])
            if rel.is_absolute() or ".." in rel.parts:
                raise DeployError("部署清单包含非法路径")
            path = base / rel
            if not path.is_file():
                raise DeployError(f"部署包缺少文件：{component}/{rel}")
            if sha256_file(path) != item["sha256"]:
                raise DeployError(f"部署包完整性失败：{component}/{rel}")
        actual = {
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts and p.suffix not in {".pyc", ".pyo"}
        }
        expected = {item["path"] for item in manifest["components"][component]["files"]}
        if actual != expected:
            extra = sorted(actual - expected)[:10]
            missing = sorted(expected - actual)[:10]
            raise DeployError(f"部署包文件集合与清单不一致：{component} extra={extra} missing={missing}")
    support_manifest = manifest.get("support_files") or {}
    if set(support_manifest) != set(SUPPORT_FILES):
        raise DeployError("部署清单诊断采集脚本不完整")
    for name in SUPPORT_FILES:
        path = SUPPORT / name
        item = support_manifest[name]
        if not path.is_file() or sha256_file(path) != item.get("sha256"):
            raise DeployError(f"部署包诊断采集脚本完整性失败：{name}")
    return manifest


def target_roots(root: Path) -> dict[str, Path]:
    return {name: (root / name).resolve() for name in COMPONENTS}


def validate_targets(root: Path, service_user: str) -> dict[str, Path]:
    try:
        pwd.getpwnam(service_user)
    except KeyError as exc:
        raise DeployError(f"服务用户不存在：{service_user}") from exc
    roots = target_roots(root)
    for component, path in roots.items():
        if not path.is_dir() or path.is_symlink():
            raise DeployError(f"目标代码目录不存在或不是普通目录：{path}")
        env = path / ".env"
        venv_python = path / ".venv" / "bin" / "python"
        if not env.is_file():
            raise DeployError(f"为避免覆盖运行配置，要求现网存在：{env}")
        if not venv_python.is_file():
            raise DeployError(f"为避免重建依赖环境，要求现网存在：{venv_python}")
    return roots


def active_known_services() -> list[str]:
    result = run(["systemctl", "list-units", "--type=service", "--state=active", "--no-legend", "--plain"], check=False)
    if result.returncode != 0:
        raise DeployError("无法读取 systemd 活动服务")
    services: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        unit = fields[0]
        if unit in EXACT_SERVICES or WORKER_RE.match(unit):
            services.append(unit)
    return sorted(set(services))


def installed_service_status() -> dict[str, str]:
    units = sorted(EXACT_SERVICES)
    result: dict[str, str] = {}
    for unit in units:
        probe = run(["systemctl", "status", unit, "--no-pager"], check=False, timeout=20)
        if probe.returncode == 4:
            result[unit] = "not-installed"
        else:
            active = run(["systemctl", "is-active", unit], check=False, timeout=20).stdout.strip()
            result[unit] = active or "installed"
    return result


def stop_services(services: list[str]) -> None:
    order = []
    if "conversation-api.service" in services:
        order.append("conversation-api.service")
    order.extend(sorted(s for s in services if WORKER_RE.match(s)))
    for unit in ("phm-diagnosis-mcp.service", "phm-feature-mcp.service", "phm-data-mcp.service", "phm-asset-mcp.service"):
        if unit in services:
            order.append(unit)
    for unit in order:
        run(["systemctl", "stop", unit], timeout=120)


def start_services(services: list[str]) -> None:
    order: list[str] = []
    for unit in ("phm-asset-mcp.service", "phm-data-mcp.service", "phm-feature-mcp.service", "phm-diagnosis-mcp.service"):
        if unit in services:
            order.append(unit)
    order.extend(sorted(s for s in services if WORKER_RE.match(s)))
    if "conversation-api.service" in services:
        order.append("conversation-api.service")
    for unit in order:
        run(["systemctl", "start", unit], timeout=120)


def backup_component(target: Path, archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as tf:
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if child.name in PRESERVE:
                continue
            tf.add(child, arcname=child.name, recursive=True)


def safe_extract(archive: Path, target: Path) -> None:
    target_real = target.resolve()
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            member_path = (target / member.name).resolve()
            if target_real != member_path and target_real not in member_path.parents:
                raise DeployError(f"备份包含非法路径：{member.name}")
        tf.extractall(target, filter="fully_trusted")


def clean_code(target: Path) -> None:
    for child in list(target.iterdir()):
        if child.name in PRESERVE:
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)


def copy_source(component: str, target: Path) -> None:
    src = SOURCE / component
    for child in sorted(src.iterdir(), key=lambda p: p.name):
        dst = target / child.name
        if child.is_dir():
            shutil.copytree(child, dst, symlinks=True, copy_function=shutil.copy2)
        elif child.is_symlink():
            os.symlink(os.readlink(child), dst)
        else:
            shutil.copy2(child, dst)


def chown_new_code(target: Path, service_user: str) -> None:
    info = pwd.getpwnam(service_user)
    for child in target.iterdir():
        if child.name in PRESERVE:
            continue
        if child.is_symlink():
            os.lchown(child, info.pw_uid, info.pw_gid)
            continue
        if child.is_dir():
            for base, dirs, files in os.walk(child):
                os.chown(base, info.pw_uid, info.pw_gid)
                for name in dirs:
                    os.chown(os.path.join(base, name), info.pw_uid, info.pw_gid)
                for name in files:
                    os.chown(os.path.join(base, name), info.pw_uid, info.pw_gid)
        else:
            os.chown(child, info.pw_uid, info.pw_gid)


def alembic_revision(root: Path) -> str | None:
    result = run([python_of(root), "-m", "alembic", "current"], cwd=root, timeout=90)
    for line in result.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        token = raw.split()[0].strip()
        if re.fullmatch(r"[0-9A-Za-z_]+", token):
            return token
    return None


def alembic_head(root: Path) -> str:
    result = run([python_of(root), "-m", "alembic", "heads"], cwd=root, timeout=60)
    heads = []
    for line in result.stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        token = raw.split()[0].strip()
        if re.fullmatch(r"[0-9A-Za-z_]+", token):
            heads.append(token)
    if len(heads) != 1:
        raise DeployError(f"Alembic 必须只有一个 head，实际={heads}")
    return heads[0]


def migrate_conversation_database(root: Path) -> tuple[str | None, str]:
    before = alembic_revision(root)
    head = alembic_head(root)
    run([python_of(root), "-m", "alembic", "upgrade", "head"], cwd=root, timeout=180)
    after = alembic_revision(root)
    if after != head:
        raise DeployError(f"数据库迁移后 revision 不正确：expected={head} actual={after}")
    return before, after


def downgrade_conversation_database(root: Path, revision: str | None) -> None:
    if not revision:
        return
    current = alembic_revision(root)
    if current == revision:
        return
    run([python_of(root), "-m", "alembic", "downgrade", revision], cwd=root, timeout=180)
    after = alembic_revision(root)
    if after != revision:
        raise DeployError(f"数据库回滚 revision 不正确：expected={revision} actual={after}")


def create_backup(root: Path, roots: dict[str, Path], active_services: list[str]) -> tuple[Path, dict]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "phm_upgrade_backups" / f"full_replace_20260912_{stamp}_{os.getpid()}"
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    metadata = {
        "version": VERSION,
        "created_at": stamp,
        "root": str(root),
        "components": {},
        "active_services": active_services,
        "preserved": sorted(PRESERVE),
        "conversation_database_revision": alembic_revision(roots["conversation_management"]),
    }
    for component, target in roots.items():
        archive = backup / f"{component}.tar.gz"
        backup_component(target, archive)
        metadata["components"][component] = {
            "target": str(target),
            "archive": archive.name,
            "archive_sha256": sha256_file(archive),
        }
    support_archive = backup / "root_support_files.tar.gz"
    existing_support = []
    with tarfile.open(support_archive, "w:gz") as tf:
        for name in SUPPORT_FILES:
            target = root / name
            if target.exists() or target.is_symlink():
                tf.add(target, arcname=name, recursive=False)
                existing_support.append(name)
    metadata["support_files"] = {
        "archive": support_archive.name,
        "archive_sha256": sha256_file(support_archive),
        "existing": existing_support,
    }
    (backup / "rollback.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(backup / "rollback.json", 0o600)
    return backup, metadata


def restore_backup(backup: Path, metadata: dict, service_user: str) -> None:
    for component in COMPONENTS:
        item = metadata["components"].get(component)
        if not item:
            raise DeployError(f"备份缺少组件：{component}")
        target = Path(item["target"])
        archive = backup / item["archive"]
        if not archive.is_file() or sha256_file(archive) != item["archive_sha256"]:
            raise DeployError(f"回滚备份校验失败：{component}")
        clean_code(target)
        safe_extract(archive, target)
        chown_new_code(target, service_user)
    support = metadata.get("support_files") or {}
    support_archive = backup / str(support.get("archive") or "")
    if not support_archive.is_file() or sha256_file(support_archive) != support.get("archive_sha256"):
        raise DeployError("回滚备份中的诊断采集脚本校验失败")
    for name in SUPPORT_FILES:
        target = Path(metadata["root"]) / name
        if target.exists() or target.is_symlink():
            target.unlink()
    safe_extract(support_archive, Path(metadata["root"]))


def python_of(root: Path) -> str:
    return str(root / ".venv" / "bin" / "python")


def verify_runtime_imports(roots: dict[str, Path]) -> None:
    checks = {
        "conversation_management": "from app.config import get_settings; get_settings(); import app.orchestration.completion; import app.orchestration.nodes",
        "phm_assert_mcp": "from app.config import get_settings; get_settings(); import app.mcp.server; from app.services.sensor_query_service import SensorQueryService",
        "phm_data_mcp": "from app.config import get_settings; get_settings(); import app.mcp_server",
        "phm_feature_mcp": "from app.config import get_settings; get_settings(); import app.server",
        "phm_diagnosis_mcp": "from app.config import get_settings; get_settings(); import app.mcp_server",
    }
    for component, code in checks.items():
        root = roots[component]
        run([python_of(root), "-c", code], cwd=root, timeout=90)
    for name in SUPPORT_FILES:
        run(["bash", "-n", str(SUPPORT / name)], timeout=30)


def get_int_setting(root: Path, expr: str) -> int:
    result = run([python_of(root), "-c", f"from app.config import get_settings; print({expr})"], cwd=root, timeout=30)
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise DeployError(f"无法读取端口：{root.name}") from exc


def http_json_ready(url: str, validator, unit: str, timeout_seconds: int = 90) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=4) as response:
                payload = json.load(response)
                if response.status == 200 and validator(payload):
                    return
                last = json.dumps(payload, ensure_ascii=False)[:500]
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise DeployError(f"{unit} 就绪检查未通过；最后状态：{last}")


def tcp_ready(host: str, port: int, unit: str, timeout_seconds: int = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except OSError as exc:
            last = str(exc)
            time.sleep(2)
    raise DeployError(f"{unit} 端口 {port} 未就绪；最后状态：{last}")


def readiness(services: list[str], roots: dict[str, Path]) -> None:
    if "phm-asset-mcp.service" in services:
        port = get_int_setting(roots["phm_assert_mcp"], "get_settings().port")
        http_json_ready(f"http://127.0.0.1:{port}/ready", lambda p: p.get("core_ready") is True, "phm-asset-mcp.service")
    if "phm-data-mcp.service" in services:
        port = get_int_setting(roots["phm_data_mcp"], "get_settings().port")
        http_json_ready(f"http://127.0.0.1:{port}/ready", lambda p: p.get("status") == "ready", "phm-data-mcp.service")
    if "phm-feature-mcp.service" in services:
        port = get_int_setting(roots["phm_feature_mcp"], "get_settings().port")
        tcp_ready("127.0.0.1", port, "phm-feature-mcp.service")
    if "phm-diagnosis-mcp.service" in services:
        port = get_int_setting(roots["phm_diagnosis_mcp"], "get_settings().port")
        http_json_ready(f"http://127.0.0.1:{port}/ready", lambda p: p.get("status") == "ready", "phm-diagnosis-mcp.service")
    if "conversation-api.service" in services:
        port = get_int_setting(roots["conversation_management"], "get_settings().app_port")
        http_json_ready(f"http://127.0.0.1:{port}/ready", lambda p: p.get("status") == "ready", "conversation-api.service")
    for unit in services:
        result = run(["systemctl", "is-active", "--quiet", unit], check=False, timeout=20)
        if result.returncode != 0:
            raise DeployError(f"服务未保持 active：{unit}")


def replace_all(roots: dict[str, Path], service_user: str) -> None:
    for component, target in roots.items():
        clean_code(target)
        copy_source(component, target)
        chown_new_code(target, service_user)


def install_support(root: Path) -> None:
    for name in SUPPORT_FILES:
        src = SUPPORT / name
        dst = root / name
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)


def print_plan(root: Path, roots: dict[str, Path], manifest: dict, service_user: str, no_services: bool) -> None:
    print(f"版本：{VERSION}")
    print("部署模式：全量代码替换（不是增量补丁）")
    print("现网五个代码目录中除 .env 与 .venv 外的内容都会先备份，然后全部清理并换成本部署包版本。")
    print("保留：.env、.venv、systemd unit；不合并任何现网源码。")
    print("数据库：自动执行 conversation_management Alembic upgrade head；备份记录部署前 revision，回滚时同步 downgrade。")
    print(f"服务用户：{service_user}")
    total = 0
    for component in COMPONENTS:
        count = manifest["components"][component]["file_count"]
        total += count
        print(f"  - {roots[component]}  <=  package/source/{component}  ({count} files)")
    print(f"包内代码文件总数：{total}")
    print("根目录诊断脚本将同步更新：")
    for name in SUPPORT_FILES:
        print(f"  - {root / name}")
    if not no_services:
        statuses = installed_service_status()
        print("服务状态：")
        for unit, status in statuses.items():
            print(f"  - {unit}: {status}")
        workers = [s for s in active_known_services() if WORKER_RE.match(s)]
        for unit in workers:
            print(f"  - {unit}: active")


def load_backup(path: Path) -> dict:
    meta_path = path / "rollback.json"
    if not meta_path.is_file():
        raise DeployError(f"找不到回滚元数据：{meta_path}")
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if metadata.get("version") != VERSION:
        raise DeployError("该备份不是本全量部署器创建的备份")
    if set((metadata.get("components") or {}).keys()) != set(COMPONENTS):
        raise DeployError("回滚备份组件不完整")
    if not isinstance(metadata.get("support_files"), dict):
        raise DeployError("回滚备份缺少根目录诊断脚本信息")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="PHM 1.7.0 Evidence Fabric v1 full code replacement deployer")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="只读检查（默认）")
    modes.add_argument("--apply", action="store_true", help="备份现网全部代码后全量替换")
    modes.add_argument("--rollback", type=Path, help="使用全量部署备份恢复部署前代码")
    parser.add_argument("--root", type=Path, default=Path("/home/chaos/program"))
    parser.add_argument("--service-user", default="jy1234")
    parser.add_argument("--no-services", action="store_true", help="只替换/回滚代码，不操作 systemd/就绪检查")
    args = parser.parse_args()

    root = args.root.resolve()
    verify_bundle()
    roots = validate_targets(root, args.service_user)

    if args.rollback:
        metadata = load_backup(args.rollback.resolve())
        for component in COMPONENTS:
            if Path(metadata["components"][component]["target"]).resolve() != roots[component]:
                raise DeployError("回滚备份的安装根目录与当前 --root 不一致")
        current_active = [] if args.no_services else active_known_services()
        if not args.no_services:
            stop_services(current_active)
        try:
            previous_revision = metadata.get("conversation_database_revision")
            downgrade_conversation_database(roots["conversation_management"], previous_revision)
            restore_backup(args.rollback.resolve(), metadata, args.service_user)
            verify_runtime_imports(roots)
            if not args.no_services:
                original_active = metadata.get("active_services") or []
                start_services(original_active)
                readiness(original_active, roots)
        except BaseException:
            if not args.no_services:
                # Best effort to return currently active services; do not mask original failure.
                try:
                    start_services(current_active)
                except Exception:
                    pass
            raise
        print("回滚完成：五个组件的代码已恢复到全量部署前版本；.env/.venv 未改动。")
        return

    manifest = verify_bundle()
    print_plan(root, roots, manifest, args.service_user, args.no_services)
    if not args.apply:
        print("只读检查完成。确认这是你要丢弃的现网代码后，再使用 --apply。")
        return

    lock_path = root / "phm-full-replace.lock"
    root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeployError("已有另一套 PHM 全量部署正在执行") from exc

        active = [] if args.no_services else active_known_services()
        if not args.no_services:
            stop_services(active)
        backup = None
        metadata = None
        try:
            backup, metadata = create_backup(root, roots, active)
            print(f"全量代码回滚备份：{backup}", flush=True)
            replace_all(roots, args.service_user)
            install_support(root)
            verify_runtime_imports(roots)
            _, migrated_revision = migrate_conversation_database(roots["conversation_management"])
            print(f"Conversation PostgreSQL migration: {metadata.get('conversation_database_revision')} -> {migrated_revision}", flush=True)
            if not args.no_services:
                start_services(active)
                readiness(active, roots)
        except BaseException as exc:
            print(f"部署失败，开始自动恢复部署前全部代码：{exc}", file=sys.stderr, flush=True)
            if not args.no_services:
                try:
                    now_active = active_known_services()
                    stop_services(now_active)
                except Exception:
                    pass
            if backup is not None and metadata is not None:
                try:
                    downgrade_conversation_database(
                        roots["conversation_management"],
                        metadata.get("conversation_database_revision"),
                    )
                except Exception as migration_rollback_exc:
                    print(f"警告：数据库自动回滚失败：{migration_rollback_exc}", file=sys.stderr, flush=True)
                    raise
                restore_backup(backup, metadata, args.service_user)
            if not args.no_services:
                try:
                    start_services(active)
                    readiness(active, roots)
                    print("自动回滚完成，原活动服务已恢复。", file=sys.stderr)
                except Exception as rollback_exc:
                    print(f"警告：代码已回滚，但服务恢复检查失败：{rollback_exc}", file=sys.stderr)
            raise

        print("全量部署完成：五个组件的旧源码已丢弃并全部替换为本包版本；.env/.venv 保留；根目录诊断采集脚本已更新。")
        print(f"如需恢复部署前代码：sudo bash ./deploy.sh --root {root} --service-user {args.service_user} --rollback {backup}")


if __name__ == "__main__":
    try:
        main()
    except (DeployError, OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        print(f"部署未完成：{exc}", file=sys.stderr)
        sys.exit(1)
