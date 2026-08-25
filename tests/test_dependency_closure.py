"""Independent contract for the standalone runtime dependency closure."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def direct_external_imports() -> set[str]:
    imports: set[str] = set()
    for path in sorted((ROOT / "lattice").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])
    return imports - set(sys.stdlib_module_names) - {"lattice"}


def declared_runtime_dependencies() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return {re.split(r"[<>=!~;\[]", dependency, maxsplit=1)[0].strip().lower().replace("-", "_") for dependency in project["dependencies"]}


@pytest.mark.contract
def test_every_direct_third_party_import_is_a_declared_runtime_dependency() -> None:
    missing = direct_external_imports() - declared_runtime_dependencies()

    assert missing == set()


@pytest.mark.regression
def test_negative_control_omitting_jsonschema_breaks_dependency_closure() -> None:
    mutated = declared_runtime_dependencies() - {"jsonschema"}

    assert "jsonschema" in direct_external_imports() - mutated
