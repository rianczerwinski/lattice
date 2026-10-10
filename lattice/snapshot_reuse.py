"""Reuse a verified compressed SQLite representation without dropping history."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def reuse_database(source: Path, destination: Path, backup_root: Path, *, copy_file, source_identity):
    """The captured source and bounded decompression supply independent oracles."""
    source_sha, source_bytes = source_identity(source)
    for previous in sorted(backup_root.iterdir(), reverse=True):
        manifest_path = previous / 'snapshot.json'
        if previous.is_symlink() or not previous.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        try:
            descriptor = json.loads(manifest_path.read_text()).get('database')
            if not isinstance(descriptor, dict) or descriptor.get('path') != 'lattice.sqlite3.gz' or descriptor.get('encoding') != 'gzip':
                continue
            if (descriptor.get('sha256'), descriptor.get('byte_count')) != (source_sha, source_bytes):
                continue
            payload = previous / descriptor['path']
            if payload.is_symlink() or not payload.is_file():
                raise OSError('prior compressed database is absent or redirected')
            before = payload.stat()
            digest, count = hashlib.sha256(), 0
            with gzip.open(payload, 'rb') as stream:
                while block := stream.read(min(1024 * 1024, source_bytes - count + 1)):
                    count += len(block)
                    if count > source_bytes:
                        raise OSError('prior compressed database exceeds its logical identity')
                    digest.update(block)
            after = payload.stat()
            if (count, digest.hexdigest()) != (source_bytes, source_sha) or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError('prior compressed database differs from captured bytes')
            copy_file(payload, destination)
            if source_identity(source) != (source_sha, source_bytes):
                raise OSError('captured database changed during reuse')
            return dict(descriptor)
        except (OSError, ValueError, EOFError) as error:
            log.warning('snapshot representation cannot be reused from %s: %s', previous, error)
            if destination.exists():
                raise  # A copied candidate is owned by the enclosing snapshot.
    return None
