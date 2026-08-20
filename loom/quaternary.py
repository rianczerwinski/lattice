"""Governed bridge to the quaternary system's own command and storage boundary."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .canonical import sha256_bytes, stamp


class QuaternaryBoundaryError(RuntimeError):
    pass


class QuaternaryAdapter:
    """Read typed state directly; mutate only through gatherctl's public commands."""

    READ_OPERATIONS = {"language-status", "peek", "next-language"}
    WRITE_OPERATIONS = {
        "prepare-language",
        "qualify-language",
        "freeze-language-profile",
        "review-language-contributions",
        "freeze-language-query-plan",
        "preflight-language",
        "create-gather",
        "stop-language-preparation",
    }
    ARGUMENTS = {
        "language": "--language",
        "preparation_id": "--preparation-id",
        "effort": "--effort",
        "input": "--input",
        "evaluator": "--evaluator",
        "reviewer": "--reviewer",
        "checker": "--checker",
        "reason": "--reason",
        "last": "--last",
    }

    def __init__(self, database: Path, gatherctl: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.database = database.resolve()
        self.gatherctl = gatherctl.resolve()
        self.runner = runner

    def inspect(self, selector: dict[str, Any]) -> dict[str, Any]:
        preparation_id = selector.get("preparation_id")
        language = selector.get("language", "de")
        with self._readonly() as conn:
            if preparation_id:
                rows = conn.execute("SELECT * FROM qe_language_preparations WHERE preparation_id=?", (preparation_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM qe_language_preparations WHERE language_code=? ORDER BY generation", (language,)).fetchall()
            preparations = []
            for row in rows:
                item = dict(row)
                item["actions"] = [dict(value) for value in conn.execute("SELECT action_id,action_kind,role,status,request_sha256,created_at,completed_at FROM qe_preparation_actions WHERE preparation_id=? ORDER BY created_at", (row["preparation_id"],))]
                item["readiness"] = self._one(conn, "SELECT state,dependency_set_sha256,proof_limits_json,verified_at,verifier FROM qe_language_readiness WHERE preparation_id=?", (row["preparation_id"],))
                preparations.append(item)
        stat = self.database.stat()
        return {"kind": "quaternary-state-receipt", "database": str(self.database), "database_size": stat.st_size, "database_mtime_ns": str(stat.st_mtime_ns), "language": language, "preparations": preparations, "observed_at": stamp()}

    def command(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation not in self.READ_OPERATIONS | self.WRITE_OPERATIONS:
            raise QuaternaryBoundaryError(f"unsupported quaternary operation: {operation}")
        command = [sys.executable, str(self.gatherctl), "--database", str(self.database), operation]
        for key, value in arguments.items():
            if key not in self.ARGUMENTS:
                raise QuaternaryBoundaryError(f"undeclared argument: {key}")
            command.extend([self.ARGUMENTS[key], str(value)])
        result = self.runner(command, text=True, capture_output=True, check=False)
        receipt = {"kind": "quaternary-command-receipt", "operation": operation, "arguments": arguments, "exit_code": result.returncode, "stdout_sha256": sha256_bytes(result.stdout.encode()), "stderr_sha256": sha256_bytes(result.stderr.encode()), "completed_at": stamp()}
        if result.returncode != 0:
            raise QuaternaryBoundaryError(json.dumps({**receipt, "stderr": result.stderr[-4000:]}, ensure_ascii=False))
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise QuaternaryBoundaryError("quaternary command did not return JSON") from error
        return {"receipt": receipt, "output": output, "stdout": result.stdout, "stderr": result.stderr}

    def _readonly(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
