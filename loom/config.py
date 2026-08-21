"""Filesystem and service configuration for the local Loom instance."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


LOOM_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parent
WEB_DIST = PACKAGE_ROOT / "web_dist" if (PACKAGE_ROOT / "web_dist").is_dir() else LOOM_ROOT / "web" / "dist"
WORKFLOW_PACK_ROOT = PACKAGE_ROOT / "workflow_packs" if (PACKAGE_ROOT / "workflow_packs").is_dir() else LOOM_ROOT / "workflow_packs"


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
        return Path.home() / "Library" / "Application Support" / "AXISVI" / "Loom"
    base = Path(environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
    return base / "axisvi" / "loom"


AXIS_ROOT = discover_axisvi_root()
DEFAULT_DATA_ROOT = AXIS_ROOT / "axis" / "psyche" / "recollection" / "loom" / "data" if AXIS_ROOT else user_data_root()


@dataclass(frozen=True)
class LoomConfig:
    data_root: Path
    host: str = "127.0.0.1"
    port: int = 9010
    octopus_base_url: str = "http://127.0.0.1:8000"
    lease_seconds: int = 900
    axisvi_root: Path | None = AXIS_ROOT
    quaternary_root_override: Path | None = None

    @classmethod
    def from_environment(cls) -> "LoomConfig":
        axisvi_root = discover_axisvi_root()
        default_data = axisvi_root / "axis" / "psyche" / "recollection" / "loom" / "data" if axisvi_root else user_data_root()
        return cls(
            data_root=Path(os.environ.get("LOOM_DATA_DIR", default_data)).expanduser(),
            host=os.environ.get("LOOM_HOST", "127.0.0.1"),
            port=int(os.environ.get("LOOM_PORT", "9010")),
            octopus_base_url=os.environ.get("LOOM_OCTOPUS_URL", "http://127.0.0.1:8000"),
            axisvi_root=axisvi_root,
            quaternary_root_override=Path(os.environ["LOOM_QUATERNARY_ROOT"]).expanduser().resolve() if os.environ.get("LOOM_QUATERNARY_ROOT") else None,
        )

    @property
    def database(self) -> Path:
        return self.data_root / "loom.sqlite3"

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
    def quaternary_root(self) -> Path | None:
        if self.quaternary_root_override:
            return self.quaternary_root_override
        if self.axisvi_root:
            return self.axisvi_root / "sui" / "mission" / "projects" / "canon" / "dev" / "quaternary-exhaustion"
        return None

    @property
    def allowed_file_roots(self) -> tuple[Path, ...]:
        return (self.axisvi_root,) if self.axisvi_root else (self.data_root,)
