"""Content-addressed local byte storage with conflict detection."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from .canonical import sha256_bytes
from .durability import sync_directory, sync_file


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        if created:
            sync_directory(self.root.parent)
        sync_directory(self.root)

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
        shard_created = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shard_created:
            sync_directory(self.root)
        if path.exists():
            if path.read_bytes() != content:
                raise OSError(f"content-address conflict for {digest}")
            return digest
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".pending", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(content)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("blob write made no progress")
                    view = view[written:]
                sync_file(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise OSError(f"content-address conflict for {digest}")
            sync_directory(path.parent)
        finally:
            existed = temporary.exists()
            temporary.unlink(missing_ok=True)
            if existed:
                sync_directory(temporary.parent)
        return digest

    @staticmethod
    def _files_equal(left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    def put_file(
        self,
        source: Path,
        *,
        expected_sha256: str | None = None,
        expected_bytes: int | None = None,
    ) -> tuple[str, int]:
        """Stream one descriptor-bound regular file into CAS without buffering it."""

        if (expected_sha256 is None) != (expected_bytes is None):
            raise ValueError("expected file identity requires both SHA-256 and byte count")
        if expected_sha256 is not None:
            self.path_for(expected_sha256)
            if isinstance(expected_bytes, bool) or expected_bytes is None or expected_bytes < 0:
                raise ValueError("expected file byte count must be a non-negative integer")
        if source.is_symlink():
            raise ValueError("blob source must be a regular non-symlink file")

        source_descriptor = -1
        destination_descriptor = -1
        temporary: Path | None = None
        digest = hashlib.sha256()
        byte_count = 0
        try:
            source_descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            before = os.fstat(source_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("blob source must be a regular non-symlink file")
            if expected_bytes is not None and before.st_size != expected_bytes:
                raise OSError("blob source byte count disagrees with its admitted identity")
            destination_descriptor, temporary_name = tempfile.mkstemp(prefix=".stream.", suffix=".pending", dir=self.root)
            temporary = Path(temporary_name)
            try:
                os.fchmod(destination_descriptor, 0o600)
                remaining = None if expected_bytes is None else expected_bytes + 1
                while remaining is None or remaining > 0:
                    requested = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
                    chunk = os.read(source_descriptor, requested)
                    if not chunk:
                        break
                    digest.update(chunk)
                    byte_count += len(chunk)
                    if remaining is not None:
                        remaining -= len(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_descriptor, view)
                        if written <= 0:
                            raise OSError("blob write made no progress")
                        view = view[written:]
                sync_file(destination_descriptor)
            finally:
                os.close(destination_descriptor)
                destination_descriptor = -1
            after = os.fstat(source_descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or byte_count != after.st_size:
                raise OSError(f"blob source changed while being ingested: {source}")
            content_sha256 = digest.hexdigest()
            if expected_sha256 is not None and (content_sha256 != expected_sha256 or byte_count != expected_bytes):
                raise OSError("blob source bytes disagree with their admitted identity")
            path = self.path_for(content_sha256)
            shard_created = not path.parent.exists()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if shard_created:
                sync_directory(self.root)
            if path.exists():
                if not self._files_equal(path, temporary):
                    raise OSError(f"content-address conflict for {content_sha256}")
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if not self._files_equal(path, temporary):
                        raise OSError(f"content-address conflict for {content_sha256}")
                sync_directory(path.parent)
            return content_sha256, byte_count
        finally:
            if destination_descriptor >= 0:
                os.close(destination_descriptor)
            if source_descriptor >= 0:
                os.close(source_descriptor)
            if temporary is not None:
                existed = temporary.exists()
                temporary.unlink(missing_ok=True)
                if existed:
                    sync_directory(temporary.parent)

    def verify_file(self, digest: str) -> tuple[Path, int]:
        """Verify one CAS object by streaming it through SHA-256 under a stable stat."""

        source = self.path_for(digest)
        try:
            before = source.stat()
        except FileNotFoundError as error:
            raise OSError(f"missing blob object: {digest}") from error
        if not source.is_file() or source.is_symlink():
            raise OSError(f"blob object is not a regular non-symlink file: {digest}")
        computed = hashlib.sha256()
        byte_count = 0
        try:
            with source.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    computed.update(chunk)
                    byte_count += len(chunk)
            after = source.stat()
        except FileNotFoundError as error:
            raise OSError(f"missing blob object: {digest}") from error
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or byte_count != after.st_size:
            raise OSError(f"blob object changed while being verified: {digest}")
        if computed.hexdigest() != digest:
            raise OSError(f"blob bytes do not match identity {digest}")
        return source, byte_count

    def get(self, digest: str) -> bytes:
        content = self.path_for(digest).read_bytes()
        if sha256_bytes(content) != digest:
            raise OSError(f"blob bytes do not match identity {digest}")
        return content
