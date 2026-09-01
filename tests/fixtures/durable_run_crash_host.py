#!/usr/bin/env python3
"""Lose the supervising process abruptly while its project worker remains live."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from lattice import LatticeConfig, LatticeStore
from lattice.run_supervisor import DurableRunSupervisor


async def _wait_for(path: Path, store: LatticeStore, run_id: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and store.get_run_unit(run_id, "two")["state"] == "running":
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"fixture worker did not reach {path.name}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--journal-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--worker-marker", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    args = parser.parse_args()

    store = LatticeStore(LatticeConfig(data_root=args.data_root, port=19011, axisvi_root=None))
    supervisor = DurableRunSupervisor(store, journal_root=args.journal_root)
    await supervisor.start()
    attempt = await supervisor.execute(args.run_id, args.idempotency_key)
    await _wait_for(args.worker_marker, store, args.run_id)
    identity = store.get_run_attempt(attempt["attempt_id"])["process_identity"]
    ready_bytes = json.dumps({"attempt_id": attempt["attempt_id"], "process_identity": identity}, sort_keys=True).encode("utf-8")
    temporary = args.ready_file.with_suffix(".next")
    with temporary.open("xb", buffering=0) as handle:
        handle.write(ready_bytes)
        os.fsync(handle.fileno())
    os.replace(temporary, args.ready_file)
    os._exit(91)


if __name__ == "__main__":
    asyncio.run(main())
