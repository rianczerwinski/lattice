"""Diagnostics must verify the change ledger under one SQLite snapshot.

Defect under test. ``assess_data_root`` opened a read-only connection and never
issued ``BEGIN``. Under WAL every statement then takes its own snapshot, so inside
``LatticeStore._verify_current_views_against_change_ledger`` the ledger scan and the
``SELECT COUNT(*)`` that follows it can observe different committed states. One
context block committed by a legitimate writer in that window produced the live
doctor failure ``Lattice current view diverges from its append-only change ledger:
context_blocks`` on 2026-10-21 while no divergence existed.

Repair under test. A deferred ``BEGIN`` before the first read pins one snapshot for
the whole SQLite phase and is rolled back before close. This is the store's own
pattern for reads that must not mix snapshots (``LatticeStore.get_invocation`` and
``LatticeStore._project_invocation_activity`` in ``lattice/store.py``). The pin is
released before the filesystem phase (blob and model-run journal hashing), which
can take minutes on a live root and must not hold the WAL checkpoint mark; the
run counts are therefore read inside the snapshot, ahead of that phase. Criteria,
messages and checks are unchanged.

How the race is reproduced truthfully. The concurrent writer is the production
``LatticeStore.create_context_block`` path on its own connection, committing a real
row and its ledger entry through the real triggers. The only test apparatus is a
scheduling hook: a thin proxy around the read-only connection that lets that write
commit immediately before the verifier's final count statement, which is the
window a real writer hits nondeterministically. Nothing about the data is faked.

Planted divergences use the store's own write path. A real view divergence needs
the ledger triggers suppressed, which Lattice does only on the rebuild staging
target by registering ``lattice_replaying`` as 1. The planted writes below take the
store's native connection (``LatticeStore.transaction``: native function
registration, write lock, ``BEGIN IMMEDIATE``, commit) and override only that one
function, exactly as the live negative controls in
``tests/test_execution_consolidation_verifier.py`` and
``tests/test_browser_qualification.py`` do. No raw connection is assembled by hand.

Mutation controls are permanent and hash-free. The defect is reproduced by
removing the snapshot pin from the loaded candidate (``_begin_read_snapshot``
replaced by a no-op) under the identical committed concurrent write, and the
guard is exercised by a connection that swallows ``BEGIN``. Neither depends on the
pre-repair bytes being present. Before/after proof against the retained defective
bytes is a subject-revision run of the same race test (point
``LATTICE_DIAGNOSTICS_CANDIDATE`` at the baseline file, expect the divergence
failure; point it at the candidate, expect a pass) and is kept as run evidence,
not as a test that would skip forever once the repair lands.

Module loading. The candidate is loaded from a declared path so the parent can run
these checks before any live application byte changes. Inside the Lattice checkout
the default path is the package module itself.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator

import pytest

from lattice import LatticeConfig, LatticeStore
from lattice.contracts import ContextBlockCreate, ProjectCreate
from lattice.store import CHANGE_TABLES


CANDIDATE_PATH = Path(os.environ.get("LATTICE_DIAGNOSTICS_CANDIDATE") or Path(__file__).parents[1] / "lattice" / "diagnostics.py")
# Exact statement the verifier issues after the per-table ledger scan (lattice/store.py, final count in the table loop).
COUNT_CONTEXT_BLOCKS = 'SELECT COUNT(*) FROM "context_blocks"'
DIVERGENCE = "diverges from its append-only change ledger: context_blocks"


def _load_diagnostics(path: Path, name: str) -> ModuleType:
    """Load a diagnostics module from an explicit path as a member of the lattice package.

    The dotted name makes ``__package__`` resolve to ``lattice`` so the module's own
    relative imports bind to the importable package; the loaded module is never
    registered in ``sys.modules`` and never shadows ``lattice.diagnostics``.
    """

    spec = importlib.util.spec_from_file_location(f"lattice.{name}", path.resolve(strict=True))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InterleavingConnection:
    """Forward every call to the real connection; run one real commit right before a chosen statement.

    This is a scheduler, not a fake. The write it releases is the store's own
    transaction on a separate connection. ``fired`` and ``in_transaction_at_fire``
    let tests prove the write happened and under which read-transaction state.
    """

    def __init__(self, connection: sqlite3.Connection, *, before_sql: str, write: Callable[[], Any]) -> None:
        self._connection = connection
        self._before_sql = before_sql
        self._write = write
        self.fired = 0
        self.in_transaction_at_fire: bool | None = None

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if self.fired == 0 and sql.lstrip().startswith(self._before_sql):
            self.fired += 1
            self.in_transaction_at_fire = bool(self._connection.in_transaction)
            self._write()
        return self._connection.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


@pytest.fixture
def ledger_env(tmp_path: Path) -> dict[str, Any]:
    config = LatticeConfig(data_root=tmp_path / "data", port=9010, axisvi_root=None)
    store = LatticeStore(config)
    project = store.create_project(ProjectCreate(title="Snapshot regression", purpose="Diagnostics read-consistency oracle"))
    seeded = [
        store.create_context_block(ContextBlockCreate(project_id=project["project_id"], semantic_type="note", content=f"seed {index}", authority="test-oracle", evidence_level="planted"))
        for index in range(3)
    ]
    counter = {"writes": 0}

    def commit_block() -> dict[str, Any]:
        counter["writes"] += 1
        return store.create_context_block(ContextBlockCreate(project_id=project["project_id"], semantic_type="note", content=f"concurrent {counter['writes']}", authority="test-oracle", evidence_level="planted"))

    return {"config": config, "store": store, "project": project, "seeded": seeded, "database": config.data_root / "lattice.sqlite3", "commit_block": commit_block, "counter": counter}


def _block_count(database: Path) -> int:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM context_blocks").fetchone()[0])
    finally:
        connection.close()


def _tracked(connection: sqlite3.Connection) -> tuple[str, ...]:
    tables = tuple(sorted(str(row[0]) for row in connection.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ledger_%_insert'")))
    assert set(tables) == set(CHANGE_TABLES)
    return tables


def _read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database.resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _divergence_writer(store: LatticeStore) -> Iterator[sqlite3.Connection]:
    """The store's native write transaction with only its ledger triggers suppressed.

    ``LatticeStore.transaction`` opens the connection through ``_open_connection``
    (native registration of ``lattice_stamp``, ``lattice_replaying`` and
    ``lattice_integrity_hash`` plus the historical aliases and pragmas), takes the
    write lock, issues ``BEGIN IMMEDIATE`` and commits on exit. Overriding
    ``lattice_replaying`` to 1 on that connection mirrors the rebuild staging target
    (``LatticeStore.rebuild_database``), the only place Lattice does so, and is the
    same override the live negative controls use. Applying it to a live file is
    exactly the manual SQL a real view divergence requires.
    """

    with store.transaction() as connection:
        connection.create_function("lattice_replaying", 0, lambda: 1)
        yield connection


def _ledger_rows(database: Path, *, where: str, parameters: tuple[Any, ...] = ()) -> int:
    connection = _read_only(database)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM ledger_changes WHERE table_name='context_blocks' AND {where}", parameters).fetchone()[0])
    finally:
        connection.close()


def _assess_consistent(candidate: ModuleType, root: Path) -> dict[str, Any]:
    """A later, unraced assessment must pass: proves the data was consistent all along."""

    return candidate.assess_data_root(root)


def _patch_connection_factory(monkeypatch: pytest.MonkeyPatch, module: ModuleType, write: Callable[[], Any]) -> list[InterleavingConnection]:
    created: list[InterleavingConnection] = []
    real = module._connect_read_only

    def wrapped(database: Path) -> InterleavingConnection:
        proxy = InterleavingConnection(real(database), before_sql=COUNT_CONTEXT_BLOCKS, write=write)
        created.append(proxy)
        return proxy

    monkeypatch.setattr(module, "_connect_read_only", wrapped)
    return created


# ---------------------------------------------------------------------------
# Mechanism, independent of the diagnostics module: the verifier itself is
# correct, the read-transaction discipline of its caller decides the outcome.
# ---------------------------------------------------------------------------


@pytest.mark.concurrency
def test_verifier_without_a_pinned_snapshot_false_fails_on_a_committed_concurrent_insert(ledger_env) -> None:
    before = _block_count(ledger_env["database"])
    connection = _read_only(ledger_env["database"])
    try:
        proxy = InterleavingConnection(connection, before_sql=COUNT_CONTEXT_BLOCKS, write=ledger_env["commit_block"])
        with pytest.raises(OSError, match=DIVERGENCE):
            LatticeStore._verify_current_views_against_change_ledger(proxy, _tracked(connection))
    finally:
        connection.close()
    assert proxy.fired == 1
    assert proxy.in_transaction_at_fire is False
    assert _block_count(ledger_env["database"]) == before + 1

    # The data is consistent; only the mixed-snapshot read was wrong.
    fresh = _read_only(ledger_env["database"])
    try:
        LatticeStore._verify_current_views_against_change_ledger(fresh, _tracked(fresh))
    finally:
        fresh.close()


@pytest.mark.concurrency
def test_verifier_under_one_deferred_transaction_ignores_the_committed_concurrent_insert(ledger_env) -> None:
    before = _block_count(ledger_env["database"])
    connection = _read_only(ledger_env["database"])
    try:
        connection.execute("BEGIN")
        proxy = InterleavingConnection(connection, before_sql=COUNT_CONTEXT_BLOCKS, write=ledger_env["commit_block"])
        LatticeStore._verify_current_views_against_change_ledger(proxy, _tracked(connection))
        pinned = int(connection.execute("SELECT COUNT(*) FROM context_blocks").fetchone()[0])
        connection.rollback()
    finally:
        connection.close()
    assert proxy.fired == 1
    assert proxy.in_transaction_at_fire is True
    assert pinned == before, "the pinned snapshot must not see the concurrent commit"
    assert _block_count(ledger_env["database"]) == before + 1, "the writer was never blocked and its commit is durable"


# ---------------------------------------------------------------------------
# Candidate diagnostics module loaded from its declared path.
# ---------------------------------------------------------------------------


@pytest.fixture
def candidate() -> ModuleType:
    return _load_diagnostics(CANDIDATE_PATH, "_diagnostics_candidate")


@pytest.mark.concurrency
def test_candidate_assess_data_root_holds_one_snapshot_across_a_genuinely_committed_concurrent_write(ledger_env, candidate, monkeypatch) -> None:
    root = ledger_env["config"].data_root
    baseline = candidate.assess_data_root(root)
    before = _block_count(ledger_env["database"])

    proxies = _patch_connection_factory(monkeypatch, candidate, ledger_env["commit_block"])
    report = candidate.assess_data_root(root)

    assert [proxy.fired for proxy in proxies] == [1]
    assert proxies[0].in_transaction_at_fire is True
    assert report["state_digest"] == baseline["state_digest"], "the concurrent commit must be invisible to the pinned snapshot"
    assert report["referenced_blob_count"] == baseline["referenced_blob_count"]
    assert _block_count(ledger_env["database"]) == before + 1, "the concurrent write really committed"

    monkeypatch.undo()
    after = candidate.assess_data_root(root)
    assert after["state_digest"] != baseline["state_digest"], "a later assessment sees the committed row and still verifies"
    assert after["referenced_blob_count"] == baseline["referenced_blob_count"] + 1


@pytest.mark.regression
def test_candidate_releases_its_read_transaction_before_closing(ledger_env, candidate, monkeypatch) -> None:
    observed: dict[str, Any] = {}
    real = candidate._connect_read_only

    def recording(database: Path) -> sqlite3.Connection:
        connection = real(database)
        original_close = connection.close

        def close() -> None:
            observed["in_transaction_at_close"] = connection.in_transaction
            original_close()

        # sqlite3.Connection attributes are read-only; wrap through a proxy instead.
        proxy = InterleavingConnection(connection, before_sql="\0never", write=lambda: None)
        proxy.close = close  # type: ignore[method-assign]
        return proxy

    monkeypatch.setattr(candidate, "_connect_read_only", recording)
    candidate.assess_data_root(ledger_env["config"].data_root)
    assert observed == {"in_transaction_at_close": False}


@pytest.mark.regression
def test_candidate_releases_the_snapshot_before_hashing_blobs_and_journals(ledger_env, candidate, monkeypatch) -> None:
    """Transaction lifetime: the pin covers the SQLite phase only.

    Blob and model-run journal hashing is filesystem work that takes minutes on a
    live root. Holding the read snapshot across it would hold the WAL checkpoint
    mark for that whole time, so the connection must be rolled back and closed
    before the first blob is hashed, and the resulting counts must still be
    reported from the pinned snapshot.
    """

    events: list[str] = []
    real_connect = candidate._connect_read_only
    real_verify_blob = candidate._verify_blob
    real_model_runs = candidate._assess_model_runs

    def recording(database: Path) -> InterleavingConnection:
        connection = real_connect(database)
        proxy = InterleavingConnection(connection, before_sql="\0never", write=lambda: None)
        original_close = connection.close

        def close() -> None:
            events.append("closed" if not connection.in_transaction else "closed-in-transaction")
            original_close()

        proxy.close = close  # type: ignore[method-assign]
        return proxy

    def verify_blob(path: Path, expected: str) -> int:
        if "blob" not in events:
            events.append("blob")
        return real_verify_blob(path, expected)

    def model_runs(runs_root: Path) -> dict[str, int]:
        events.append("model-runs")
        return real_model_runs(runs_root)

    monkeypatch.setattr(candidate, "_connect_read_only", recording)
    monkeypatch.setattr(candidate, "_verify_blob", verify_blob)
    monkeypatch.setattr(candidate, "_assess_model_runs", model_runs)
    report = candidate.assess_data_root(ledger_env["config"].data_root)

    assert events == ["closed", "blob", "model-runs"], events
    assert report["referenced_blob_count"] >= 1 and report["referenced_blob_bytes"] > 0
    assert report["active_work"]["attempts"] == 0 and report["durable_runs"]["total"] == 0


@pytest.mark.regression
def test_candidate_still_rejects_an_actual_extra_row_without_a_ledger_entry(ledger_env, candidate) -> None:
    """Same verifier branch the race hit (count mismatch), now on a real divergence."""

    root = ledger_env["config"].data_root
    candidate.assess_data_root(root)
    before = _block_count(ledger_env["database"])

    with _divergence_writer(ledger_env["store"]) as forged:
        row = dict(forged.execute("SELECT * FROM context_blocks ORDER BY block_id LIMIT 1").fetchone())
        row["block_id"] = "block_forged_without_ledger_row"
        columns = ",".join(f'"{name}"' for name in row)
        placeholders = ",".join("?" for _ in row)
        forged.execute(f"INSERT INTO context_blocks({columns}) VALUES({placeholders})", tuple(row.values()))
    assert _block_count(ledger_env["database"]) == before + 1, "the forged row committed"
    assert _ledger_rows(ledger_env["database"], where="primary_key_json LIKE '%block_forged_without_ledger_row%'") == 0, "and left no ledger entry"

    with pytest.raises(OSError, match=DIVERGENCE):
        candidate.assess_data_root(root)


@pytest.mark.regression
def test_candidate_still_rejects_an_actual_row_mutation_behind_the_ledger(ledger_env, candidate) -> None:
    root = ledger_env["config"].data_root
    candidate.assess_data_root(root)
    target = ledger_env["seeded"][0]["block_id"]

    with _divergence_writer(ledger_env["store"]) as mutated:
        changed = mutated.execute("UPDATE context_blocks SET byte_count=byte_count+1 WHERE block_id=?", (target,)).rowcount
    assert changed == 1
    assert _ledger_rows(ledger_env["database"], where="operation='update'") == 0, "the mutation left no ledger entry"

    with pytest.raises(OSError, match=DIVERGENCE):
        candidate.assess_data_root(root)


@pytest.mark.regression
def test_candidate_rejects_a_real_divergence_even_with_a_concurrent_writer_present(ledger_env, candidate, monkeypatch) -> None:
    """A genuine divergence must not hide behind the snapshot repair."""

    root = ledger_env["config"].data_root
    with _divergence_writer(ledger_env["store"]) as mutated:
        changed = mutated.execute("UPDATE context_blocks SET byte_count=byte_count+1 WHERE block_id=?", (ledger_env["seeded"][1]["block_id"],)).rowcount
    assert changed == 1
    assert _ledger_rows(ledger_env["database"], where="operation='update'") == 0

    proxies = _patch_connection_factory(monkeypatch, candidate, ledger_env["commit_block"])
    with pytest.raises(OSError, match=DIVERGENCE):
        candidate.assess_data_root(root)
    # The divergence is detected during the ledger scan, before the count statement.
    assert [proxy.fired for proxy in proxies] == [0]


# ---------------------------------------------------------------------------
# Permanent mutation controls on the loaded candidate. These replace a
# hash-gated baseline check: they reproduce the defect by removing the repair
# from the candidate itself, so they never skip and never depend on which bytes
# happen to be installed.
# ---------------------------------------------------------------------------


@pytest.mark.concurrency
def test_mutation_removing_the_snapshot_pin_reproduces_the_live_false_divergence(ledger_env, candidate, monkeypatch) -> None:
    """The pre-repair behaviour, re-created from the candidate: no BEGIN, same real concurrent write, false divergence."""

    root = ledger_env["config"].data_root
    candidate.assess_data_root(root)
    before = _block_count(ledger_env["database"])

    monkeypatch.setattr(candidate, "_begin_read_snapshot", lambda connection: None)
    proxies = _patch_connection_factory(monkeypatch, candidate, ledger_env["commit_block"])
    with pytest.raises(OSError, match=DIVERGENCE):
        candidate.assess_data_root(root)
    assert [proxy.fired for proxy in proxies] == [1]
    assert proxies[0].in_transaction_at_fire is False, "without the pin the count statement runs outside any transaction"
    assert _block_count(ledger_env["database"]) == before + 1, "the concurrent write really committed"

    # Same bytes, pin restored, no race: the data was consistent all along.
    monkeypatch.undo()
    after = _assess_consistent(candidate, root)
    assert after["referenced_blob_count"] >= 1


@pytest.mark.regression
def test_mutation_a_pin_that_opens_no_transaction_is_refused_before_any_check(ledger_env, candidate, monkeypatch) -> None:
    """The guard inside ``_begin_read_snapshot`` must notice when BEGIN did not take effect."""

    root = ledger_env["config"].data_root
    real = candidate._connect_read_only

    # Control: on the real read-only connection the pin opens a transaction and never blocks a writer.
    control = real(ledger_env["database"])
    try:
        candidate._begin_read_snapshot(control)
        assert control.in_transaction is True
        ledger_env["commit_block"]()
        control.rollback()
    finally:
        control.close()

    statements: list[str] = []

    def swallowing(database: Path) -> InterleavingConnection:
        connection = real(database)
        proxy = InterleavingConnection(connection, before_sql="\0never", write=lambda: None)

        def execute(sql: str, *args: Any) -> sqlite3.Cursor:
            statements.append(sql)
            if sql.lstrip().upper().startswith("BEGIN"):
                return connection.execute("SELECT 1")  # accepted the call, opened nothing
            return connection.execute(sql, *args)

        proxy.execute = execute  # type: ignore[method-assign]
        return proxy

    monkeypatch.setattr(candidate, "_connect_read_only", swallowing)
    with pytest.raises(OSError, match="could not open a read snapshot"):
        candidate.assess_data_root(root)
    assert statements == ["BEGIN"], "refusal happens before the first diagnostic statement"
