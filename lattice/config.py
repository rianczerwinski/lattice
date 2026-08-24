"""Filesystem and service configuration for the local Lattice instance."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


LATTICE_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parent
WEB_DIST = PACKAGE_ROOT / "web_dist" if (PACKAGE_ROOT / "web_dist").is_dir() else LATTICE_ROOT / "web" / "dist"
WORKFLOW_PACK_ROOT = PACKAGE_ROOT / "workflow_packs" if (PACKAGE_ROOT / "workflow_packs").is_dir() else LATTICE_ROOT / "workflow_packs"


def discover_axisvi_root(start: Path = PACKAGE_ROOT, environ: Mapping[str, str] = os.environ) -> Path | None:
    explicit = environ.get("AXISVI_ROOT")
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / "axis").is_dir() or not (root / "sui").is_dir():
            raise RuntimeError(f"AXISVI_ROOT does not name an AXISVI repository: {root}")
        return root
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "axis").is_dir() and (candidate / "sui").is_dir():
            return candidate
    return None


def user_data_root(environ: Mapping[str, str] = os.environ) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Lattice"
    base = Path(environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
    return base / "axisvi" / "lattice"


AXIS_ROOT = discover_axisvi_root()
DEFAULT_DATA_ROOT = user_data_root()


@dataclass(frozen=True)
class LatticeConfig:
    data_root: Path
    host: str = "127.0.0.1"
    port: int = 9010
    octopus_base_url: str = "http://127.0.0.1:8000"
    lease_seconds: int = 900
    axisvi_root: Path | None = AXIS_ROOT
    quaternary_root_override: Path | None = None
    workflow_pack_roots_extra: tuple[Path, ...] = ()
    public_url: str | None = None
    frame_origins: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "LatticeConfig":
        axisvi_root = discover_axisvi_root()
        default_data = user_data_root()
        extra_pack_roots = tuple(Path(value).expanduser().resolve() for value in os.environ.get("LATTICE_WORKFLOW_PACK_ROOTS", "").split(os.pathsep) if value)
        return cls(
            data_root=Path(os.environ.get("LATTICE_DATA_DIR", default_data)).expanduser(),
            host=os.environ.get("LATTICE_HOST", "127.0.0.1"),
            port=int(os.environ.get("LATTICE_PORT", "9010")),
            octopus_base_url=os.environ.get("LATTICE_OCTOPUS_URL", "http://127.0.0.1:8000"),
            axisvi_root=axisvi_root,
            quaternary_root_override=Path(os.environ["LATTICE_QUATERNARY_ROOT"]).expanduser().resolve() if os.environ.get("LATTICE_QUATERNARY_ROOT") else None,
            workflow_pack_roots_extra=extra_pack_roots,
            public_url=os.environ.get("LATTICE_PUBLIC_URL"),
            frame_origins=tuple(value.rstrip("/") for value in os.environ.get("LATTICE_FRAME_ORIGINS", "").split(",") if value),
        )

    @property
    def database(self) -> Path:
        return self.data_root / "lattice.sqlite3"

    @property
    def blobs(self) -> Path:
        return self.data_root / "blobs"

    @property
    def runs(self) -> Path:
        return self.data_root / "model-runs"

    @property
    def operator_token(self) -> Path:
        return self.data_root / "operator-token"

    @property
    def backups(self) -> Path:
        return self.data_root / "backups"

    @property
    def quaternary_root(self) -> Path | None:
        if self.quaternary_root_override:
            return self.quaternary_root_override
        if self.axisvi_root:
            return self.axisvi_root / "sui" / "mission" / "projects" / "canon" / "dev" / "quaternary-exhaustion"
        return None

    @property
    def workflow_pack_roots(self) -> tuple[Path, ...]:
        return (WORKFLOW_PACK_ROOT, *(path.expanduser().resolve() for path in self.workflow_pack_roots_extra))

    @property
    def allowed_file_roots(self) -> tuple[Path, ...]:
        return (self.axisvi_root,) if self.axisvi_root else (self.data_root,)
