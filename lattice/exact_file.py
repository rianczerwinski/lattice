"""Descriptor-bound, byte-bounded capture of externally mutable files."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


class ExactFileCaptureError(OSError):
    """A file could not be captured under its admitted byte identity."""


def capture_exact_file(path: Path, *, expected_sha256: str, expected_bytes: int, max_bytes: int) -> bytes:
    """Read one regular file without following swaps or exceeding its admitted size."""

    if isinstance(expected_bytes, bool) or expected_bytes < 0:
        raise ValueError("expected byte count must be a non-negative integer")
    if isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("capture byte limit must be a non-negative integer")
    if expected_bytes > max_bytes:
        raise ExactFileCaptureError(f"admitted file exceeds the {max_bytes}-byte capture limit")
    if not path.is_absolute():
        raise ExactFileCaptureError("captured file path must be absolute")

    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ExactFileCaptureError("captured path is not a regular file")
        if before.st_size != expected_bytes:
            raise ExactFileCaptureError("captured file size changed after admission")
        chunks: list[bytes] = []
        remaining = expected_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    except ExactFileCaptureError:
        raise
    except OSError as error:
        raise ExactFileCaptureError(f"captured file is unavailable: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise ExactFileCaptureError("captured file changed while being read")
    if len(content) != expected_bytes or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ExactFileCaptureError("captured file bytes changed after admission")
    return content
