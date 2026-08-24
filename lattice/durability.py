"""Crash-consistent filesystem synchronization for canonical Lattice bytes."""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path


def sync_file(descriptor: int) -> None:
    """Flush through the drive cache on macOS, not merely through the kernel."""

    os.fsync(descriptor)
    if sys.platform == "darwin":
        fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_tree_directories(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        sync_directory(directory)
    sync_directory(root)
