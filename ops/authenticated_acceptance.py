#!/usr/bin/env python3
"""Run the synthetic, authenticated Lattice conversation acceptance path."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"succeeded", "failed", "interrupted", "imported"}


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def sha256_bytes(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


class AcceptanceFailure(RuntimeError):
    pass


class LatticeAPI:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        content = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if method not in {"GET", "HEAD"}:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=content, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10 if method in {"GET", "HEAD"} else 30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise AcceptanceFailure(f"{method} {path} returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AcceptanceFailure(f"{method} {path} could not reach Lattice: {error}") from error
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AcceptanceFailure(f"{method} {path} returned non-JSON bytes") from error

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", path, payload)

    def expect_rejected(self, path: str, payload: dict[str, Any], *, contains: str) -> str:
        try:
            self.post(path, payload)
        except AcceptanceFailure as error:
            if contains not in str(error):
                raise AcceptanceFailure(f"rejection did not contain {contains!r}: {error}") from error
            return str(error)
        raise AcceptanceFailure(f"{path} accepted a deliberately invalid request")


def route(provider: str, **overrides: Any) -> dict[str, Any]:
    defaults = {
        "anthropic": {"provider": "anthropic", "model": "sonnet", "harness": "claude", "effort": "medium"},
        "openai": {"provider": "openai", "model": "gpt-5.6-sol", "harness": "codex", "effort": "medium"},
    }
    value = {**defaults[provider], **overrides}
    return value


def assembly(project_id: str, prompt: str, *, parents: list[str] | None = None, selections: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "actor_binding": {"kind": "direct"},
        "prompt": prompt,
        "parent_invocation_ids": parents or [],
        "selections": selections or [],
    }


def specification(spec_id: str, semantic_context: dict[str, Any], provider_route: dict[str, Any], *, concerns: list[str] | None = None) -> dict[str, Any]:
    return {"spec_id": spec_id, "assembly": semantic_context, "route": provider_route, "concern_refs": concerns or []}


class AcceptanceRun:
    def __init__(self, api: LatticeAPI, data_root: Path, *, timeout_seconds: int) -> None:
        self.api = api
        self.data_root = data_root
        self.timeout_seconds = timeout_seconds
        self.started_at = stamp()
        self.project_id = ""
        self.invocations: list[str] = []
        self.batches: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.false_mutations: list[dict[str, Any]] = []
        self.health_before = api.get("/api/health")

    def assert_truth(self, name: str, condition: bool, detail: Any) -> None:
        if not condition:
            raise AcceptanceFailure(f"acceptance assertion failed: {name}: {detail}")
        self.assertions.append({"name": name, "state": "pass", "detail": detail})

    def assert_final_health(self) -> dict[str, Any]:
        started = time.monotonic()
        health = self.api.get("/api/health")
        elapsed = time.monotonic() - started
        self.assert_truth("final-daemon-health", health.get("status") == "ok", health)
        self.assert_truth("final-daemon-health-latency", elapsed < 2.0, {"elapsed_seconds": elapsed})
        return health

    def create_batch(self, specs: list[dict[str, Any]], label: str, *, expect_failure: bool = False) -> list[dict[str, Any]]:
        batch_id = f"batch_{uuid.uuid4().hex}"
        preview_request = {"batch_id": batch_id, "specifications": specs}
        preview = self.api.post("/api/v1/invocation-batches/preview", preview_request)
        if len(specs) > 1 and all(item["assembly"] == specs[0]["assembly"] for item in specs):
            self.assert_truth(f"{label}:fanout-semantic-context", len({item["assembly_sha256"] for item in preview["members"]}) == 1, preview["members"])
        self.assert_truth(f"{label}:route-capabilities", all("capabilities" in item["preflight"] for item in preview["members"]), preview["members"])
        create_request = {
            **preview_request,
            "idempotency_key": f"acceptance:{label}:{uuid.uuid4().hex}",
            "expected_preview_sha256": preview["preview_sha256"],
            "confirmed_reason_sha256s": [item["reason_sha256"] for item in preview["confirmation_reasons"]],
        }
        created = self.api.post("/api/v1/invocation-batches", create_request)
        duplicate = self.api.post("/api/v1/invocation-batches", create_request)
        self.assert_truth(f"{label}:idempotent", duplicate["receipt_sha256"] == created["receipt_sha256"] and duplicate["invocation_ids"] == created["invocation_ids"], {"receipt_sha256": created["receipt_sha256"]})
        self.invocations.extend(created["invocation_ids"])
        self.batches.append({"label": label, "batch_id": batch_id, "preview_sha256": preview["preview_sha256"], "receipt_sha256": created["receipt_sha256"], "invocation_ids": created["invocation_ids"], "confirmation_reason_sha256s": create_request["confirmed_reason_sha256s"]})
        terminal = [self.wait_for_invocation(invocation_id) for invocation_id in created["invocation_ids"]]
        if expect_failure:
            self.assert_truth(f"{label}:provider-failure-terminal", all(item["state"] in {"failed", "interrupted"} for item in terminal), [item["state"] for item in terminal])
        else:
            self.assert_truth(f"{label}:provider-success", all(item["state"] == "succeeded" for item in terminal), [{"id": item["invocation_id"], "state": item["state"], "attempts": item["attempts"]} for item in terminal])
        return terminal

    def wait_for_invocation(self, invocation_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        last: dict[str, Any] | None = None
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                last = self.api.get(f"/api/v1/invocations/{invocation_id}")
            except AcceptanceFailure as error:
                last_error = str(error)
                time.sleep(0.5)
                continue
            if last["state"] in TERMINAL_STATES:
                return last
            time.sleep(0.5)
        raise AcceptanceFailure(f"invocation {invocation_id} did not terminalize within {self.timeout_seconds}s; last={last}; last_error={last_error}")

    @staticmethod
    def native_session(invocation: dict[str, Any]) -> str:
        successful = [item for item in invocation["attempts"] if item["state"] == "succeeded" and item.get("runtime_receipt")]
        session = successful[-1]["runtime_receipt"].get("native_session") if successful else None
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str):
            raise AcceptanceFailure(f"workspace invocation {invocation['invocation_id']} exposed no provider session UUID")
        return session_id

    def assert_marker(self, label: str, invocation: dict[str, Any], marker: str) -> None:
        output = invocation.get("output_block")
        content = output.get("content") if isinstance(output, dict) else None
        attempts = invocation.get("attempts") or []
        digest = attempts[-1].get("output_sha256") if attempts else None
        self.assert_truth(f"{label}:semantic-response", isinstance(content, str) and marker in content, {"marker": marker, "output_sha256": digest})

    def run_core(self) -> dict[str, Any]:
        project = self.api.post("/api/v1/projects", {"title": f"Authenticated acceptance {self.started_at}", "purpose": "Synthetic Lattice closure evidence; contains no personal corpus."})
        self.project_id = project["project_id"]
        self.assert_truth("blank-project-has-no-role", project.get("roles") == [] and project.get("sessions") == [], {"roles": project.get("roles"), "sessions": project.get("sessions")})

        root = self.create_batch([specification("claude-root", assembly(self.project_id, "Reply in one short sentence containing marker ROOT-CLAUDE. This is synthetic acceptance data."), route("anthropic"))], "root-claude")[0]
        self.assert_marker("root-claude", root, "ROOT-CLAUDE")
        main_episode = self.api.post("/api/v1/episodes", {"project_id": self.project_id, "name": "Main acceptance trail", "anchor_type": "invocation", "anchor_id": root["invocation_id"]})
        child_episode = self.api.post("/api/v1/episodes", {"project_id": self.project_id, "name": "Synthetic side quest", "parent_episode_id": main_episode["slice_id"], "anchor_type": "invocation", "anchor_id": root["invocation_id"]})

        side = self.create_batch([specification("codex-side", assembly(self.project_id, "Take a side quest: identify one limitation of a two-model comparison in one sentence and include marker SIDE-CODEX.", parents=[root["invocation_id"]]), route("openai"), concerns=[child_episode["concern_ref"]])], "codex-side-quest")[0]
        self.assert_marker("codex-side-quest", side, "SIDE-CODEX")
        self.api.post(f"/api/v1/episodes/{child_episode['slice_id']}/members", {"object_type": "invocation", "object_id": side["invocation_id"]})

        shared = assembly(self.project_id, "Independently give one concise criterion for trustworthy context routing and include marker FANOUT-ROUTED.", parents=[root["invocation_id"]])
        fanout = self.create_batch([
            specification("claude-fanout", shared, route("anthropic")),
            specification("codex-fanout", shared, route("openai")),
        ], "two-provider-fanout")
        for invocation in fanout:
            self.assert_marker(f"two-provider-fanout-{invocation['route']['provider']}", invocation, "FANOUT-ROUTED")
        by_provider = {item["route"]["provider"]: item for item in fanout}

        source_text = by_provider["anthropic"]["output_block"]["content"]
        excerpt_text = source_text[: min(len(source_text), 96)]
        if not excerpt_text:
            raise AcceptanceFailure("Claude fan-out returned an empty successful response")
        excerpt = self.api.post("/api/v1/context-blocks/excerpts", {
            "project_id": self.project_id,
            "source_block_id": by_provider["anthropic"]["output_block_id"],
            "start_byte": 0,
            "end_byte": len(excerpt_text.encode("utf-8")),
        })
        graft = {"block_id": excerpt["block_id"], "disposition": "selected", "plane": "knowledge", "label": "authenticated-exact-graft", "derived_from_block_ids": [by_provider["anthropic"]["output_block_id"]]}

        crossfeed = self.create_batch([
            specification("claude-reads-codex", assembly(self.project_id, "Critique the Codex-produced ancestry response in exactly one sentence beginning `XFEED-CLAUDE: `.", parents=[by_provider["openai"]["invocation_id"]], selections=[graft]), route("anthropic")),
            specification("codex-reads-claude", assembly(self.project_id, "Critique the Claude-produced ancestry response in exactly one sentence beginning `XFEED-CODEX: `.", parents=[by_provider["anthropic"]["invocation_id"]], selections=[graft]), route("openai")),
        ], "reciprocal-crossfeed")
        claude_crossfeed = next(item for item in crossfeed if item["route"]["provider"] == "anthropic")
        codex_crossfeed = next(item for item in crossfeed if item["route"]["provider"] == "openai")
        self.assert_marker("reciprocal-crossfeed-claude", claude_crossfeed, "XFEED-CLAUDE")
        self.assert_marker("reciprocal-crossfeed-codex", codex_crossfeed, "XFEED-CODEX")
        for label, invocation, source, harness in (
            ("reciprocal-crossfeed-claude-parent", claude_crossfeed, by_provider["openai"], "codex"),
            ("reciprocal-crossfeed-codex-parent", codex_crossfeed, by_provider["anthropic"], "claude"),
        ):
            ancestry_segments = [item for item in invocation["manifest"]["segments"] if item["plane"] == "ancestry"]
            expected_label = f"parent:{harness}:{source['invocation_id']}"
            self.assert_truth(
                label,
                len(ancestry_segments) == 1
                and ancestry_segments[0]["label"] == expected_label
                and ancestry_segments[0]["producing_invocation_id"] == source["invocation_id"],
                {"expected_label": expected_label, "ancestry_segments": ancestry_segments},
            )

        joined = self.create_batch([specification("codex-join", assembly(self.project_id, "Join the two critiques into one practical recommendation and include marker JOINED-CODEX.", parents=[item["invocation_id"] for item in crossfeed], selections=[graft]), route("openai"), concerns=[main_episode["concern_ref"]])], "fan-in-return")[0]
        self.assert_marker("fan-in-return", joined, "JOINED-CODEX")
        joined_inspection = self.api.get(f"/api/v1/invocations/{joined['invocation_id']}")
        self.assert_truth("exact-context-reader", joined_inspection["manifest_verification"]["valid"] is True and excerpt_text in joined_inspection["compiled_prompt"], {"manifest_sha256": joined_inspection["manifest"]["manifest_sha256"], "excerpt_block_id": excerpt["block_id"]})
        self.assert_truth("proof-limits-visible", bool(joined_inspection["manifest"]["provider_boundary"]["proof_limits"]), joined_inspection["manifest"]["provider_boundary"])

        concern = f"slate:lattice-authenticated-acceptance-{self.started_at.lower()}"
        tagged_invocation = self.api.post(f"/api/v1/invocations/{joined['invocation_id']}/tags", {"concern_ref": concern, "label": "Lattice authenticated acceptance"})
        tagged_block = self.api.post(f"/api/v1/context-blocks/{joined['output_block_id']}/tags", {"concern_ref": concern, "label": "Lattice authenticated acceptance"})
        self.assert_truth("one-live-slate-slice", tagged_invocation["slice"]["slice_id"] == tagged_block["slice"]["slice_id"], {"slice_id": tagged_invocation["slice"]["slice_id"]})

        template = self.api.post("/api/v1/workflows/extract", {"project_id": self.project_id, "invocation_ids": [by_provider["openai"]["invocation_id"], crossfeed[0]["invocation_id"], joined["invocation_id"]], "title": "Authenticated synthetic comparison path"})
        self.assert_truth("path-extracted", template["generated_object"]["artifact_id"].startswith("artifact:"), {"artifact_id": template["generated_object"]["artifact_id"]})

        reachable: set[str] = set()
        cursor = None
        while True:
            query = "?limit=2"
            if cursor:
                query += "&" + urllib.parse.urlencode({"before_created_at": cursor["created_at"], "before_invocation_id": cursor["invocation_id"]})
            page = self.api.get(f"/api/v1/projects/{self.project_id}/invocations{query}")
            reachable.update(item["invocation_id"] for item in page["items"])
            if not page["has_older"]:
                break
            cursor = page["next_before"]
        self.assert_truth("paginated-history-reachable", set(self.invocations).issubset(reachable), {"expected": len(self.invocations), "reachable": len(reachable)})

        episodes = self.api.get(f"/api/v1/invocations/{root['invocation_id']}/episodes")
        self.assert_truth("episodes-overlap", {item["slice_id"] for item in episodes} >= {main_episode["slice_id"], child_episode["slice_id"]}, {"episode_ids": [item["slice_id"] for item in episodes]})
        return {"root": root, "side": side, "fanout": fanout, "crossfeed": crossfeed, "joined": joined, "main_episode": main_episode, "child_episode": child_episode, "excerpt": excerpt, "slate_slice_id": tagged_invocation["slice"]["slice_id"], "template_artifact_id": template["generated_object"]["artifact_id"]}

    def run_profiles(self, core: dict[str, Any]) -> dict[str, Any]:
        workspace = self.data_root / "acceptance" / f"workspace-{self.started_at}"
        workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
        workspace.joinpath("SYNTHETIC.txt").write_text("Synthetic Lattice authenticated acceptance workspace.\n", encoding="utf-8")
        workspace = workspace.resolve()
        bases: dict[str, dict[str, Any]] = {}
        continuations: dict[str, dict[str, dict[str, Any]]] = {}
        portable: dict[str, dict[str, Any]] = {}
        for provider in ("anthropic", "openai"):
            authority = {
                "execution_profile": "workspace-agent",
                "workspace": str(workspace),
                "tools": [] if provider == "anthropic" else ["shell"],
                "permissions": [],
                "sandbox": "provider-permissions" if provider == "anthropic" else "read-only",
                "approval_policy": "never",
                "network_policy": "disabled",
            }
            base = self.create_batch([specification(f"{provider}-workspace", assembly(self.project_id, f"Start a persistent {provider} session and reply with marker WORKSPACE-{provider.upper()} without changing files.", parents=[core["joined"]["invocation_id"]]), route(provider, **authority))], f"{provider}-workspace")[0]
            self.assert_marker(f"{provider}-workspace", base, f"WORKSPACE-{provider.upper()}")
            self.native_session(base)
            bases[provider] = base
            continuations[provider] = {}
            for mode in ("fork", "resume"):
                native_authority = {**authority, "execution_profile": "native-continuation", "session_policy": "visible_native_continuation", "continuation_mode": mode, "native_parent_invocation_id": base["invocation_id"]}
                native_route = route(provider, **native_authority)
                continuation = self.create_batch([specification(f"{provider}-{mode}", assembly(self.project_id, f"{mode.title()} this provider session and reply with marker {mode.upper()}-{provider.upper()}.", parents=[base["invocation_id"]]), native_route)], f"{provider}-native-{mode}")[0]
                self.assert_marker(f"{provider}-native-{mode}", continuation, f"{mode.upper()}-{provider.upper()}")
                continuations[provider][mode] = continuation
            portable[provider] = self.create_batch([specification(f"{provider}-portable", assembly(self.project_id, f"Use portable Lattice ancestry, not provider-session continuation, and reply with marker PORTABLE-{provider.upper()}.", parents=[base["invocation_id"]]), route(provider))], f"{provider}-portable-fallback")[0]
            self.assert_marker(f"{provider}-portable-fallback", portable[provider], f"PORTABLE-{provider.upper()}")

            changed = dict(native_route)
            changed["workspace"] = str(self.data_root.resolve())
            invalid = {"batch_id": f"batch_{uuid.uuid4().hex}", "specifications": [specification(f"{provider}-authority-crossing", assembly(self.project_id, "This must be rejected.", parents=[base["invocation_id"]]), changed)]}
            rejection = self.api.expect_rejected("/api/v1/invocation-batches/preview", invalid, contains="authority boundaries")
            self.false_mutations.append({"name": f"{provider}-native-authority-crossing", "state": "rejected", "detail_sha256": sha256_bytes(rejection.encode("utf-8"))})
        return {"workspace": str(workspace), "bases": bases, "continuations": continuations, "portable": portable}

    def run_provider_failure_recovery(self, parent_id: str) -> dict[str, Any]:
        broken = route("openai", model="lattice-deliberately-invalid-model")
        failed = self.create_batch([specification("codex-deliberate-failure", assembly(self.project_id, "This invocation must terminalize as a provider failure.", parents=[parent_id]), broken)], "provider-failure", expect_failure=True)[0]
        health = self.api.get("/api/health")
        self.assert_truth("daemon-survives-provider-failure", health["status"] == "ok", health)
        recovered = self.create_batch([specification("claude-after-failure", assembly(self.project_id, "Reply with marker RECOVERED-AFTER-PROVIDER-FAILURE.", parents=[parent_id]), route("anthropic"))], "provider-recovery")[0]
        self.assert_marker("provider-recovery", recovered, "RECOVERED-AFTER-PROVIDER-FAILURE")
        return {"failed": failed, "recovered": recovered}

    def restart_and_resume(self, dormant_parent_id: str) -> dict[str, Any]:
        if sys.platform != "darwin":
            raise AcceptanceFailure("launchd restart acceptance requires macOS")
        before = self.api.get("/api/health")
        result = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.axisvi.lattice"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            raise AcceptanceFailure(f"launchd restart failed: {result.stderr.decode('utf-8', errors='replace')}")
        deadline = time.monotonic() + 120
        after = None
        while time.monotonic() < deadline:
            try:
                after = self.api.get("/api/health")
                if after.get("boot_id") != before.get("boot_id"):
                    break
            except AcceptanceFailure:
                pass
            time.sleep(0.5)
        if after is None or after.get("boot_id") == before.get("boot_id"):
            raise AcceptanceFailure("Lattice did not return with a new daemon boot identity")
        resumed = self.create_batch([specification("resume-dormant", assembly(self.project_id, "Resume this older branch and reply with marker DORMANT-RESUMED.", parents=[dormant_parent_id]), route("openai"))], "dormant-branch-after-restart")[0]
        self.assert_marker("dormant-branch-after-restart", resumed, "DORMANT-RESUMED")
        return {"before_boot_id": before["boot_id"], "after_boot_id": after["boot_id"], "resumed_invocation_id": resumed["invocation_id"]}

    def report(self, *, status: str, core: dict[str, Any] | None, profiles: dict[str, Any] | None, failure_recovery: dict[str, Any] | None, restart: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
        from lattice.canonical import canonical_bytes, generated_object, strict_json_loads

        inspected = []
        for invocation_id in self.invocations:
            try:
                value = self.api.get(f"/api/v1/invocations/{invocation_id}")
            except AcceptanceFailure:
                continue
            inspected.append({"invocation_id": invocation_id, "state": value["state"], "manifest_sha256": value["manifest"]["manifest_sha256"], "output_sha256": value["attempts"][-1].get("output_sha256") if value.get("attempts") else None})
        report_root = self.data_root / "acceptance" / "reports"
        report_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        report_paths = sorted(report_root.glob("*.json"))
        prior = report_paths[-1] if report_paths else None
        parent_artifact_id = None
        if prior:
            try:
                prior_value = strict_json_loads(prior.read_bytes())
                parent_artifact_id = prior_value["generated_object"]["artifact_id"]
            except (OSError, KeyError, TypeError, UnicodeDecodeError, ValueError) as prior_error:
                raise AcceptanceFailure(f"prior acceptance report is unreadable and cannot be silently bypassed: {prior}") from prior_error
        artifact_id = f"artifact:{uuid.uuid4()}"
        sources = [{"ref": f"lattice:invocation:{item['invocation_id']}", "revision": None, "sha256": item["manifest_sha256"]} for item in inspected]
        if not sources:
            sources = [{"ref": f"lattice:acceptance-run:{self.started_at}", "revision": self.started_at, "sha256": None}]
        report_envelope = generated_object(
            kind="lattice-authenticated-acceptance-report",
            schema_name="lattice-authenticated-acceptance-report",
            subject="lattice:authenticated-acceptance",
            sources=sources,
            role="snapshot",
            artifact_id=artifact_id,
            producer_name="lattice-authenticated-acceptance",
            producer_version="2.0.0",
        )
        try:
            health_after = self.api.get("/api/health")
        except AcceptanceFailure as health_error:
            health_after = {"status": "unavailable", "error": str(health_error)}
        value = {
            "generated_object": report_envelope,
            "contract_version": "LatticeAuthenticatedAcceptanceReport/2.0",
            "status": status,
            "started_at": self.started_at,
            "completed_at": stamp(),
            "project_id": self.project_id or None,
            "health_before": self.health_before,
            "health_after": health_after,
            "assertions": self.assertions,
            "false_mutations": self.false_mutations,
            "batches": self.batches,
            "invocations": inspected,
            "core_receipts": None if core is None else {"main_episode_id": core["main_episode"]["slice_id"], "child_episode_id": core["child_episode"]["slice_id"], "excerpt_block_id": core["excerpt"]["block_id"], "slate_slice_id": core["slate_slice_id"], "template_artifact_id": core["template_artifact_id"]},
            "profile_receipts": None if profiles is None else {"workspace": profiles["workspace"], "base_invocation_ids": {key: item["invocation_id"] for key, item in profiles["bases"].items()}, "continuation_invocation_ids": {provider: {mode: item["invocation_id"] for mode, item in modes.items()} for provider, modes in profiles["continuations"].items()}, "portable_invocation_ids": {key: item["invocation_id"] for key, item in profiles["portable"].items()}},
            "provider_failure_receipts": None if failure_recovery is None else {"failed_invocation_id": failure_recovery["failed"]["invocation_id"], "recovered_invocation_id": failure_recovery["recovered"]["invocation_id"]},
            "restart_receipt": restart,
            "error": error,
            "prior_report_artifact_id": parent_artifact_id,
            "proof_limits": ["Provider-owned system context remains observable only through the adapter's declared proof limits.", "Browser rendering is a separate acceptance plane and is not inferred from this API run."],
        }
        path = report_root / f"{value['completed_at']}-{artifact_id.removeprefix('artifact:')[:12]}.json"
        raw = canonical_bytes(value)
        with path.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = os.open(report_root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {"path": str(path), "artifact_id": artifact_id, "status": status, "project_id": self.project_id}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--base-url", default="http://127.0.0.1:9010")
    value.add_argument("--data-root", type=Path, default=Path.home() / "Library" / "Application Support" / "Lattice")
    value.add_argument("--timeout", type=int, default=900)
    value.add_argument("--profiles", action="store_true", help="exercise workspace, native resume/fork, and portable fallback for Claude and Codex")
    value.add_argument("--provider-failure", action="store_true", help="induce a real typed provider failure and prove recovery")
    value.add_argument("--restart-launchd", action="store_true", help="restart the supervised daemon and resume a dormant branch")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    data_root = args.data_root.expanduser().resolve()
    token_path = data_root / "operator-token"
    if not token_path.is_file() or token_path.stat().st_mode & 0o777 != 0o600:
        raise AcceptanceFailure(f"operator token is unavailable or not mode 0600: {token_path}")
    api = LatticeAPI(args.base_url, token_path.read_text(encoding="utf-8").strip())
    run = AcceptanceRun(api, data_root, timeout_seconds=args.timeout)
    core = profiles = failure_recovery = restart = None
    try:
        core = run.run_core()
        if args.profiles:
            profiles = run.run_profiles(core)
        if args.provider_failure:
            failure_recovery = run.run_provider_failure_recovery(core["joined"]["invocation_id"])
        if args.restart_launchd:
            restart = run.restart_and_resume(core["root"]["invocation_id"])
        run.assert_final_health()
        report = run.report(status="pass", core=core, profiles=profiles, failure_recovery=failure_recovery, restart=restart, error=None)
    except BaseException as error:
        report = run.report(status="fail", core=core, profiles=profiles, failure_recovery=failure_recovery, restart=restart, error=f"{type(error).__name__}: {error}")
        print(json.dumps(report, indent=2, sort_keys=True))
        raise
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
