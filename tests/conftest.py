import os
from pathlib import Path

import pytest

from cadence import core

os.environ["CADENCE_NO_NOTIFY"] = "1"


@pytest.fixture
def project(tmp_path: Path):
    """A throwaway project with its own .cadence/ directory."""
    (tmp_path / core.DIR_NAME).mkdir()
    core.configure(tmp_path)
    core.NOTIFIED.clear()
    yield tmp_path


def write_config(root: Path, text: str) -> None:
    (root / core.DIR_NAME / "tasks.toml").write_text(text)
