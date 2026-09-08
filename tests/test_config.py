"""Contract checks for AXISVI and standalone filesystem discovery."""

from __future__ import annotations

import sys
from pathlib import Path

from lattice.config import LatticeConfig


def test_axisvi_quaternary_paths_follow_the_canon_package_layout(tmp_path: Path) -> None:
    config = LatticeConfig(data_root=tmp_path / "lattice", axisvi_root=tmp_path / "axisvi")

    canon = tmp_path / "axisvi" / "sui" / "mission" / "projects" / "canon"
    assert config.quaternary_root == canon / "src" / "canon" / "discovery" / "reference_cascade"
    assert config.quaternary_database == canon / "data" / "canon.db"
    assert config.quaternary_gatherctl == canon / "src" / "canon" / "discovery" / "reference_cascade" / "gatherctl.py"
    assert config.quaternary_command_prefix == (str(canon / "dev" / "runtime" / "venv" / "bin" / "canon-gather"),)


def test_explicit_quaternary_override_retains_the_portable_layout(tmp_path: Path) -> None:
    override = tmp_path / "portable-quaternary"
    config = LatticeConfig(data_root=tmp_path / "lattice", axisvi_root=None, quaternary_root_override=override)

    assert config.quaternary_root == override
    assert config.quaternary_database == override / "quaternary.sqlite3"
    assert config.quaternary_gatherctl == override / "tools" / "gatherctl.py"
    assert config.quaternary_command_prefix == (sys.executable, str(override / "tools" / "gatherctl.py"))
