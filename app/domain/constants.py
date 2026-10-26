from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def _resolve_runtime_base() -> Path | None:
    """The task-owned runtime base, when `XRML_DATA_DIR` configures one.

    `XRML_DATA_DIR` used to redirect run storage only. Every legacy root below
    is equally mutable — `retrieval._mirror_legacy_outputs` appends retrieved
    sources into `data/papers/raw` and `data/queues`, and note, report and
    experiment generation write under `notes/`, `reports/` and `experiments/` —
    so a disposable release or test run deposited runtime records inside the
    repository working tree and left the checkout dirty. Deriving every mutable
    root from the same base keeps such a run entirely outside the repository.

    Returns None when unset, in which case all roots keep their historical
    repository-relative defaults.
    """
    configured_root = str(os.getenv("XRML_DATA_DIR", "") or "").strip()
    if not configured_root:
        return None
    return Path(configured_root).expanduser().resolve()


_RUNTIME_BASE = _resolve_runtime_base()
_STORAGE_BASE = PROJECT_ROOT / "storage" if _RUNTIME_BASE is None else _RUNTIME_BASE
_LEGACY_BASE = PROJECT_ROOT if _RUNTIME_BASE is None else _RUNTIME_BASE

STORAGE_ROOT = _STORAGE_BASE / "runs"

LEGACY_DATA_ROOT = _LEGACY_BASE / "data"
LEGACY_QUEUE_ROOT = LEGACY_DATA_ROOT / "queues"
LEGACY_RAW_ROOT = LEGACY_DATA_ROOT / "papers" / "raw"
LEGACY_PDF_ROOT = LEGACY_DATA_ROOT / "papers" / "pdf"
LEGACY_TEXT_ROOT = LEGACY_DATA_ROOT / "papers" / "text"
LEGACY_NOTES_ROOT = _LEGACY_BASE / "notes"
LEGACY_REPORTS_ROOT = _LEGACY_BASE / "reports"
LEGACY_EXPERIMENTS_ROOT = _LEGACY_BASE / "experiments"

RUN_SUBDIRS = (
    "inputs",
    "raw",
    "triage",
    "notes",
    "reviews",
    "reports",
)

TERMINAL_RUN_STATUSES = {"completed", "failed"}
STEP_ORDER = (
    "scout",
    "triage",
    "summary",
    "synthesis",
    "experiment",
)
