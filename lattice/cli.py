"""Operational CLI for the recoverable local Lattice service."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

import uvicorn
from axis_model_runner import installed_adapter_status

from .config import LatticeConfig
from .diagnostics import assess_backup, assess_data_root, latest_backup
from .outport import OutportService, outport_executable, resolve_lattice_service
from .server import create_app
from .snapshot import create_snapshot, restore_snapshot


LAUNCHD_LABEL = "com.axisvi.lattice"
BACKUP_FRESH_SECONDS = 36 * 60 * 60


def _version() -> str:
    try:
        return importlib.metadata.version("lattice")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0+source"


def _service(config: LatticeConfig) -> OutportService | None:
    return resolve_lattice_service(config.axisvi_root) if config.axisvi_root else None


def _effective_config(config: LatticeConfig, *, explicit_port: int | None) -> tuple[LatticeConfig, OutportService | None]:
    service = _service(config)
    if service is not None:
        if explicit_port is not None and explicit_port != service.port:
            raise RuntimeError(f"AXISVI Lattice must use Outport's :{service.port} allocation, not :{explicit_port}")
        return replace(config, port=service.port, public_url=service.url, frame_origins=service.frame_origins), service
    if explicit_port is None:
        raise RuntimeError("standalone Lattice requires --port because no Outport project identity is available")
    return replace(config, port=explicit_port, public_url=f"http://127.0.0.1:{explicit_port}"), None


def _health(service: OutportService, *, timeout: float = 2.0) -> dict[str, Any]:
    request = urllib.request.Request(f"{service.loopback_url}/api/health", headers={"Host": f"127.0.0.1:{service.port}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Lattice is not healthy on Outport :{service.port}: {error}") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("Lattice health response is not JSON") from error
    if not isinstance(payload, dict) or payload.get("status") != "ok" or payload.get("service") != "lattice":
        raise RuntimeError(f"Lattice health response is not healthy: {payload}")
    return payload


def _service_routes(service: OutportService, *, timeout: float = 2.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(f"{service.loopback_url}/api/v1/routes", headers={"Host": f"127.0.0.1:{service.port}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Lattice service adapter status is unreadable on Outport :{service.port}: {error}") from error
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RuntimeError("Lattice service adapter status is not a list of provider objects")
    return payload


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif isinstance(value, dict):
        for key, item in value.items():
            rendered = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
            print(f"{key}: {rendered}")
    else:
        print(value)


def _check(name: str, operation) -> dict[str, Any]:
    try:
        detail = operation()
        return {"name": name, "state": "pass", "detail": detail}
    except BaseException as error:
        return {"name": name, "state": "fail", "detail": f"{type(error).__name__}: {error}"}


def _launchd_status() -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("launchd supervision is required only on macOS")
    target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    result = subprocess.run(["launchctl", "print", target], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{target} is not loaded: {detail}")
    output = result.stdout.decode("utf-8", errors="replace")
    return {"label": LAUNCHD_LABEL, "loaded": True, "running": "state = running" in output}


def _runtime_status() -> dict[str, Any]:
    executable = Path(sys.executable).resolve()
    module = Path(__file__).resolve()
    editable = "site-packages" not in module.parts
    if editable:
        raise RuntimeError(f"Lattice is running from an editable/source checkout: {module}")
    if "axis/chariots/loom" in str(executable) or "axis/chariots/loom" in str(module):
        raise RuntimeError("installed runtime still references the retired loom checkout")
    return {"version": _version(), "executable": str(executable), "module": str(module), "editable": False}


def _adapter_status(service: OutportService | None = None) -> dict[str, Any]:
    statuses = _service_routes(service) if service is not None else installed_adapter_status(probe_versions=False)
    required = {item["provider_code"]: item for item in statuses if item["provider_code"] in {"anthropic", "openai"}}
    missing = [provider for provider in ("anthropic", "openai") if provider not in required or not required[provider]["qualified"]]
    profile_gaps = [provider for provider, item in required.items() if not {"compiled-fresh", "workspace-agent", "native-continuation"}.issubset(set(item.get("qualified_profiles", [])))]
    if missing or profile_gaps:
        raise RuntimeError(f"required adapters are not qualified: unavailable={missing}, profile_gaps={profile_gaps}")
    return {provider: {"model": item["model_code"], "cli": item["official_cli"], "profiles": item["qualified_profiles"]} for provider, item in sorted(required.items())}


def _backup_status(config: LatticeConfig) -> dict[str, Any]:
    snapshot = latest_backup(config.backups)
    if snapshot is None:
        raise RuntimeError("no verified Lattice backup exists")
    report = assess_backup(snapshot)
    generated = time.mktime(time.strptime(report["generated_at"], "%Y%m%dT%H%M%S"))
    age_seconds = max(0, int(time.time() - generated))
    if age_seconds > BACKUP_FRESH_SECONDS:
        raise RuntimeError(f"latest verified backup is stale by policy: {age_seconds} seconds old")
    return {**report, "age_seconds": age_seconds}


def _database_status(config: LatticeConfig) -> dict[str, Any]:
    report = assess_data_root(config.data_root)
    if report["schema_version"] != report["expected_schema_version"]:
        raise RuntimeError(f"Lattice schema generation is {report['schema_version']}, expected {report['expected_schema_version']}")
    return report


def command_serve(args: argparse.Namespace) -> int:
    config, _service_identity = _effective_config(LatticeConfig.from_environment(), explicit_port=args.port)
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level=args.log_level, access_log=True)
    return 0


def command_open(args: argparse.Namespace) -> int:
    config = LatticeConfig.from_environment()
    service = _service(config)
    if service is not None:
        result = subprocess.run([outport_executable(), "open", "lattice"], cwd=config.axisvi_root, check=False)
        return result.returncode
    if args.port is None:
        raise RuntimeError("standalone Lattice open requires --port")
    import webbrowser
    if not webbrowser.open(f"http://127.0.0.1:{args.port}"):
        raise RuntimeError("the system browser did not accept the Lattice URL")
    return 0


def command_status(args: argparse.Namespace) -> int:
    config = LatticeConfig.from_environment()
    service = _service(config)
    if service is None:
        if args.port is None:
            raise RuntimeError("standalone Lattice status requires --port")
        service = OutportService(project="standalone", instance="standalone", name="lattice", port=args.port, hostname="127.0.0.1", url=f"http://127.0.0.1:{args.port}", up=False)
    try:
        health = _health(service)
        running = True
    except RuntimeError as error:
        health = {"error": str(error)}
        running = False
    result = {"identity": service.__dict__, "running": running, "health": health, "data_root": str(config.data_root), "version": _version()}
    _emit(result, as_json=args.json)
    return 0 if running else 1


def command_backup(args: argparse.Namespace) -> int:
    config = LatticeConfig.from_environment()
    report = assess_data_root(config.data_root)
    snapshot = create_snapshot(
        config.data_root,
        backup_root=Path(args.to).expanduser() if args.to else None,
        reason=args.reason,
        source_revision=report["state_digest"],
        schema_version=report["schema_version"],
        state_digest=report["state_digest"],
        require_idle=True,
    )
    verified = assess_backup(snapshot)
    _emit({"snapshot": str(snapshot), "artifact_id": verified["artifact_id"], "state_digest": verified["state_digest"]}, as_json=args.json)
    return 0


def command_restore(args: argparse.Namespace) -> int:
    config = LatticeConfig.from_environment()
    snapshot = Path(args.snapshot).expanduser() if args.snapshot else latest_backup(config.backups)
    if snapshot is None:
        raise RuntimeError("no Lattice snapshot is available to restore")
    destination = Path(args.to).expanduser()
    manifest = restore_snapshot(snapshot, destination)
    report = assess_data_root(destination)
    if manifest.get("state_digest") is not None and report["state_digest"] != manifest["state_digest"]:
        raise OSError("restored Lattice state does not match its snapshot")
    _emit({"destination": str(destination.resolve()), "source_snapshot": str(snapshot.resolve()), "artifact_id": manifest["generated_object"]["artifact_id"], "state_digest": report["state_digest"]}, as_json=args.json)
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    config = LatticeConfig.from_environment()
    service_box: dict[str, OutportService] = {}

    def outport_check() -> dict[str, Any]:
        service = _service(config)
        if service is None:
            raise RuntimeError("doctor requires an AXISVI Outport identity")
        service_box["value"] = service
        return service.__dict__

    def health_check() -> dict[str, Any]:
        service = service_box.get("value") or _service(config)
        if service is None:
            raise RuntimeError("no Outport service identity")
        health = _health(service)
        if health.get("port") != service.port:
            raise RuntimeError(f"running Lattice reports :{health.get('port')} but Outport owns :{service.port}")
        if Path(str(health.get("data_root", ""))).resolve() != config.data_root.resolve():
            raise RuntimeError("running Lattice uses a different canonical data root")
        return health

    checks = [
        _check("outport", outport_check),
        _check("launchd", _launchd_status),
        _check("service", health_check),
        _check("database-blobs-and-journals", lambda: _database_status(config)),
        _check("runtime", _runtime_status),
        _check("provider-adapters", lambda: _adapter_status(service_box.get("value") or _service(config))),
        _check("backup-and-recovery", lambda: _backup_status(config)),
    ]
    result = {"status": "pass" if all(item["state"] == "pass" for item in checks) else "fail", "checks": checks}
    _emit(result, as_json=args.json)
    return 0 if result["status"] == "pass" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="lattice", description="Operate the local Lattice conversational harness")
    subparsers = root.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the single-origin Lattice service")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level", default="info", choices=("critical", "error", "warning", "info", "debug", "trace"))
    serve.set_defaults(handler=command_serve)

    open_command = subparsers.add_parser("open", help="open Lattice through its Outport identity")
    open_command.add_argument("--port", type=int)
    open_command.set_defaults(handler=command_open)

    status = subparsers.add_parser("status", help="show Outport identity and live health")
    status.add_argument("--port", type=int)
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    doctor = subparsers.add_parser("doctor", help="verify service, state, runtime, providers, and recovery")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    backup = subparsers.add_parser("backup", help="create and verify a consistent Lattice snapshot")
    backup.add_argument("--to", help="backup directory; defaults to the canonical data root")
    backup.add_argument("--reason", default="operator-request")
    backup.add_argument("--json", action="store_true")
    backup.set_defaults(handler=command_backup)

    restore = subparsers.add_parser("restore", help="restore a verified snapshot into a new directory")
    restore.add_argument("snapshot", nargs="?", help="snapshot directory; defaults to the newest canonical backup")
    restore.add_argument("--to", required=True, help="new, nonexistent destination directory")
    restore.add_argument("--json", action="store_true")
    restore.set_defaults(handler=command_restore)
    return root


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        status = args.handler(args)
    except (FileNotFoundError, FileExistsError, OSError, RuntimeError, ValueError) as error:
        print(f"lattice: {error}", file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    main()
