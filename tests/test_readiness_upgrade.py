"""Independent negative controls for the exact unchanged-migration upgrade."""
from pathlib import Path
import sys
from contextlib import contextmanager

import pytest

OPS = Path(__file__).resolve().parents[1] / 'ops'
sys.path.insert(0, str(OPS))
import readiness_upgrade as upgrade


def fixture():
    old = {'runtime_id': upgrade.OLD_RUNTIME, 'packages': ['same-dependency==1'], 'python': {'version': 'same'}}
    new = {**old, 'runtime_id': upgrade.NEW_RUNTIME}
    before = {name: 'old' for name in upgrade.REVIEWED_CHANGES | {'store.py', 'conversation_schema.py'}}
    after = {name: 'new' if name in upgrade.REVIEWED_CHANGES else value for name, value in before.items()}
    return old, new, before, after


def validate(values, **kwargs):
    return upgrade.validate_equivalence(*values, kwargs.pop('old_entry', 'same-entry'), kwargs.pop('new_entry', 'same-entry'),
                                        saved_schema=kwargs.pop('saved_schema', 16), expected_schema=16, **kwargs)


def test_accepts_only_reviewed_module_changes():
    assert set(validate(fixture())) == upgrade.REVIEWED_CHANGES


@pytest.mark.parametrize('module', ['store.py', 'conversation_schema.py', 'new_migration.py'])
def test_rejects_added_or_changed_migration_code(module):
    values = fixture()
    values[3][module] = 'mutated SQL or schema implementation'
    with pytest.raises(RuntimeError, match='unreviewed'):
        validate(values)


def test_rejects_dependency_and_version_changes():
    values = fixture()
    values[1]['packages'] = ['different-dependency==2']
    with pytest.raises(RuntimeError, match='dependencies'):
        validate(values)
    values = fixture()
    values[1]['python'] = {'version': 'different'}
    with pytest.raises(RuntimeError, match='Python'):
        validate(values)
    values = fixture()
    values[1]['runtime_id'] = 'another-runtime'
    with pytest.raises(RuntimeError, match='only'):
        validate(values)


def test_rejects_changed_entry_and_noncurrent_schema():
    with pytest.raises(RuntimeError, match='entry or schema'):
        validate(fixture(), new_entry='now invokes a new migration')
    with pytest.raises(RuntimeError, match='entry or schema'):
        validate(fixture(), saved_schema=15)


def test_busy_upgrade_does_not_restore_or_reload_unrelated_work(tmp_path, monkeypatch):
    old = tmp_path / upgrade.OLD_RUNTIME
    old.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(old)
    monkeypatch.setattr(upgrade.install, 'CURRENT_LINK', current)
    monkeypatch.setattr(upgrade, 'prove', lambda *args: {})
    monkeypatch.setattr(upgrade.install, 'capture_publication_state', lambda: object())
    restored = []
    monkeypatch.setattr(upgrade.install, 'restore_publication_state', restored.append)
    @contextmanager
    def busy():
        raise RuntimeError('busy')
        yield
    monkeypatch.setattr(upgrade.install, 'upgrade_quiescence', busy)
    monkeypatch.setattr(sys, 'argv', ['readiness_upgrade', '--rollback-snapshot', str(tmp_path)])
    with pytest.raises(RuntimeError, match='busy'):
        upgrade.main()
    assert restored == []
