"""Permanent raw journals for substantive model invocations."""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime())


@dataclass(frozen=True)
class RunJournal:
    root: Path
    start: Path
    stdout: Path
    stderr: Path
    terminal: Path
    checkpoint: Path
    progress: Path
    continuation: Path
    request: Path
    prompt: Path
    provider_prompt: Path
    system_prompt: Path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("journal write made no progress")
        view = view[written:]


def write_new_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(path.parent)


def write_new_json(path: Path, value: dict[str, Any]) -> None:
    write_new_file(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def journal_from_root(root: Path) -> RunJournal:
    return RunJournal(
        root=root,
        start=root / "start.json",
        stdout=root / "stdout.log",
        stderr=root / "stderr.log",
        terminal=root / "terminal.json",
        checkpoint=root / "checkpoint.json",
        progress=root / "progress.jsonl",
        continuation=root / "continuation.json",
        request=root / "request.json",
        prompt=root / "prompt.txt",
        provider_prompt=root / "provider-prompt.txt",
        system_prompt=root / "system-prompt.txt",
    )


def create_run_journal(
    driver: str,
    *,
    subject: str,
    model: str,
    effort: str,
    timeout: int | float | None,
    attempt: int = 1,
    runs_dir: Path,
    invocation_id: str | None = None,
    parent_invocation_id: str | None = None,
) -> RunJournal:
    base_was_missing = not runs_dir.exists()
    runs_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runs_dir, 0o700)
    if base_was_missing:
        _sync_directory(runs_dir.parent)
    invocation_id = invocation_id or f"{stamp()}-{driver}-{secrets.token_hex(4)}"
    root = runs_dir / invocation_id
    root.mkdir(mode=0o700)
    _sync_directory(runs_dir)
    journal = journal_from_root(root)
    write_new_file(journal.stdout, b"")
    write_new_file(journal.stderr, b"")
    write_new_file(journal.progress, b"")
    write_new_json(
        journal.start,
        {
            "version": 2,
            "event": "start",
            "recorded_at": stamp(),
            "invocation_id": invocation_id,
            "parent_invocation_id": parent_invocation_id,
            "driver": driver,
            "subject": subject,
            "model": model,
            "effort": effort,
            "attempt": attempt,
            "timeout_seconds": timeout,
            "stdout": journal.stdout.name,
            "stderr": journal.stderr.name,
            "progress": journal.progress.name,
        },
    )
    print(f"model-journal[{driver}]: {journal.root}", file=sys.stderr)
    return journal


def journal_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def fsync_existing_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def finish_run_journal(journal: RunJournal, status: str, **details: Any) -> None:
    from .checkpoint import mark_checkpoint

    event = {
        **details,
        "version": 2,
        "event": "finish",
        "recorded_at": stamp(),
        "invocation_id": journal.root.name,
        "status": status,
        "stdout_bytes": journal.stdout.stat().st_size,
        "stderr_bytes": journal.stderr.stat().st_size,
    }
    write_new_json(journal.terminal, event)
    if journal.checkpoint.exists():
        state = "succeeded" if status == "success" else "resumable"
        mark_checkpoint(journal, state=state, terminal_status=status, details=details)
