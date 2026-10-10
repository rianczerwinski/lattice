"""An immutable source capsule must retain its explicit personal workspace."""
import os
import subprocess
import sys
from pathlib import Path


def test_installer_honors_the_explicit_workspace_instead_of_capsule_depth(tmp_path):
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, '-c', 'from ops.install import AXISVI_ROOT; print(AXISVI_ROOT)'], cwd=root,
                            env={**os.environ, 'AXISVI_ROOT': str(tmp_path)}, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == str(tmp_path.resolve())
