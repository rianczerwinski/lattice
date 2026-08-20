"""Filesystem and service configuration for the local Loom instance."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


LOOM_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parent
AXIS_ROOT = LOOM_ROOT.parents[2]
DEFAULT_DATA_ROOT = AXIS_ROOT / "axis" / "psyche" / "recollection" / "loom" / "data"
WEB_DIST = PACKAGE_ROOT / "web_dist" if (PACKAGE_ROOT / "web_dist").is_dir() else LOOM_ROOT / "web" / "dist"
WORKFLOW_PACK_ROOT = PACKAGE_ROOT / "workflow_packs" if (PACKAGE_ROOT / "workflow_packs").is_dir() else LOOM_ROOT / "workflow_packs"


@dataclass(frozen=True)
class LoomConfig:
    data_root: Path
    host: str = "127.0.0.1"
    port: int = 9010
    octopus_base_url: str = "http://127.0.0.1:8000"
    lease_seconds: int = 900

    @classmethod
    def from_environment(cls) -> "LoomConfig":
        return cls(
            data_root=Path(os.environ.get("LOOM_DATA_DIR", DEFAULT_DATA_ROOT)).expanduser(),
            host=os.environ.get("LOOM_HOST", "127.0.0.1"),
            port=int(os.environ.get("LOOM_PORT", "9010")),
            octopus_base_url=os.environ.get("LOOM_OCTOPUS_URL", "http://127.0.0.1:8000"),
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
    def quaternary_root(self) -> Path:
        return AXIS_ROOT / "sui" / "mission" / "projects" / "canon" / "dev" / "quaternary-exhaustion"
