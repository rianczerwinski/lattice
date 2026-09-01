#!/usr/bin/env python3
"""Independent three-unit worker used as the durable-run acceptance oracle."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

from lattice.canonical import canonical_bytes
from lattice.contracts import RunArtifactClass
from lattice.run_sdk import DurableRunChannelClosed, DurableRunSDK


UNITS = ("one", "two", "three")


def _close_non_protocol_descriptors() -> None:
    keep = {int(os.environ["LATTICE_RUN_EVENT_FD"]), int(os.environ["LATTICE_RUN_CONTROL_FD"])}
    descriptor_root = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
    descriptors = [int(item.name) for item in descriptor_root.iterdir() if item.name.isdigit()]
    for descriptor in descriptors:
        if descriptor > 2 and descriptor not in keep:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_once(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"durable fixture output changed across attempts: {path.name}")
        return
    with path.open("xb", buffering=0) as handle:
        handle.write(content)
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _write_state(path: Path, value: dict[str, object]) -> bytes:
    content = canonical_bytes(value)
    temporary = path.with_suffix(".next")
    with temporary.open("xb", buffering=0) as handle:
        handle.write(content)
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _sync_directory(path.parent)
    return content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--block-unit", choices=UNITS)
    parser.add_argument("--block-phase", choices=("before-work", "after-checkpoint"), default="before-work")
    parser.add_argument("--release-file", type=Path)
    parser.add_argument("--behavior", choices=("normal", "repeat-completed-one", "close-non-protocol-fds"), default="normal")
    args = parser.parse_args()
    if args.behavior == "close-non-protocol-fds":
        _close_non_protocol_descriptors()
    args.work_root.mkdir(parents=True, exist_ok=True)
    try:
        with DurableRunSDK.from_environment() as sdk:
            state_path = args.work_root / "state.json"
            if state_path.exists():
                state = json.loads(state_path.read_bytes())
                completed = list(state["completed"])
            else:
                completed = []
            selected = list(UNITS) if args.behavior == "repeat-completed-one" else list(sdk.remaining_unit_keys)
            if sdk.resume_checkpoint is not None:
                checkpoint = sdk.resume_checkpoint
                checkpoint.read_verified(max_bytes=1024 * 1024)
                _write_once(args.work_root / f"resumed-from-{checkpoint.artifact_id}.txt", b"verified\n")
            for unit in selected:
                sdk.unit_started(unit)
                sdk.progress(f"processing {unit}", completed=len(completed), total=len(UNITS), safe_to_stop=False)
                lock_handle = (args.work_root / f"active-{unit}.lock").open("a+b", buffering=0)
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _write_once(args.work_root / f"concurrent-{unit}.txt", b"duplicate\n")
                    sdk.run_failed(failure_kind="concurrent_duplicate_unit", error_text=f"unit {unit} already has a live worker")
                    return 74
                if unit == args.block_unit and args.block_phase == "before-work":
                    reached = args.work_root / f"reached-{unit}"
                    _write_once(reached, b"reached\n")
                    if args.release_file is None:
                        raise ValueError("blocked fixture unit requires --release-file")
                    while not args.release_file.exists():
                        time.sleep(0.02)
                result_bytes = f"deterministic-result:{unit}\n".encode("utf-8")
                result_path = args.work_root / f"result-{unit}.txt"
                _write_once(result_path, result_bytes)
                result = sdk.file_artifact(
                    result_path,
                    artifact_key=f"result-{unit}",
                    name=f"Result for {unit}",
                    media_type="text/plain",
                    artifact_class=RunArtifactClass.durable_evidence,
                    unit_key=unit,
                )
                if unit not in completed:
                    completed.append(unit)
                    completed.sort(key=UNITS.index)
                state_bytes = _write_state(state_path, {"completed": completed})
                checkpoint_path = args.work_root / f"checkpoint-{unit}.json"
                _write_once(checkpoint_path, state_bytes)
                sdk.file_artifact(
                    checkpoint_path,
                    artifact_key=f"checkpoint-{unit}",
                    name=f"Checkpoint after {unit}",
                    media_type="application/json",
                    artifact_class=RunArtifactClass.checkpoint,
                    unit_key=unit,
                )
                if unit == args.block_unit and args.block_phase == "after-checkpoint":
                    reached = args.work_root / f"reached-{unit}-after-checkpoint"
                    _write_once(reached, b"reached\n")
                    if args.release_file is None:
                        raise ValueError("blocked fixture unit requires --release-file")
                    while not args.release_file.exists():
                        time.sleep(0.02)
                sdk.unit_completed(unit, output_sha256=result.content_sha256)
                lock_handle.close()
                if sdk.stop_requested():
                    print(f"stopped after {unit}", flush=True)
                    return 0
                sdk.resource({"completed_units": len(completed), "fixture_pid": os.getpid()})
                print(f"completed {unit}", flush=True)
            sdk.run_completed()
    except DurableRunChannelClosed:
        return 75
    expected = hashlib.sha256(b"".join(f"deterministic-result:{unit}\n".encode("utf-8") for unit in UNITS)).hexdigest()
    print(f"aggregate={expected}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
