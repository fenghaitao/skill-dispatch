"""Fail closed if a test accidentally attempts to launch a worker."""
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def forbid_real_agents(monkeypatch):
    original = subprocess.run

    def guarded(argv, *args, **kwargs):
        executable = argv[0] if isinstance(argv, (list, tuple)) else argv.split()[0]
        if Path(executable).name in {"claude", "copilot", "codex", "orca", "orca-ide", "orca-dev", "herdr"}:
            pytest.fail(f"test attempted to start a real agent or terminal manager: {executable}")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
