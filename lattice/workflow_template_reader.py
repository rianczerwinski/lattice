"""Independent source-bound verification of executable Lattice workflow templates."""

from __future__ import annotations

import re
from typing import Any

from .canonical import canonical_bytes, sha256_bytes, strict_json_loads
from .manifest_reader import ARTIFACT_ID, SEMVER, SHA256, TRACKING_TIMESTAMP, verify_and_reconstruct
from .store import LatticeConflict, LatticeStore


OPAQUE_WORKFLOW_SUBJECT = re.compile(r"lattice-workflow:workflow_[0-9a-f]{32}")


def _require_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise LatticeConflict(f"workflow template {label} is malformed")
    return value


def _verify_envelope(template: dict[str, Any], artifact_id: str, *, schema_version: int) -> list[dict[str, str]]:
    envelope = _require_keys(template.get("generated_object"), {"contract_version", "role", "kind", "schema", "artifact_id", "generated_at", "producer", "subject", "sources"}, "generated-object envelope")
    if envelope["contract_version"] != 1 or envelope["role"] != "canonical" or envelope["kind"] != "lattice-workflow-template" or envelope["artifact_id"] != artifact_id:
        raise LatticeConflict("workflow template generated-object identity is invalid")
    schema = _require_keys(envelope["schema"], {"name", "version"}, "schema")
    producer = _require_keys(envelope["producer"], {"name", "version"}, "producer")
    if schema != {"name": "lattice-workflow-template", "version": schema_version} or producer.get("name") != "lattice" or not isinstance(producer.get("version"), str) or SEMVER.fullmatch(producer["version"]) is None:
        raise LatticeConflict("workflow template schema or producer is invalid")
    if ARTIFACT_ID.fullmatch(str(envelope["artifact_id"])) is None or TRACKING_TIMESTAMP.fullmatch(str(envelope["generated_at"])) is None or OPAQUE_WORKFLOW_SUBJECT.fullmatch(str(envelope["subject"])) is None:
        raise LatticeConflict("workflow template durable identity is invalid")
    sources = envelope["sources"]
    if not isinstance(sources, list) or not sources:
        raise LatticeConflict("workflow template requires exact invocation sources")
    seen: set[str] = set()
    for source in sources:
        _require_keys(source, {"ref", "revision", "sha256"}, "source")
        if not isinstance(source["ref"], str) or not source["ref"].startswith("lattice:invocation/") or source["ref"] in seen or source["revision"] != source["sha256"] or not isinstance(source["sha256"], str) or SHA256.fullmatch(source["sha256"]) is None:
            raise LatticeConflict("workflow template source is ambiguous or inexact")
        seen.add(source["ref"])
    return sources


def _block_contract(store: LatticeStore, block_id: str) -> dict[str, Any]:
    block = store.get_block(block_id)
    return {
        "kind": "context-block",
        "semantic_type": block["semantic_type"],
        "status": block["status"],
        "authority": block["authority"],
        "evidence_level": block["evidence_level"],
        "sensitivity": block["sensitivity"],
        "source_content_sha256": block["content_sha256"],
    }


def verify_workflow_template(store: LatticeStore, stored: dict[str, Any]) -> dict[str, Any]:
    """Re-derive executable topology and contracts without trusting the extractor."""

    if stored.get("artifact_kind") != "workflow-template":
        raise LatticeConflict("artifact is not a Lattice workflow template")
    raw = store.blobs.get(stored["content_sha256"])
    if sha256_bytes(raw) != stored["content_sha256"]:
        raise LatticeConflict("workflow template bytes do not match their artifact identity")
    try:
        template = strict_json_loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise LatticeConflict("workflow template is not strict canonical JSON") from error
    if not isinstance(template, dict) or canonical_bytes(template) != raw or template != stored.get("generated"):
        raise LatticeConflict("workflow template database view disagrees with its canonical bytes")
    _require_keys(template, {"generated_object", "contract_version", "title", "roles", "parameters", "steps", "materialization", "evidence_policy"}, "body")
    versions = {"LatticeWorkflowTemplate/2.0": 2, "LatticeWorkflowTemplate/3.0": 3}
    schema_version = versions.get(template["contract_version"])
    if schema_version is None or not isinstance(template["title"], str) or not template["title"]:
        raise LatticeConflict("workflow template contract or title is invalid")
    if template["materialization"] != {"mode": "one-edge-at-a-time", "endpoint": "/api/v1/workflow-templates/{artifact_id}/steps/{step_id}/drafts"} or template["evidence_policy"] != "prompt and project evidence bytes are required at materialization and are not copied into this template":
        raise LatticeConflict("workflow template execution policy is not supported")
    sources = _verify_envelope(template, stored["artifact_id"], schema_version=schema_version)

    roles = template["roles"]
    parameters = template["parameters"]
    steps = template["steps"]
    if not all(isinstance(value, list) for value in (roles, parameters, steps)) or not steps:
        raise LatticeConflict("workflow template declarations are malformed")
    role_by_id = {item.get("parameter_id"): item for item in roles if isinstance(item, dict)}
    parameter_by_id = {item.get("parameter_id"): item for item in parameters if isinstance(item, dict)}
    step_by_id = {item.get("step_id"): item for item in steps if isinstance(item, dict)}
    if len(role_by_id) != len(roles) or len(parameter_by_id) != len(parameters) or len(step_by_id) != len(steps) or None in role_by_id or None in parameter_by_id or None in step_by_id:
        raise LatticeConflict("workflow template declaration identities are duplicated or missing")
    if list(step_by_id) != [f"step-{index:03d}" for index in range(1, len(steps) + 1)]:
        raise LatticeConflict("workflow template step order is not canonical")

    source_by_invocation: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for source in sources:
        invocation_id = source["ref"].removeprefix("lattice:invocation/")
        invocation = store.get_invocation(invocation_id)
        verify_and_reconstruct(store, invocation["manifest"])
        if invocation["manifest_sha256"] != source["sha256"] or invocation["state"] not in {"succeeded", "imported"}:
            raise LatticeConflict(f"workflow template source does not resolve exactly: {invocation_id}")
        source_by_invocation[invocation_id] = (invocation, source)
    if [step.get("source_invocation_id") for step in steps] != [source["ref"].removeprefix("lattice:invocation/") for source in sources]:
        raise LatticeConflict("workflow template steps do not preserve canonical source order")

    output_to_step = {invocation["output_block_id"]: step_id for step_id, (invocation, _source) in zip(step_by_id, source_by_invocation.values(), strict=True) if invocation.get("output_block_id")}
    used_context_parameters: set[str] = set()
    used_invocation_parameters: set[str] = set()
    used_role_parameters: set[str] = set()
    completed_steps: set[str] = set()

    def verify_binding(binding: Any, block_id: str, role_block_id: str) -> None:
        nonlocal used_context_parameters
        if not isinstance(binding, dict) or not isinstance(binding.get("kind"), str):
            raise LatticeConflict("workflow template context binding is malformed")
        if block_id == role_block_id:
            if binding != {"kind": "role-instruction"}:
                raise LatticeConflict("workflow template changed its source role binding")
            return
        source_step = output_to_step.get(block_id)
        if source_step is not None:
            if binding != {"kind": "step-output", "step_id": source_step} or source_step not in completed_steps:
                raise LatticeConflict("workflow template step output binding is not causally valid")
            return
        if set(binding) != {"kind", "parameter_id"} or binding["kind"] != "context-parameter":
            raise LatticeConflict("workflow template external context binding is malformed")
        parameter_id = binding["parameter_id"]
        parameter = parameter_by_id.get(parameter_id)
        if parameter is None or parameter != {"parameter_id": parameter_id, **_block_contract(store, block_id)}:
            raise LatticeConflict("workflow template context parameter is not source-bound")
        used_context_parameters.add(parameter_id)

    for step in steps:
        if schema_version == 2:
            _require_keys(step, {"step_id", "source_invocation_id", "role_parameter_id", "route_default", "relation", "response_contract", "parents", "context"}, "step")
            actor_declaration = {"kind": "role-parameter", "parameter_id": step["role_parameter_id"]}
        else:
            _require_keys(step, {"step_id", "source_invocation_id", "actor_binding", "route_default", "relation", "response_contract", "parents", "context"}, "step")
            actor_declaration = step["actor_binding"]
        invocation, _source = source_by_invocation[step["source_invocation_id"]]
        if step["route_default"] != invocation["route"] or step["response_contract"] != invocation["response_schema"] or step["relation"] != store.get_draft(invocation["draft_id"])["relation"]:
            raise LatticeConflict("workflow template changed source execution semantics")
        entries = sorted(invocation["manifest"]["segments"] + invocation["manifest"]["omitted"], key=lambda entry: entry["ordinal"])
        role_entry = next((entry for entry in entries if entry["plane"] == "instruction" and entry["semantic_type"] == "role-instruction" and entry["label"].startswith("role:")), None)
        role_block = store.get_block(role_entry["block_id"]) if role_entry else None
        if invocation["actor_kind"] == "direct":
            if schema_version == 2 or actor_declaration != {"kind": "direct"} or role_entry is not None or invocation["role_session_id"] is not None:
                raise LatticeConflict("workflow template changed the direct source actor")
            role_block_id = ""
        else:
            if not isinstance(actor_declaration, dict) or set(actor_declaration) != {"kind", "parameter_id"} or actor_declaration["kind"] != "role-parameter":
                raise LatticeConflict("workflow template changed the role-bound source actor")
            role_parameter_id = actor_declaration["parameter_id"]
            role_parameter = role_by_id.get(role_parameter_id)
            expected_role = {
                "parameter_id": role_parameter_id,
                "kind": "role-session",
                "source_role_label": role_entry["label"] if role_entry else None,
                "source_role_contract": role_block["content"] if role_block else None,
                "source_role_contract_sha256": role_block["content_sha256"] if role_block else None,
                "proof_limit": None if role_block else "the source invocation did not include an observable role-instruction block",
            }
            if role_parameter != expected_role:
                raise LatticeConflict("workflow template role contract is not source-bound")
            used_role_parameters.add(role_parameter_id)
            role_block_id = role_block["block_id"] if role_block else ""
        if not isinstance(step["context"], list) or len(step["context"]) != len(entries):
            raise LatticeConflict("workflow template context does not cover the exact source manifest")
        for declaration, entry in zip(step["context"], entries, strict=True):
            _require_keys(declaration, {"selection", "binding", "derived_bindings"}, "context declaration")
            expected_selection = {key: entry.get(key) for key in ("disposition", "plane", "label", "reason")}
            if declaration["selection"] != expected_selection or not isinstance(declaration["derived_bindings"], list) or len(declaration["derived_bindings"]) != len(entry.get("derived_from_block_ids", [])):
                raise LatticeConflict("workflow template changed source context selection semantics")
            verify_binding(declaration["binding"], entry["block_id"], role_block_id)
            for derived_binding, source_block_id in zip(declaration["derived_bindings"], entry.get("derived_from_block_ids", []), strict=True):
                verify_binding(derived_binding, source_block_id, role_block_id)
        expected_parents = []
        for parent in invocation["manifest"]["ancestry"]:
            parent_id = parent["invocation_id"]
            internal_step = next((candidate["step_id"] for candidate in steps if candidate["source_invocation_id"] == parent_id), None)
            if internal_step is not None:
                if internal_step not in completed_steps:
                    raise LatticeConflict("workflow template source topology is not acyclic")
                expected_parents.append({"kind": "step", "step_id": internal_step})
            else:
                matching = [item for item in parameters if isinstance(item, dict) and item.get("kind") == "invocation" and item.get("source_invocation_id") == parent_id]
                if len(matching) != 1:
                    raise LatticeConflict("workflow template external parent parameter is not exact")
                parameter_id = matching[0]["parameter_id"]
                expected_parents.append({"kind": "invocation-parameter", "parameter_id": parameter_id})
                used_invocation_parameters.add(parameter_id)
        if step["parents"] != expected_parents:
            raise LatticeConflict("workflow template changed source parent topology")
        completed_steps.add(step["step_id"])

    declared_context = {item["parameter_id"] for item in parameters if isinstance(item, dict) and item.get("kind") == "context-block"}
    declared_invocations = {item["parameter_id"] for item in parameters if isinstance(item, dict) and item.get("kind") == "invocation"}
    if declared_context != used_context_parameters or declared_invocations != used_invocation_parameters or set(role_by_id) != used_role_parameters or declared_context | declared_invocations != set(parameter_by_id):
        raise LatticeConflict("workflow template carries unused or unsupported parameters")
    return template
