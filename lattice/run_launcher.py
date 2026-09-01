#!/usr/bin/env python3
"""Lattice-owned exec gate for trusted foreground process-group workers.

The guardian controls its direct target and the process group that target
inherits. Project work that calls setsid, double-forks, or reparents itself is
outside this adapter's lifecycle and recovery contract; this launcher is not a
hostile same-user process sandbox.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import stat
from pathlib import Path


MAX_TARGET_ENVIRONMENT_BYTES = 16 * 1024 * 1024


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("durable-run launcher descriptor accepted no bytes")
        offset += written


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_target_environment(path: Path, expected_sha256: str) -> dict[str, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_TARGET_ENVIRONMENT_BYTES:
            raise OSError("durable-run target environment is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise OSError("durable-run target environment changed while being read")
    if len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise OSError("durable-run target environment disagrees with its admitted identity")
    value = json.loads(raw)
    if _canonical_bytes(value) != raw or not isinstance(value, dict) or set(value) != {"contract_version", "environment"}:
        raise OSError("durable-run target environment is not exact canonical JSON")
    if value["contract_version"] != "LocalRunTargetEnvironment/1.0" or not isinstance(value["environment"], dict):
        raise OSError("durable-run target environment contract is unsupported")
    environment = value["environment"]
    if any(not isinstance(key, str) or not key or "=" in key or "\x00" in key or not isinstance(item, str) or "\x00" in item for key, item in environment.items()):
        raise OSError("durable-run target environment contains a process-unsafe literal")
    return environment


def _guardian_exit_for(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        signum = os.WTERMSIG(status)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        return 128 + signum
    return 114


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-fd", required=True, type=int)
    parser.add_argument("--ready-fd", required=True, type=int)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--lock-path", required=True, type=Path)
    parser.add_argument("--record-path", required=True, type=Path)
    parser.add_argument("--target-environment-path", required=True, type=Path)
    parser.add_argument("--target-environment-sha256", required=True)
    parser.add_argument("--launcher-environment-sha256", required=True)
    parser.add_argument("--worker-fd", action="append", default=[], type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not Path(command[0]).is_absolute():
        raise ValueError("durable-run launcher requires an absolute target command")
    if hashlib.sha256(_canonical_bytes(dict(os.environ))).hexdigest() != args.launcher_environment_sha256:
        raise OSError("durable-run launcher environment disagrees with its sanitized binding")

    lock_fd = os.open(args.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock_fd, True)
    _write_all(lock_fd, (args.nonce + "\n").encode("ascii"))
    os.fsync(lock_fd)

    identity = {
        "attempt_id": args.attempt_id,
        "contract_version": "RunProcessIdentity/2.0",
        "launcher_environment_sha256": args.launcher_environment_sha256,
        "lock_path": str(args.lock_path),
        "nonce": args.nonce,
        "pid": os.getpid(),
        "process_group_id": os.getpgrp(),
        "record_path": str(args.record_path),
        "run_id": args.run_id,
        "target_environment_path": str(args.target_environment_path),
        "target_environment_sha256": args.target_environment_sha256,
    }
    raw = _canonical_bytes(identity)
    with args.record_path.open("xb", buffering=0) as handle:
        os.chmod(args.record_path, 0o600)
        handle.write(raw)
        os.fsync(handle.fileno())
    _sync_directory(args.record_path.parent)
    _write_all(args.ready_fd, raw + b"\n")
    os.close(args.ready_fd)

    gate = os.read(args.gate_fd, 1)
    os.close(args.gate_fd)
    if gate != b"G":
        return 112
    target_environment = _read_target_environment(args.target_environment_path, args.target_environment_sha256)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda _signum, _frame: None)
    # Contract boundary: the target and all work it owns remain in this
    # guardian-owned foreground process group until the direct target exits.
    target_pid = os.fork()
    if target_pid == 0:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, signal.SIG_DFL)
        os.close(lock_fd)
        os.execve(command[0], command, target_environment)
        return 113
    for descriptor in args.worker_fd:
        try:
            os.close(descriptor)
        except OSError:
            pass
    while True:
        try:
            waited_pid, status = os.waitpid(target_pid, 0)
            if waited_pid == target_pid:
                return _guardian_exit_for(status)
        except InterruptedError:
            continue


if __name__ == "__main__":
    raise SystemExit(main())
