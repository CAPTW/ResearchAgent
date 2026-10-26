"""Every mutable runtime root must follow the configured runtime base.

`XRML_DATA_DIR` used to redirect run storage only, so a disposable release or
test run still appended retrieved sources into the tracked `data/queues` and
`data/papers/raw` files and generated `notes/**` inside the checkout. A passing
release run then depended on restoring the repository afterwards, which is not
isolation. These controls pin both halves of the contract: unset keeps the
historical defaults exactly, and set moves every mutable root out of the tree.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

MUTABLE_ROOTS = (
    "STORAGE_ROOT",
    "LEGACY_DATA_ROOT",
    "LEGACY_QUEUE_ROOT",
    "LEGACY_RAW_ROOT",
    "LEGACY_PDF_ROOT",
    "LEGACY_TEXT_ROOT",
    "LEGACY_NOTES_ROOT",
    "LEGACY_REPORTS_ROOT",
    "LEGACY_EXPERIMENTS_ROOT",
)


def _constants_with(monkeypatch: pytest.MonkeyPatch, data_dir: str | None):
    if data_dir is None:
        monkeypatch.delenv("XRML_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("XRML_DATA_DIR", data_dir)
    from app.domain import constants

    return importlib.reload(constants)


def test_unset_runtime_base_keeps_historical_repository_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    constants = _constants_with(monkeypatch, None)
    project_root = constants.PROJECT_ROOT

    assert constants.STORAGE_ROOT == project_root / "storage" / "runs"
    assert constants.LEGACY_DATA_ROOT == project_root / "data"
    assert constants.LEGACY_QUEUE_ROOT == project_root / "data" / "queues"
    assert constants.LEGACY_RAW_ROOT == project_root / "data" / "papers" / "raw"
    assert constants.LEGACY_PDF_ROOT == project_root / "data" / "papers" / "pdf"
    assert constants.LEGACY_TEXT_ROOT == project_root / "data" / "papers" / "text"
    assert constants.LEGACY_NOTES_ROOT == project_root / "notes"
    assert constants.LEGACY_REPORTS_ROOT == project_root / "reports"
    assert constants.LEGACY_EXPERIMENTS_ROOT == project_root / "experiments"


def test_configured_runtime_base_moves_every_mutable_root_out_of_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_base = tmp_path / "task-owned-runtime"
    constants = _constants_with(monkeypatch, str(runtime_base))
    repository = str(constants.PROJECT_ROOT)

    for name in MUTABLE_ROOTS:
        root = getattr(constants, name)
        assert root.is_relative_to(runtime_base.resolve()), f"{name} escaped the runtime base: {name}"
        assert not str(root).startswith(repository), f"{name} still writes inside the repository"

    # The queue and raw roots are the two that previously appended into tracked
    # files, so pin their exact shape rather than only their containment.
    assert constants.LEGACY_QUEUE_ROOT == runtime_base.resolve() / "data" / "queues"
    assert constants.LEGACY_RAW_ROOT == runtime_base.resolve() / "data" / "papers" / "raw"
    assert constants.LEGACY_NOTES_ROOT == runtime_base.resolve() / "notes"


def test_runtime_base_reload_is_reversible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Leave the module in its default state for the rest of the session."""
    _constants_with(monkeypatch, str(tmp_path / "scratch"))
    constants = _constants_with(monkeypatch, None)
    assert constants.LEGACY_QUEUE_ROOT == constants.PROJECT_ROOT / "data" / "queues"
