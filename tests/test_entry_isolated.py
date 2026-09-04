"""Entry-point isolation test: ComfyUI loads root ``__init__.py`` by path
without putting the extension dir on ``sys.path``.

Runs the load in a subprocess scrubbed of the repo so a missing
path-bootstrap fails here exactly like a missing node in ComfyUI.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE = (
    "import importlib.util, sys; "
    "sys.path = [p for p in sys.path if 'TelegramPublisher-Project-Pack' not in p]; "
    "assert not any('TelegramPublisher-Project-Pack' in (p or '') for p in sys.path); "
    f"spec = importlib.util.spec_from_file_location('tp_entry_probe', r'{REPO_ROOT / '__init__.py'}'); "
    "mod = importlib.util.module_from_spec(spec); "
    "spec.loader.exec_module(mod); "
    "assert 'Telegram Send Image' in mod.NODE_CLASS_MAPPINGS, dict(mod.NODE_CLASS_MAPPINGS); "
    "print('isolated load OK')"
)


def test_entry_loads_without_repo_on_sys_path():
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        cwd="/",
        timeout=120,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "isolated load OK" in proc.stdout
