#!/usr/bin/env python3
"""Drive the installed cockpit and independently bind its actions to base ledger rows."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from lattice.canonical import canonical_bytes, generated_object, sha256_bytes, strict_json_loads


REQUIRED_ACTIONS = ("blank-project", "claude-send", "codex-side-quest", "fan-out", "exact-excerpt-graft", "reciprocal-cross-feed", "fan-in", "context-inspection", "dormant-branch-return", "slate-tag")


class BrowserQualificationFailure(RuntimeError):
    pass


def _bootstrap(base_url: str, token: str) -> str:
    host = base_url.removeprefix("http://").removeprefix("https://").rstrip("/")
    request = urllib.request.Request(f"{base_url.rstrip('/')}/api/session/bootstrap", data=b"{}", method="POST", headers={"Host": host, "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise BrowserQualificationFailure(f"could not issue one-use browser bootstrap: {error}") from error
    bootstrap = value.get("bootstrap_token") if isinstance(value, dict) else None
    if not isinstance(bootstrap, str):
        raise BrowserQualificationFailure("service issued no browser bootstrap")
    return bootstrap


def _read_blob(data_root: Path, digest: str) -> bytes:
    path = data_root / "blobs" / digest[:2] / digest
    content = path.read_bytes()
    if sha256_bytes(content) != digest:
        raise BrowserQualificationFailure(f"ledger blob disagrees with its identity: {digest}")
    return content


def _project_rows(data_root: Path, title: str) -> tuple[str, list[dict[str, Any]]]:
    database = data_root / "lattice.sqlite3"
    connection = sqlite3.connect(f"file:{database.resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        projects = list(connection.execute("SELECT project_id FROM projects WHERE title=?", (title,)))
        if len(projects) != 1:
            raise BrowserQualificationFailure(f"browser project title is not unique in the base ledger: {title}")
        project_id = str(projects[0]["project_id"])
        rows = [dict(row) for row in connection.execute("""SELECT i.invocation_id,i.manifest_sha256,i.manifest_json,i.route_json,i.state,i.output_block_id,
                    d.prompt_block_id,d.parents_json,d.selections_json,cb.content_sha256 AS prompt_sha256,
                    bm.batch_id,bm.assembly_sha256,i.created_at
               FROM invocations i
               JOIN drafts d ON d.draft_id=i.draft_id
               JOIN context_blocks cb ON cb.block_id=d.prompt_block_id
               LEFT JOIN invocation_batch_members bm USING(invocation_id)
               WHERE i.project_id=? ORDER BY i.created_at,i.invocation_id""", (project_id,))]
        for row in rows:
            row["prompt"] = _read_blob(data_root, row.pop("prompt_sha256")).decode("utf-8", errors="strict")
            row["parents"] = strict_json_loads(row.pop("parents_json"))
            row["selections"] = strict_json_loads(row.pop("selections_json"))
            row["route"] = strict_json_loads(row.pop("route_json"))
            row["manifest"] = strict_json_loads(row.pop("manifest_json"))
        return project_id, rows
    finally:
        connection.close()


def _match(rows: list[dict[str, Any]], marker: str, count: int) -> list[dict[str, Any]]:
    matches = [row for row in rows if marker in row["prompt"]]
    if len(matches) != count or any(row["state"] != "succeeded" for row in matches):
        raise BrowserQualificationFailure(f"browser action marker did not resolve to {count} successful invocation(s): {marker}")
    return matches


def _provider(row: dict[str, Any]) -> str:
    provider = row["route"].get("provider")
    if provider not in {"anthropic", "openai"}:
        raise BrowserQualificationFailure(f"browser journey used an unexpected provider route: {provider}")
    return provider


def _one_batch(rows: list[dict[str, Any]], *, shared_assembly: bool) -> tuple[str, list[str]]:
    batch_ids = {row.get("batch_id") for row in rows}
    if len(batch_ids) != 1 or None in batch_ids:
        raise BrowserQualificationFailure("browser fan operation was not atomically sealed as one invocation batch")
    assemblies = {row.get("assembly_sha256") for row in rows}
    if None in assemblies or (shared_assembly and len(assemblies) != 1):
        raise BrowserQualificationFailure("browser fan-out did not retain one content-bound context assembly")
    return str(next(iter(batch_ids))), sorted(str(item) for item in assemblies)


def _manifest_block_ids(row: dict[str, Any]) -> set[str]:
    return {str(item["block_id"]) for item in row["manifest"].get("segments", []) if isinstance(item, dict) and isinstance(item.get("block_id"), str)}


def build_receipt(*, data_root: Path, driver: dict[str, Any], runtime_artifact_id: str) -> dict[str, Any]:
    markers = driver.get("markers")
    if not isinstance(markers, dict) or set(markers) != {"root", "excerpt", "side", "fanout", "crossfeed", "join", "dormant"}:
        raise BrowserQualificationFailure("browser driver markers are malformed")
    project_id, rows = _project_rows(data_root, driver["project_title"])
    root = _match(rows, markers["root"], 1)
    side = _match(rows, markers["side"], 1)
    fanout = _match(rows, markers["fanout"], 2)
    crossfeed = _match(rows, markers["crossfeed"], 2)
    joined = _match(rows, markers["join"], 1)
    dormant = _match(rows, markers["dormant"], 1)
    if root[0]["parents"]:
        raise BrowserQualificationFailure("blank-project root unexpectedly inherited conversational ancestry")
    if side[0]["parents"] != [root[0]["invocation_id"]] or _provider(side[0]) != "openai":
        raise BrowserQualificationFailure("Codex side quest does not descend exactly from the Claude root")
    fanout_batch_id, fanout_assemblies = _one_batch(fanout, shared_assembly=True)
    if {_provider(row) for row in fanout} != {"anthropic", "openai"}:
        raise BrowserQualificationFailure("fan-out is not one Claude and one Codex invocation")
    crossfeed_batch_id, crossfeed_assemblies = _one_batch(crossfeed, shared_assembly=False)
    fanout_by_provider = {_provider(row): row["invocation_id"] for row in fanout}
    for row in crossfeed:
        expected_parent = fanout_by_provider[{"anthropic": "openai", "openai": "anthropic"}[_provider(row)]]
        if row["parents"] != [expected_parent]:
            raise BrowserQualificationFailure("reciprocal cross-feed did not consume the opposite provider branch")
    if set(joined[0]["parents"]) != {row["invocation_id"] for row in crossfeed}:
        raise BrowserQualificationFailure("fan-in did not join both cross-fed branches")
    if dormant[0]["parents"] != [root[0]["invocation_id"]]:
        raise BrowserQualificationFailure("dormant branch return did not resume the original trailhead")
    connection = sqlite3.connect(f"file:{(data_root / 'lattice.sqlite3').resolve(strict=True)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        source_block_id = root[0]["output_block_id"]
        excerpt_rows = list(connection.execute("SELECT block_id,source_sha256,content_sha256,transformation_json FROM context_blocks WHERE project_id=? AND source_ref=?", (project_id, f"lattice:block/{source_block_id}")))
        exact = []
        for row in excerpt_rows:
            transformation = strict_json_loads(row["transformation_json"]) if row["transformation_json"] else None
            if isinstance(transformation, dict) and transformation.get("kind") == "exact-utf8-byte-range-v1":
                exact.append((row, transformation))
        if len(exact) != 1:
            raise BrowserQualificationFailure("browser excerpt does not resolve uniquely")
        excerpt_row, transformation = exact[0]
        if _read_blob(data_root, excerpt_row["content_sha256"]).decode("utf-8", errors="strict") != markers["excerpt"]:
            raise BrowserQualificationFailure("browser excerpt bytes do not equal the selected visible marker")
        source_row = connection.execute("SELECT content_sha256 FROM context_blocks WHERE block_id=?", (source_block_id,)).fetchone()
        if source_row is None:
            raise BrowserQualificationFailure("browser excerpt source block is absent")
        root_output_bytes = _read_blob(data_root, source_row["content_sha256"])
        excerpt_bytes = _read_blob(data_root, excerpt_row["content_sha256"])
        if transformation.get("source_sha256") != source_row["content_sha256"] or transformation.get("excerpt_sha256") != excerpt_row["content_sha256"] or root_output_bytes[transformation["start_byte"]:transformation["end_byte"]] != excerpt_bytes:
            raise BrowserQualificationFailure("browser excerpt bytes disagree with the recorded source range")
        root_output = root_output_bytes.decode("utf-8", errors="strict")
        if markers["root"] not in root_output or markers["excerpt"] not in root_output:
            raise BrowserQualificationFailure("Claude root output omitted its requested visible markers")
        if excerpt_row["block_id"] not in _manifest_block_ids(side[0]):
            raise BrowserQualificationFailure("Codex side quest manifest omitted the exact selected excerpt")
        side_episode = connection.execute("""SELECT e.target_id FROM edges e JOIN slices s ON json_extract(s.query_json,'$.concern_ref')=e.target_id
                                             WHERE e.relation='concerns' AND e.source_type='invocation' AND e.source_id=?
                                               AND e.target_type='external-concern' AND e.target_id GLOB 'lattice:episode_*'""", (side[0]["invocation_id"],)).fetchone()
        if side_episode is None:
            raise BrowserQualificationFailure("Codex branch was not recorded as a child episode")
        slice_row = connection.execute("SELECT slice_id,external_ref,query_json FROM slices WHERE external_ref=?", (driver["slate_external_ref"],)).fetchone()
        if slice_row is None:
            raise BrowserQualificationFailure("browser Slate slice is absent")
        slate_invocation = dormant[0]["invocation_id"]
        concern = connection.execute("SELECT 1 FROM edges WHERE relation='concerns' AND source_id=? AND target_id=?", (slate_invocation, driver["slate_external_ref"])).fetchone()
        if concern is None:
            raise BrowserQualificationFailure("browser Slate tag is not a concern edge")
    finally:
        connection.close()
    ids = lambda values: [item["invocation_id"] for item in values]
    actions = [
        {"action": "blank-project", "invocation_ids": []},
        {"action": "claude-send", "invocation_ids": ids(root)},
        {"action": "codex-side-quest", "invocation_ids": ids(side)},
        {"action": "fan-out", "invocation_ids": ids(fanout)},
        {"action": "exact-excerpt-graft", "invocation_ids": ids(side)},
        {"action": "reciprocal-cross-feed", "invocation_ids": ids(crossfeed)},
        {"action": "fan-in", "invocation_ids": ids(joined)},
        {"action": "context-inspection", "invocation_ids": ids(joined)},
        {"action": "dormant-branch-return", "invocation_ids": ids(dormant)},
        {"action": "slate-tag", "invocation_ids": ids(dormant)},
    ]
    referenced = {invocation_id for action in actions for invocation_id in action["invocation_ids"]}
    manifests = {row["invocation_id"]: row["manifest_sha256"] for row in rows if row["invocation_id"] in referenced}
    sources = [{"ref": f"lattice:invocation:{identity}", "revision": manifests[identity], "sha256": manifests[identity]} for identity in sorted(manifests)]
    value = {
        "generated_object": generated_object(kind="lattice-browser-acceptance-receipt", schema_name="lattice-browser-acceptance-receipt", subject="lattice:installed-browser-acceptance:0.3.1", role="snapshot", sources=sources, producer_name="lattice-installed-browser-acceptance", producer_version="1.0.0"),
        "contract_version": "LatticeBrowserAcceptanceReceipt/1.0",
        "runtime_artifact_id": runtime_artifact_id,
        "project_id": project_id,
        "actions": actions,
        "manifest_sha256s": manifests,
        "excerpt": {"block_id": excerpt_row["block_id"], "source_block_id": transformation["source_block_id"], "source_sha256": transformation["source_sha256"], "start_byte": transformation["start_byte"], "end_byte": transformation["end_byte"], "excerpt_sha256": transformation["excerpt_sha256"]},
        "topology": {
            "root_invocation_id": root[0]["invocation_id"],
            "side_episode_ref": side_episode["target_id"],
            "fanout_batch_id": fanout_batch_id,
            "fanout_assembly_sha256": fanout_assemblies[0],
            "crossfeed_batch_id": crossfeed_batch_id,
            "crossfeed_assembly_sha256s": crossfeed_assemblies,
            "join_parent_invocation_ids": sorted(joined[0]["parents"]),
            "dormant_parent_invocation_id": root[0]["invocation_id"],
        },
        "slate": {"slice_id": slice_row["slice_id"], "external_ref": slice_row["external_ref"], "invocation_id": slate_invocation, "copied_transcript_bytes": 0},
        "driver": {"run_id": driver["run_id"], "single_use_bootstrap": True, "ephemeral_browser_profile": True},
    }
    if {item["action"] for item in actions} != set(REQUIRED_ACTIONS):
        raise BrowserQualificationFailure("browser receipt action set is incomplete")
    return value


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9010")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--web-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1800.0)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    data_root = args.data_root.expanduser().resolve(strict=True)
    runtime_raw = args.runtime_manifest.read_bytes()
    runtime = strict_json_loads(runtime_raw)
    if not isinstance(runtime, dict) or canonical_bytes(runtime) != runtime_raw or runtime.get("contract_version") != "LatticeInstalledRuntime/4.0":
        raise BrowserQualificationFailure("installed runtime manifest is not exact")
    token_path = data_root / "operator-token"
    bootstrap = _bootstrap(args.base_url, token_path.read_text(encoding="utf-8").strip())
    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    payload = canonical_bytes({"base_url": args.base_url, "bootstrap_token": bootstrap, "run_id": run_id, "timeout_ms": int(args.timeout * 1000)})
    driver_path = args.web_root.resolve(strict=True) / "acceptance/installed-conversation.mjs"
    completed = subprocess.run(["node", str(driver_path)], cwd=args.web_root, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-8000:]
        raise BrowserQualificationFailure(f"installed browser journey failed: {detail}")
    driver = strict_json_loads(completed.stdout)
    if not isinstance(driver, dict):
        raise BrowserQualificationFailure("installed browser driver returned no action identity")
    receipt = build_receipt(data_root=data_root, driver=driver, runtime_artifact_id=runtime["generated_object"]["artifact_id"])
    raw = canonical_bytes(receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps({"output": str(args.output.resolve()), "artifact_id": receipt["generated_object"]["artifact_id"], "project_id": receipt["project_id"], "actions": len(receipt["actions"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
