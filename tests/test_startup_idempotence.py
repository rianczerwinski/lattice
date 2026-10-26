"""Startup backfills preserve admission markers when no values change."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from lattice import LatticeConfig, LatticeStore
from lattice.contracts import ContextBlockCreate, ProjectCreate
from ops import install as deployment


def heads(config: LatticeConfig) -> dict:
    return deployment.append_stream_heads(config.database)


def counts(config: LatticeConfig) -> tuple[int, int, int]:
    with closing(sqlite3.connect(config.database)) as connection, connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("events", "ledger_changes", "append_integrity"))


def generation(config: LatticeConfig, monkeypatch) -> tuple[str, int]:
    monkeypatch.setattr(deployment, "DEFAULT_DATA_ROOT", config.data_root)
    return deployment.live_database_generation()


def unresolvable_instance(store: LatticeStore, title: str) -> str:
    """A workflow row with no blackboard and no entry event: both backfills resolve to nothing."""
    project = store.create_project(ProjectCreate(title=title, purpose="legacy binding", workflow_pack="quaternary"))
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO workflow_instances(project_id,pack_id,state,revision,automatic_grants_json,updated_at) VALUES(?,?,?,1,'[]',?)",
            (project["project_id"], "quaternary", "initial", "2026-10-26T00:00:00Z"),
        )
    return project["project_id"]


@pytest.mark.regression
@pytest.mark.oracle
def test_repeated_boot_of_current_schema_appends_nothing(tmp_path, monkeypatch):
    config = LatticeConfig(data_root=tmp_path / "data", axisvi_root=None)
    store = LatticeStore(config)
    unresolvable_instance(store, "Legacy A")
    # A reconciled legacy project: blackboard NULL, entry seq already set. This is
    # a valid legacy shape that must remain unchanged when there is no backfill.
    store.create_project(ProjectCreate(title="Legacy B", purpose="", workflow_pack="quaternary"))
    assert store.reconcile_workflow_instances({"quaternary": "initial"}) == 1
    with closing(sqlite3.connect(config.database)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_instances WHERE current_blackboard_block_id IS NULL").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM workflow_instances WHERE state_entry_seq=0").fetchone()[0] == 1

    before_heads, before_counts, before_generation = heads(config), counts(config), generation(config, monkeypatch)
    for _boot in range(3):
        LatticeStore(config)
    assert heads(config) == before_heads, "a clean boot must not move the admission heads"
    assert counts(config) == before_counts
    assert generation(config, monkeypatch) == before_generation, "a clean boot must not move the state digest"

    # Oracle control: one real write is visible to exactly these observers.
    store.create_project(ProjectCreate(title="Admitted after boot"))
    assert heads(config) != before_heads
    assert generation(config, monkeypatch) != before_generation


@pytest.mark.regression
def test_backfill_still_resolves_once_and_then_stays_quiet(tmp_path, monkeypatch):
    """Behaviour preserved: a resolvable row is backfilled on the next boot, exactly once."""
    config = LatticeConfig(data_root=tmp_path / "data", axisvi_root=None)
    store = LatticeStore(config)
    project = store.create_project(ProjectCreate(title="Resolvable", purpose="", workflow_pack="quaternary"))
    assert store.reconcile_workflow_instances({"quaternary": "initial"}) == 1
    block = store.create_context_block(ContextBlockCreate(
        project_id=project["project_id"], semantic_type="workflow-blackboard-snapshot", content="{}",
        authority="workflow-engine", evidence_level="authoritative-read",
    ))
    with store.transaction() as conn:
        conn.execute("UPDATE workflow_instances SET state_entry_seq=0 WHERE project_id=?", (project["project_id"],))
        entry_seq = conn.execute(
            "SELECT MAX(seq) FROM events WHERE project_id=? AND event_type='workflow.instance-reconciled'", (project["project_id"],)
        ).fetchone()[0]
    assert entry_seq is not None

    before = heads(config)
    LatticeStore(config)
    with closing(sqlite3.connect(config.database)) as connection, connection:
        row = connection.execute(
            "SELECT current_blackboard_block_id,state_entry_seq FROM workflow_instances WHERE project_id=?", (project["project_id"],)
        ).fetchone()
    assert row == (block["block_id"], entry_seq)
    after = heads(config)
    assert after["events"] == before["events"], "backfill is a current-view change, never an event"
    assert after["ledger_changes"][0] == before["ledger_changes"][0] + 2, "one ledger row per resolved UPDATE"
    assert after["append_integrity"][0] == before["append_integrity"][0] + 2

    quiet = heads(config)
    LatticeStore(config)
    assert heads(config) == quiet, "once resolved, further boots append nothing"


@pytest.mark.regression
def test_boot_leaves_no_unresolved_rows_rewritten(tmp_path):
    """Direct oracle on the mechanism: no update ledger row names a row whose values did not change."""
    config = LatticeConfig(data_root=tmp_path / "data", axisvi_root=None)
    store = LatticeStore(config)
    project_id = unresolvable_instance(store, "Legacy C")
    with closing(sqlite3.connect(config.database)) as connection, connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM ledger_changes WHERE table_name='workflow_instances' AND operation='update' AND json_extract(primary_key_json,'$.project_id')=?",
            (project_id,),
        ).fetchone()[0]
    LatticeStore(config)
    LatticeStore(config)
    with closing(sqlite3.connect(config.database)) as connection, connection:
        after = connection.execute(
            "SELECT COUNT(*) FROM ledger_changes WHERE table_name='workflow_instances' AND operation='update' AND json_extract(primary_key_json,'$.project_id')=?",
            (project_id,),
        ).fetchone()[0]
    assert after == before

