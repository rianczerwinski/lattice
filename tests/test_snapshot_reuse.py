"""Exact gzip reuse and rejection of forged, oversized, or redirected history."""
import gzip
import hashlib
import json

import pytest
from lattice.snapshot import _compress_database


@pytest.mark.parametrize('mutation', [None, 'forged', 'oversized', 'redirected'])
def test_prior_identity_is_verified_against_exact_captured_source(tmp_path, mutation, monkeypatch, caplog):
    source = tmp_path / 'captured.sqlite3'
    source.write_bytes(b'captured source oracle' * 1024)
    backups = tmp_path / 'backups'
    prior = backups / 'previous'
    prior.mkdir(parents=True)
    payload = prior / 'lattice.sqlite3.gz'
    descriptor = _compress_database(source, payload)
    (prior / 'snapshot.json').write_text(json.dumps({'database': descriptor}))
    if mutation == 'forged':
        payload.write_bytes(gzip.compress(b'wrong source bytes'))
    elif mutation == 'oversized':
        payload.write_bytes(gzip.compress(source.read_bytes() + b'oversized'))
    elif mutation == 'redirected':
        payload.unlink()
        payload.symlink_to(source)
    staging = backups / '.staging'
    staging.mkdir()
    candidate = staging / 'lattice.sqlite3.gz'
    copied = []
    from lattice import snapshot
    native = snapshot._copy_regular
    def copy(a,b):
        copied.append(a)
        native(a,b)
    monkeypatch.setattr(snapshot, '_copy_regular', copy)
    result = _compress_database(source, candidate, reuse_root=backups)
    assert gzip.decompress(candidate.read_bytes()) == source.read_bytes()
    assert result['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert bool(copied) == (mutation is None)
    if mutation is not None:
        assert 'cannot be reused' in caplog.text
