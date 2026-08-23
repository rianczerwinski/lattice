"""Content-addressed local byte storage with conflict detection."""

from __future__ import annotations

import hashlib
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

    def put_file(self, source: Path) -> tuple[str, int]:
        """Stream a stable regular file into the blob store without buffering it."""

        source = source.resolve(strict=True)
        before = source.stat()
        if not source.is_file() or source.is_symlink():
            raise ValueError("blob source must be a regular non-symlink file")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".stream.", suffix=".pending", dir=self.root)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_count = 0
        try:
            try:
                os.fchmod(descriptor, 0o600)
                with source.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                        byte_count += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("blob write made no progress")
                            view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or byte_count != after.st_size:
                raise OSError(f"blob source changed while being ingested: {source}")
            content_sha256 = digest.hexdigest()
            path = self.path_for(content_sha256)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.exists():
                if not self._files_equal(path, temporary):
                    raise OSError(f"content-address conflict for {content_sha256}")
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if not self._files_equal(path, temporary):
                        raise OSError(f"content-address conflict for {content_sha256}")
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return content_sha256, byte_count
        finally:
            temporary.unlink(missing_ok=True)

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
