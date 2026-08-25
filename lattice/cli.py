"""Operational CLI for the recoverable local Lattice service."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import uvicorn
import lattice as lattice_package
from .execution import installed_adapter_status

from .canonical import canonical_bytes, strict_json_loads
from .client import LatticeClient
from .contracts import ExecutionSubmissionV1, HistoricalExecutionImportV1
from .__about__ import __version__
from .config import LatticeConfig
from .diagnostics import assess_backup, assess_data_root, latest_backup
from .outport import OutportService, outport_executable, resolve_lattice_service
from .runtime_attestation import tree_inventory, validate_tree_set, verify_provider_attestation_freshness
from .security import ensure_cli_token
from .server import create_app
from .snapshot import create_snapshot, restore_snapshot


LAUNCHD_LABEL = "com.axisvi.lattice"
BACKUP_LAUNCHD_LABEL = "com.axisvi.lattice.backup"
BACKUP_FRESH_SECONDS = 36 * 60 * 60


def _version() -> str:
    return __version__


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


def _service_routes(service: OutportService, cli_token: str, *, timeout: float = 35.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        f"{service.loopback_url}/api/v1/routes/probe",
        data=b"{}",
        method="POST",
        headers={"Host": f"127.0.0.1:{service.port}", "Authorization": f"Bearer {cli_token}", "Content-Type": "application/json"},
    )
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
    except Exception as error:
        return {"name": name, "state": "fail", "detail": f"{type(error).__name__}: {error}"}


def _launchd_job_status(label: str, *, require_running: bool) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("launchd supervision is required only on macOS")
    target = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(["launchctl", "print", target], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{target} is not loaded: {detail}")
    output = result.stdout.decode("utf-8", errors="replace")
    running = "state = running" in output
    if require_running and not running:
        raise RuntimeError(f"{target} is loaded but not running")
    return {"label": label, "loaded": True, "running": running}


def _launchd_status() -> dict[str, Any]:
    return {
        "service": _launchd_job_status(LAUNCHD_LABEL, require_running=True),
        "backup": _launchd_job_status(BACKUP_LAUNCHD_LABEL, require_running=False),
    }


def _runtime_status() -> dict[str, Any]:
    executable = Path(sys.executable).resolve()
    module = Path(__file__).resolve()
    editable = "site-packages" not in module.parts
    if editable:
        raise RuntimeError(f"Lattice is running from an editable/source checkout: {module}")
    if "axis/chariots/loom" in str(executable) or "axis/chariots/loom" in str(module):
        raise RuntimeError("installed runtime still references the retired loom checkout")
    runtime_root = next((parent for parent in module.parents if parent.parent.name == "versions"), None)
    if runtime_root is None:
        raise RuntimeError(f"installed Lattice module is not inside a versioned runtime: {module}")
    manifest_path = runtime_root / "runtime.json"
    try:
        raw = manifest_path.read_bytes()
        manifest = strict_json_loads(raw)
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise RuntimeError(f"installed runtime manifest is unreadable: {manifest_path}") from error
    if not isinstance(manifest, dict) or canonical_bytes(manifest) != raw or set(manifest) != {"generated_object", "contract_version", "runtime_id", "python", "packages", "module_locations", "source_trees", "provider_attestations"}:
        raise RuntimeError("installed runtime manifest is not exact canonical LatticeInstalledRuntime/4.0 JSON")
    envelope = manifest.get("generated_object")
    required = {"contract_version", "role", "kind", "schema", "artifact_id", "generated_at", "producer", "subject", "sources"}
    if not isinstance(envelope, dict) or set(envelope) != required or envelope.get("contract_version") != 1 or envelope.get("role") != "canonical" or envelope.get("kind") != "lattice-installed-runtime" or envelope.get("schema") != {"name": "lattice-installed-runtime", "version": 4} or envelope.get("producer") != {"name": "lattice-installer", "version": "4.0.0"}:
        raise RuntimeError("installed runtime generated-object envelope is invalid")
    artifact_id = envelope.get("artifact_id")
    try:
        artifact_uuid = uuid.UUID(str(artifact_id).removeprefix("artifact:"))
    except ValueError as error:
        raise RuntimeError("installed runtime artifact identity is invalid") from error
    if artifact_id != f"artifact:{artifact_uuid}" or envelope.get("subject") != f"lattice:installed-runtime:{runtime_root.name}":
        raise RuntimeError("installed runtime artifact does not describe the selected generation")
    sources = envelope.get("sources")
    if not isinstance(sources, list) or len(sources) != 2:
        raise RuntimeError("installed runtime lacks exact source provenance")
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"ref", "revision", "sha256"} or not isinstance(source["ref"], str) or not source["ref"] or source["revision"] is None and source["sha256"] is None:
            raise RuntimeError("installed runtime carries a malformed source binding")
    locations = manifest.get("module_locations")
    actual_locations = {"lattice": str(Path(lattice_package.__file__).resolve())}
    if manifest.get("contract_version") != "LatticeInstalledRuntime/4.0" or manifest.get("runtime_id") != runtime_root.name or manifest.get("python") != str(executable) or locations != actual_locations:
        raise RuntimeError("installed runtime manifest disagrees with the executing environment")
    try:
        expected_trees = validate_tree_set(manifest.get("source_trees"))
    except ValueError as error:
        raise RuntimeError("installed runtime source-tree attestation is malformed") from error
    actual_trees = {"lattice": tree_inventory(Path(lattice_package.__file__).resolve().parent)}
    if actual_trees != expected_trees:
        raise RuntimeError("installed runtime application bytes disagree with its source-tree attestation")
    try:
        provider_attestations = verify_provider_attestation_freshness(manifest.get("provider_attestations"))
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return {"version": _version(), "runtime_id": runtime_root.name, "artifact_id": artifact_id, "executable": str(executable), "module": str(module), "editable": False, "provider_profiles": {item["provider_code"]: item["profile_id"] for item in provider_attestations}}


def _adapter_status(service: OutportService | None = None, cli_token: str | None = None) -> dict[str, Any]:
    if service is not None and cli_token is None:
        raise RuntimeError("service adapter probes require an operator capability")
    statuses = _service_routes(service, cli_token) if service is not None and cli_token is not None else installed_adapter_status(probe_versions=False)
    required = {item["provider_code"]: item for item in statuses if item["provider_code"] in {"anthropic", "openai"}}
    missing = [provider for provider in ("anthropic", "openai") if provider not in required or not required[provider]["qualified"] or required[provider].get("operational") is not True]
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
    if service is None and args.port is None:
        raise RuntimeError("standalone Lattice open requires --port")
    port = service.port if service is not None else args.port
    loopback_url = service.loopback_url if service is not None else f"http://127.0.0.1:{port}"
    public_url = service.url if service is not None else loopback_url
    cli_token = ensure_cli_token(config.operator_token)
    request = urllib.request.Request(
        f"{loopback_url}/api/session/bootstrap",
        data=b"{}",
        method="POST",
        headers={"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {cli_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Lattice browser bootstrap failed: {error}") from error
    bootstrap = payload.get("bootstrap_token") if isinstance(payload, dict) else None
    if not isinstance(bootstrap, str):
        raise RuntimeError("Lattice browser bootstrap returned no operator capability")
    import webbrowser
    browser_url = f"{public_url.rstrip('/')}/#operator-bootstrap={urllib.parse.quote(bootstrap, safe='')}"
    if not webbrowser.open(browser_url):
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


def command_invoke(args: argparse.Namespace) -> int:
    path = Path(args.request).expanduser()
    raw = path.read_bytes()
    try:
        payload = strict_json_loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"execution request is not exact JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("execution request must be a JSON object")
    result = LatticeClient.from_environment().submit(ExecutionSubmissionV1.model_validate(payload), wait=args.wait, timeout=args.timeout)
    _emit(result.model_dump(mode="json"), as_json=True)
    return 0 if result.state.value not in {"failed", "interrupted"} else 1


def command_execution_result(args: argparse.Namespace) -> int:
    result = LatticeClient.from_environment().result(args.external_project_key, args.operation_key)
    _emit(result.model_dump(mode="json"), as_json=True)
    return 0 if result.state.value not in {"failed", "interrupted"} else 1


def command_artifact(args: argparse.Namespace) -> int:
    _emit(LatticeClient.from_environment().artifact(args.artifact_id), as_json=True)
    return 0


def command_blob(args: argparse.Namespace) -> int:
    content = LatticeClient.from_environment().blob(args.content_sha256)
    sys.stdout.buffer.write(content)
    sys.stdout.buffer.flush()
    return 0


def command_routes(args: argparse.Namespace) -> int:
    _emit(LatticeClient.from_environment().routes(probe=args.probe), as_json=True)
    return 0


def command_import_history(args: argparse.Namespace) -> int:
    path = Path(args.request).expanduser()
    raw = path.read_bytes()
    try:
        payload = strict_json_loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"historical execution request is not exact JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("historical execution request must be a JSON object")
    result = LatticeClient.from_environment().import_history(HistoricalExecutionImportV1.model_validate(payload))
    _emit(result.model_dump(mode="json"), as_json=True)
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
        if health.get("startup_recovery_failures"):
            raise RuntimeError(f"Lattice startup left batch members undispatched: {health['startup_recovery_failures']}")
        if health.get("workflow_pack_failures"):
            raise RuntimeError(f"Lattice quarantined invalid workflow packs: {health['workflow_pack_failures']}")
        selected_runtime = _runtime_status()
        if health.get("runtime_id") != selected_runtime["runtime_id"]:
            raise RuntimeError(f"running Lattice uses runtime {health.get('runtime_id')!r} but the selected runtime is {selected_runtime['runtime_id']!r}")
        if health.get("version") != selected_runtime["version"]:
            raise RuntimeError(f"running Lattice reports version {health.get('version')!r} but the selected runtime contains {selected_runtime['version']!r}")
        return health

    checks = [
        _check("outport", outport_check),
        _check("launchd", _launchd_status),
        _check("service", health_check),
        _check("database-blobs-and-journals", lambda: _database_status(config)),
        _check("runtime", _runtime_status),
        _check("provider-adapters", lambda: _adapter_status(service_box.get("value") or _service(config), ensure_cli_token(config.operator_token))),
        _check("backup-and-recovery", lambda: _backup_status(config)),
    ]
    result = {"status": "pass" if all(item["state"] == "pass" for item in checks) else "fail", "checks": checks}
    _emit(result, as_json=args.json)
    return 0 if result["status"] == "pass" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="lattice", description="Operate the local Lattice conversational harness")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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

    invoke = subparsers.add_parser("invoke", help="submit one execution as a one-node Lattice graph")
    invoke.add_argument("--request", required=True, help="ExecutionSubmission/1.0 JSON file")
    invoke.add_argument("--wait", action="store_true", help="follow the Lattice event stream until terminal")
    invoke.add_argument("--timeout", type=float, default=1800.0)
    invoke.set_defaults(handler=command_invoke)

    result = subparsers.add_parser("result", help="read one external execution result")
    result.add_argument("--external-project-key", required=True)
    result.add_argument("--operation-key", required=True)
    result.set_defaults(handler=command_execution_result)

    artifact = subparsers.add_parser("artifact", help="read one content-addressed execution artifact")
    artifact.add_argument("artifact_id")
    artifact.set_defaults(handler=command_artifact)

    blob = subparsers.add_parser("blob", help="write exact content-addressed bytes to stdout")
    blob.add_argument("content_sha256")
    blob.set_defaults(handler=command_blob)

    routes = subparsers.add_parser("routes", help="read Lattice provider profiles and qualification state")
    routes.add_argument("--probe", action="store_true", help="run the bounded official-CLI capability probes")
    routes.set_defaults(handler=command_routes)

    import_history = subparsers.add_parser("import-history", help="register an immutable census of historical execution files")
    import_history.add_argument("--request", required=True, help="HistoricalExecutionImport/1.0 JSON file")
    import_history.set_defaults(handler=command_import_history)
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
