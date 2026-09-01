"""Bounded, read-only repository navigation for the Lattice cockpit.

Provider harnesses remain responsible for editing and tool execution.  This
module gives the cockpit a fast, exact view of the workspace around those
actions without creating a second editor or terminal state machine.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .context_modules import ContextModuleError, read_bounded_local_file


class WorkspaceControlError(RuntimeError):
    """A workspace view could not be produced inside its declared boundary."""


@dataclass(frozen=True)
class WorkspaceFileView:
    path: str
    content_sha256: str
    byte_count: int
    start_line: int
    end_line: int
    total_lines: int
    truncated_before: bool
    truncated_after: bool
    content: str


@dataclass(frozen=True)
class WorkspaceSearchHit:
    path: str
    line: int | None
    column: int | None
    preview: str


class WorkspaceControl:
    """Read one explicitly selected workspace beneath configured roots."""

    MAX_FILE_BYTES = 8 * 1024 * 1024
    MAX_VIEW_LINES = 1000
    MAX_RESULTS = 500
    MAX_PROCESS_BYTES = 8 * 1024 * 1024

    def __init__(self, workspace: Path, *, allowed_roots: tuple[Path, ...]) -> None:
        resolved = workspace.expanduser().resolve()
        roots = tuple(root.expanduser().resolve() for root in allowed_roots)
        if not roots or not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise WorkspaceControlError("workspace is outside the configured local roots")
        if not resolved.is_dir() or resolved.is_symlink():
            raise WorkspaceControlError("workspace must be a real local directory")
        self.workspace = resolved
        self.allowed_roots = roots

    def _path(self, relative: str) -> Path:
        if not isinstance(relative, str) or "\x00" in relative:
            raise WorkspaceControlError("workspace path is malformed")
        candidate = (self.workspace / relative).resolve()
        if candidate != self.workspace and not candidate.is_relative_to(self.workspace):
            raise WorkspaceControlError("workspace path escapes the selected repository")
        return candidate

    def list_directory(self, relative: str = "", *, limit: int = 500) -> dict[str, object]:
        if isinstance(limit, bool) or not 1 <= limit <= self.MAX_RESULTS:
            raise WorkspaceControlError(f"directory limit must be between 1 and {self.MAX_RESULTS}")
        directory = self._path(relative)
        if not directory.is_dir() or directory.is_symlink():
            raise WorkspaceControlError("workspace path is not a readable directory")
        entries: list[dict[str, object]] = []
        truncated = False
        try:
            with os.scandir(directory) as children:
                for child in sorted(children, key=lambda item: (not item.is_dir(follow_symlinks=False), item.name.casefold(), item.name)):
                    if child.name == ".git":
                        continue
                    kind = "symlink" if child.is_symlink() else "directory" if child.is_dir(follow_symlinks=False) else "file" if child.is_file(follow_symlinks=False) else "other"
                    stat_result = child.stat(follow_symlinks=False)
                    entries.append({
                        "name": child.name,
                        "path": Path(child.path).relative_to(self.workspace).as_posix(),
                        "kind": kind,
                        "byte_count": stat_result.st_size if kind == "file" else None,
                    })
                    if len(entries) > limit:
                        entries.pop()
                        truncated = True
                        break
        except OSError as error:
            raise WorkspaceControlError(f"workspace directory is unavailable: {error}") from error
        return {"path": directory.relative_to(self.workspace).as_posix() or ".", "entries": entries, "truncated": truncated}

    def read_file(
        self,
        relative: str,
        *,
        start_line: int = 1,
        line_count: int = 240,
    ) -> WorkspaceFileView:
        if isinstance(start_line, bool) or start_line < 1:
            raise WorkspaceControlError("start_line must be a positive integer")
        if isinstance(line_count, bool) or not 1 <= line_count <= self.MAX_VIEW_LINES:
            raise WorkspaceControlError(f"line_count must be between 1 and {self.MAX_VIEW_LINES}")
        source = self._path(relative)
        try:
            exact_path, content = read_bounded_local_file(
                str(source),
                allowed_roots=(self.workspace,),
                max_bytes=self.MAX_FILE_BYTES,
            )
        except ContextModuleError as error:
            raise WorkspaceControlError(str(error)) from error
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise WorkspaceControlError("workspace file is not UTF-8 text") from error
        lines = text.splitlines(keepends=True)
        first = min(start_line - 1, len(lines))
        last = min(first + line_count, len(lines))
        return WorkspaceFileView(
            path=exact_path.relative_to(self.workspace).as_posix(),
            content_sha256=hashlib.sha256(content).hexdigest(),
            byte_count=len(content),
            start_line=first + 1,
            end_line=last,
            total_lines=len(lines),
            truncated_before=first > 0,
            truncated_after=last < len(lines),
            content="".join(lines[first:last]),
        )

    def search(
        self,
        query: str,
        *,
        mode: Literal["text", "path", "symbol"] = "text",
        limit: int = 200,
    ) -> list[WorkspaceSearchHit]:
        if not isinstance(query, str) or not query or "\x00" in query or "\n" in query:
            raise WorkspaceControlError("search query must be one non-empty line")
        if isinstance(limit, bool) or not 1 <= limit <= self.MAX_RESULTS:
            raise WorkspaceControlError(f"search limit must be between 1 and {self.MAX_RESULTS}")
        rg = shutil.which("rg")
        if rg is None:
            raise WorkspaceControlError("repository search requires ripgrep (rg)")
        if mode == "path":
            output = self._run([rg, "--files", "--hidden", "-g", "!.git", "--sort", "path"])
            folded = query.casefold()
            return [
                WorkspaceSearchHit(path=line, line=None, column=None, preview=line)
                for line in output.splitlines()
                if folded in line.casefold()
            ][:limit]
        pattern = query if mode == "text" else rf"\b(?:class|def|function|interface|type|struct|enum|trait|fn)\s+{re.escape(query)}\b"
        arguments = [
            rg,
            "--json",
            "--hidden",
            "-g",
            "!.git",
            "--sort",
            "path",
            "--max-count",
            str(limit),
            "--",
            pattern,
            ".",
        ]
        if mode == "text":
            arguments.insert(2, "--fixed-strings")
        output = self._run(arguments, allow_no_match=True)
        return self._parse_rg_json(output, limit=limit)

    def git_status(self) -> dict[str, object]:
        output = self._git(["status", "--porcelain=v2", "--branch"], allow_no_match=False)
        lines = output.splitlines()
        branch = next((line.removeprefix("# branch.head ") for line in lines if line.startswith("# branch.head ")), None)
        changes = [line for line in lines if not line.startswith("# ")]
        return {"branch": branch, "clean": not changes, "changes": changes}

    def git_diff(
        self,
        *,
        view: Literal["unstaged", "staged", "revision"] = "unstaged",
        revision: str | None = None,
        relative: str | None = None,
    ) -> dict[str, object]:
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--unified=3"]
        if view == "staged":
            args.append("--cached")
        elif view == "revision":
            if not revision or revision.startswith("-") or "\x00" in revision or "\n" in revision:
                raise WorkspaceControlError("revision diff requires an explicit revision")
            args.append(revision)
        if relative is not None:
            path = self._path(relative)
            args.extend(["--", path.relative_to(self.workspace).as_posix()])
        output = self._git(args, allow_no_match=True)
        return {"view": view, "revision": revision, "path": relative, "empty": output == "", "diff": output}

    def _git(self, args: list[str], *, allow_no_match: bool) -> str:
        git = shutil.which("git")
        if git is None:
            raise WorkspaceControlError("repository review requires git")
        if not (self.workspace / ".git").exists():
            raise WorkspaceControlError("selected workspace is not a Git repository")
        return self._run(
            [git, "-c", "core.fsmonitor=false", "-C", str(self.workspace), *args],
            allow_no_match=allow_no_match,
        )

    def _run(self, argv: list[str], *, allow_no_match: bool = False) -> str:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise WorkspaceControlError(f"workspace command failed: {error}") from error
        stdout = bytearray()
        stderr = bytearray()
        selector = selectors.DefaultSelector()
        try:
            assert process.stdout is not None and process.stderr is not None
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise WorkspaceControlError("workspace command exceeded its 10-second time bound")
                for key, _mask in selector.select(timeout=remaining):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = stdout if key.fileobj is process.stdout else stderr
                    target.extend(chunk)
                    if len(stdout) + len(stderr) > self.MAX_PROCESS_BYTES:
                        process.kill()
                        process.wait()
                        raise WorkspaceControlError("workspace command exceeded its output bound")
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if returncode != 0 and not (allow_no_match and returncode == 1):
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise WorkspaceControlError(f"workspace command failed ({returncode}): {detail}")
        try:
            return stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise WorkspaceControlError("workspace command emitted non-UTF-8 output") from error

    @staticmethod
    def _parse_rg_json(output: str, *, limit: int) -> list[WorkspaceSearchHit]:
        import json

        hits: list[WorkspaceSearchHit] = []
        for line in output.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise WorkspaceControlError("ripgrep emitted malformed structured output") from error
            if record.get("type") != "match":
                continue
            data = record["data"]
            path = data["path"]["text"]
            line_number = int(data["line_number"])
            preview = data["lines"]["text"].rstrip("\r\n")
            submatches = data.get("submatches", [])
            column = int(submatches[0]["start"]) + 1 if submatches else None
            hits.append(WorkspaceSearchHit(path=path.removeprefix("./"), line=line_number, column=column, preview=preview))
            if len(hits) >= limit:
                break
        return hits
