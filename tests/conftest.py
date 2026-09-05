from __future__ import annotations

from pathlib import Path

import pytest

from lattice import Lattice, LatticeConfig, LatticeStore
from lattice.contracts import ProjectCreate, ProviderRoute, RoleCreate, RoleSessionCreate


BUILTIN_WORKFLOW_PACK_ROOT = Path(__file__).parents[1] / "workflow_packs"
TEST_WORKFLOW_PACK_ROOT = Path(__file__).parent / "fixtures" / "workflow_packs"
TEST_WORKFLOW_PACK_ROOTS = (BUILTIN_WORKFLOW_PACK_ROOT, TEST_WORKFLOW_PACK_ROOT)
TEST_QUATERNARY_PACK = TEST_WORKFLOW_PACK_ROOT / "quaternary" / "pack.json"


@pytest.fixture
def lattice_env(tmp_path, monkeypatch):
    # Unit tests replace provider execution at the supervisor seam. Runtime
    # qualification itself has dedicated installed-boundary tests.
    monkeypatch.setattr("lattice.supervisor.require_current_adapter_qualification", lambda _adapter: None)
    config = LatticeConfig(data_root=tmp_path / "data", port=9010, axisvi_root=None, workflow_pack_roots_extra=(TEST_WORKFLOW_PACK_ROOT,))
    store = LatticeStore(config)
    lattice = Lattice(store)
    project = store.create_project(ProjectCreate(title="Test lattice", purpose="Independent Lattice assurance"))
    role = store.create_role(project["project_id"], RoleCreate(name="operator", instructions="Use only supplied context.", authority_limits=["no external actions"]))
    session = store.create_role_session(RoleSessionCreate(project_id=project["project_id"], role_id=role["role_id"], label="operator-spine"))
    route = ProviderRoute(provider="openai", model="gpt-5.6-sol", harness="codex", effort="high")
    return {"config": config, "store": store, "lattice": lattice, "project": project, "role": role, "session": session, "route": route}


def completed_invocation(env, prompt: str, response: str):
    from lattice.contracts import ContextBlockCreate, DraftCreate

    draft = env["store"].create_draft(DraftCreate(project_id=env["project"]["project_id"], role_session_id=env["session"]["role_session_id"], prompt=prompt, route=env["route"]))
    invocation = env["lattice"].seal(draft["draft_id"])
    output = env["store"].create_context_block(ContextBlockCreate(project_id=env["project"]["project_id"], semantic_type="model-response", content=response, producing_invocation_id=invocation["invocation_id"], authority="test-oracle", evidence_level="planted"))
    return env["store"].attach_imported_output(invocation["invocation_id"], output["block_id"])
