"""Atomic checkpoints and portable continuation projections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .journal import RunJournal, journal_from_root, stamp, write_all, write_new_file, write_new_json


_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_SESSION_PATTERNS = (
    re.compile(r'"session_id"\s*:\s*"([0-9a-f-]{36})"', re.I),
    re.compile(r'"sessionId"\s*:\s*"([0-9a-f-]{36})"', re.I),
    re.compile(r"\bsession id:\s*([0-9a-f-]{36})\b", re.I),
    re.compile(r"\bCreated conversation\s+([0-9a-f-]{36})\b", re.I),
    re.compile(r'"conversation_id"\s*:\s*"([0-9a-f-]{36})"', re.I),
    re.compile(r'"thread_id"\s*:\s*"([0-9a-f-]{36})"', re.I),
    re.compile(r'"threadId"\s*:\s*"([0-9a-f-]{36})"', re.I),
)


def _lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path.resolve(), threading.Lock())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        write_all(handle.fileno(), content)
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _generated_object(
    *,
    role: str,
    kind: str,
    schema_name: str,
    subject: str,
    sources: list[dict[str, str | None]] | None = None,
    parent_artifact_id: str | None = None,
    binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "contract_version": 1,
        "role": role,
        "kind": kind,
        "schema": {"name": schema_name, "version": 1},
        "artifact_id": f"artifact:{uuid.uuid4()}",
        "generated_at": stamp(),
        "producer": {"name": "lattice", "version": version("lattice")},
        "subject": subject,
    }
    if sources:
        value["sources"] = sources
    if parent_artifact_id:
        value["parent_artifact_id"] = parent_artifact_id
    if binding:
        value["binding"] = binding
    return value


def bind_request(
    journal: RunJournal,
    *,
    prompt: str,
    system_prompt: str | None = None,
    response_format: str,
    resume_strategy: str,
    tool_policy: str = "unspecified",
    native_state_dir: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind immutable request bytes before a provider process starts."""
    prompt_bytes = prompt.encode("utf-8")
    write_new_file(journal.prompt, prompt_bytes)
    system_prompt_bytes = system_prompt.encode("utf-8") if system_prompt is not None else None
    if system_prompt_bytes is not None:
        write_new_file(journal.system_prompt, system_prompt_bytes)
    start = json.loads(journal.start.read_text(encoding="utf-8"))
    request = {
        "version": 1,
        "invocation_id": journal.root.name,
        "driver": start["driver"],
        "subject": start["subject"],
        "model": start["model"],
        "effort": start["effort"],
        "prompt": journal.prompt.name,
        "prompt_sha256": _sha256(prompt_bytes),
        "system_prompt": journal.system_prompt.name if system_prompt_bytes is not None else None,
        "system_prompt_sha256": _sha256(system_prompt_bytes) if system_prompt_bytes is not None else None,
        "response_format": response_format,
        "tool_policy": tool_policy,
        "resume_strategy": resume_strategy,
        "native_state_dir": str(native_state_dir) if native_state_dir else None,
        "metadata": metadata or {},
    }
    write_new_json(journal.request, request)
    checkpoint = {
        "generated_object": _generated_object(
            role="canonical",
            kind="model-run-checkpoint",
            schema_name="axis-model-run-checkpoint",
            subject=f"model-run:{journal.root.name}",
            sources=[
                {
                    "ref": str(journal.prompt),
                    "revision": None,
                    "sha256": request["prompt_sha256"],
                },
                *(
                    [
                        {
                            "ref": str(journal.system_prompt),
                            "revision": None,
                            "sha256": request["system_prompt_sha256"],
                        }
                    ]
                    if system_prompt_bytes is not None
                    else []
                ),
            ],
        ),
        "version": 1,
        "run_id": journal.root.name,
        "generation": 0,
        "state": "running",
        "request": journal.request.name,
        "request_sha256": _sha256(journal.request.read_bytes()),
        "native_session": {
            "strategy": resume_strategy,
            "id": None,
            "state_dir": request["native_state_dir"],
        },
        "observed": {
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "event_count": 0,
            "last_channel": None,
        },
        "observable_work": [],
        "terminal_status": None,
        "details": {},
    }
    _atomic_json(journal.checkpoint, checkpoint)
    _write_continuation(journal, checkpoint, [])
    return checkpoint


def load_checkpoint(path_or_root: Path | str) -> dict[str, Any]:
    path = Path(path_or_root)
    checkpoint_path = path if path.name == "checkpoint.json" else path / "checkpoint.json"
    value = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    request_path = checkpoint_path.parent / value["request"]
    if _sha256(request_path.read_bytes()) != value["request_sha256"]:
        raise ValueError(f"checkpoint request binding failed: {request_path}")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    prompt_path = checkpoint_path.parent / request["prompt"]
    if _sha256(prompt_path.read_bytes()) != request["prompt_sha256"]:
        raise ValueError(f"checkpoint prompt binding failed: {prompt_path}")
    system_prompt_name = request.get("system_prompt")
    system_prompt_sha256 = request.get("system_prompt_sha256")
    if system_prompt_name is not None:
        system_prompt_path = checkpoint_path.parent / system_prompt_name
        if not isinstance(system_prompt_sha256, str) or _sha256(system_prompt_path.read_bytes()) != system_prompt_sha256:
            raise ValueError(f"checkpoint system prompt binding failed: {system_prompt_path}")
    elif system_prompt_sha256 is not None:
        raise ValueError("checkpoint carries a system prompt digest without a file")
    return value


def _progress_event(journal: RunJournal, channel: str, chunk: bytes) -> dict[str, Any]:
    event = {
        "version": 1,
        "recorded_at": stamp(),
        "channel": channel,
        "bytes": len(chunk),
        "sha256": _sha256(chunk),
        "text": chunk.decode("utf-8", errors="replace"),
    }
    line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(journal.progress, os.O_APPEND | os.O_WRONLY)
    try:
        write_all(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return event


def _events(journal: RunJournal) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in journal.progress.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            result.append(event)
    return result


def _useful_event_lines(events: list[dict[str, Any]]) -> list[str]:
    useful: list[str] = []
    seen: set[str] = set()
    for event in events:
        text = event.get("text")
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            clean = line.strip()
            if not clean:
                continue
            rendered = clean
            try:
                payload = json.loads(clean)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                kind = payload.get("type") or payload.get("event") or payload.get("method")
                if kind in {"rate_limit_event", "system"}:
                    continue
                message = payload.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, list):
                        texts = [part.get("text") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
                        if texts:
                            rendered = " ".join(texts)
                tool = payload.get("tool_info") or payload.get("tool")
                if tool:
                    rendered = json.dumps({"event": kind, "tool": tool}, ensure_ascii=False)
            if rendered not in seen:
                seen.add(rendered)
                useful.append(rendered)
    return useful[-400:]


def _write_continuation(
    journal: RunJournal,
    checkpoint: dict[str, Any],
    observable_work: list[str],
) -> None:
    checkpoint_bytes = (json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    body = {
        "generated_object": _generated_object(
            role="derived-view",
            kind="model-run-continuation",
            schema_name="axis-model-run-continuation",
            subject=f"model-run:{journal.root.name}",
            binding={
                "artifact_id": checkpoint["generated_object"]["artifact_id"],
                "sha256": _sha256(checkpoint_bytes),
            },
        ),
        "version": 1,
        "run_id": journal.root.name,
        "native_session": checkpoint["native_session"],
        "observed": checkpoint["observed"],
        "observable_work": observable_work,
    }
    _atomic_json(journal.continuation, body)


def observe_chunk(root: Path, channel: str, chunk: bytes) -> None:
    journal = journal_from_root(root)
    if not journal.checkpoint.exists():
        return
    with _lock(journal.checkpoint):
        event = _progress_event(journal, channel, chunk)
        checkpoint = load_checkpoint(journal.root)
        previous_artifact = checkpoint["generated_object"]["artifact_id"]
        checkpoint["generated_object"] = _generated_object(
            role="canonical",
            kind="model-run-checkpoint",
            schema_name="axis-model-run-checkpoint",
            subject=f"model-run:{journal.root.name}",
            sources=checkpoint["generated_object"]["sources"],
            parent_artifact_id=previous_artifact,
        )
        checkpoint["generation"] += 1
        observed = checkpoint["observed"]
        byte_key = f"{channel}_bytes"
        observed[byte_key] = observed.get(byte_key, 0) + len(chunk)
        observed["event_count"] += 1
        observed["last_channel"] = channel
        prior_work = checkpoint.get("observable_work", [])
        if not isinstance(prior_work, list) or any(not isinstance(item, str) for item in prior_work):
            prior_work = _useful_event_lines(_events(journal)[:-1])
        for line in _useful_event_lines([event]):
            if line in prior_work:
                prior_work.remove(line)
            prior_work.append(line)
        checkpoint["observable_work"] = prior_work[-400:]
        session_id = _session_id(event["text"])
        if session_id and not checkpoint["native_session"].get("id"):
            checkpoint["native_session"]["id"] = session_id
        _atomic_json(journal.checkpoint, checkpoint)
        _write_continuation(journal, checkpoint, checkpoint["observable_work"])


def observe_sidecar(root: Path, name: str, chunk: bytes) -> None:
    observe_chunk(root, f"sidecar-{name}", chunk)


def _session_id(text: str) -> str | None:
    for pattern in _SESSION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def record_native_session(journal: RunJournal, session_id: str) -> dict[str, Any]:
    with _lock(journal.checkpoint):
        checkpoint = load_checkpoint(journal.root)
        previous_artifact = checkpoint["generated_object"]["artifact_id"]
        checkpoint["generated_object"] = _generated_object(
            role="canonical",
            kind="model-run-checkpoint",
            schema_name="axis-model-run-checkpoint",
            subject=f"model-run:{journal.root.name}",
            sources=checkpoint["generated_object"]["sources"],
            parent_artifact_id=previous_artifact,
        )
        checkpoint["generation"] += 1
        checkpoint["native_session"]["id"] = session_id
        observable_work = checkpoint.get("observable_work")
        if not isinstance(observable_work, list) or any(not isinstance(item, str) for item in observable_work):
            observable_work = _useful_event_lines(_events(journal))
            checkpoint["observable_work"] = observable_work
        _atomic_json(journal.checkpoint, checkpoint)
        _write_continuation(journal, checkpoint, observable_work)
        return checkpoint


def mark_checkpoint(
    journal: RunJournal,
    *,
    state: str,
    terminal_status: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _lock(journal.checkpoint):
        checkpoint = load_checkpoint(journal.root)
        previous_artifact = checkpoint["generated_object"]["artifact_id"]
        checkpoint["generated_object"] = _generated_object(
            role="canonical",
            kind="model-run-checkpoint",
            schema_name="axis-model-run-checkpoint",
            subject=f"model-run:{journal.root.name}",
            sources=checkpoint["generated_object"]["sources"],
            parent_artifact_id=previous_artifact,
        )
        checkpoint["generation"] += 1
        checkpoint["state"] = state
        checkpoint["terminal_status"] = terminal_status
        checkpoint["details"] = details or {}
        observable_work = checkpoint.get("observable_work")
        if not isinstance(observable_work, list) or any(not isinstance(item, str) for item in observable_work):
            observable_work = _useful_event_lines(_events(journal))
            checkpoint["observable_work"] = observable_work
        _atomic_json(journal.checkpoint, checkpoint)
        _write_continuation(journal, checkpoint, observable_work)
        return checkpoint


def continuation_prompt(path_or_root: Path | str, *, native: bool) -> str:
    root = Path(path_or_root)
    if root.name == "checkpoint.json":
        root = root.parent
    checkpoint = load_checkpoint(root)
    request = json.loads((root / checkpoint["request"]).read_text(encoding="utf-8"))
    prompt = (root / request["prompt"]).read_text(encoding="utf-8")
    continuation = json.loads((root / "continuation.json").read_text(encoding="utf-8"))
    work = continuation.get("observable_work") or []
    if native:
        return (
            "Continue the interrupted task from where the prior turn stopped. "
            "Preserve completed research and return only the originally requested final result."
        )
    rendered = "\n".join(f"- {line}" for line in work)
    return (
        prompt
        + "\n\n## Recovery context\n\n"
        + "A prior isolated session was interrupted. Continue the original task using the observable work below; do not restart completed work. "
        + "Return only the originally requested final result.\n\n"
        + (rendered if rendered else "No provider-visible intermediate events were emitted before interruption.")
    )
