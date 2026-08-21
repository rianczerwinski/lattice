"""Content-addressed local byte storage with conflict detection."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .canonical import sha256_bytes


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid SHA-256 digest")
        path = self.root / digest[:2] / digest
        if self.root.resolve() not in path.resolve().parents:
            raise ValueError("blob path escaped its root")
        return path

    def put(self, content: bytes) -> str:
        digest = sha256_bytes(content)
        path = self.path_for(digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            if path.read_bytes() != content:
                raise OSError(f"content-address conflict for {digest}")
            return digest
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".pending", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("blob write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise OSError(f"content-address conflict for {digest}")
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def get(self, digest: str) -> bytes:
        content = self.path_for(digest).read_bytes()
        if sha256_bytes(content) != digest:
            raise OSError(f"blob bytes do not match identity {digest}")
        return content
