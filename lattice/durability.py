"""Crash-consistent filesystem synchronization for canonical Lattice bytes."""

from __future__ import annotations

import fcntl
import ctypes
import errno
import os
import sys
from pathlib import Path


def sync_volume(path: Path) -> bool:
    """Flush filesystem data/metadata through hardware and wait on macOS.

    Apple's Libc sync_volume_np returns an errno value, not -1/errno.
    FULLSYNC=1 and WAIT=2 are public unistd.h flags. Unsupported/sandboxed
    callers use the per-file path; a real I/O failure is never downgraded.
    https://github.com/apple-oss-distributions/Libc/blob/main/gen/sync_volume_np.c
    """
    if sys.platform != "darwin":
        return False
    operation = ctypes.CDLL(None).sync_volume_np
    operation.argtypes = (ctypes.c_char_p, ctypes.c_int)
    operation.restype = ctypes.c_int
    error = operation(os.fsencode(path), 0x01 | 0x02)
    if error in {errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.ENOSYS}:
        return False
    if error:
        raise OSError(error, os.strerror(error), path)
    return True


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
