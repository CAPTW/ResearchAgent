"""Explicit isolation required by the two reserved, network-free adapters."""
import os
from pathlib import Path
from urllib.parse import urlparse

from app.db import bootstrap


def require_isolated_data_root(prefix: str, error: str) -> None:
    data = os.environ.get("XRML_DATA_DIR", "")
    dedicated = os.environ.get(prefix + "_DATA_ROOT", "")
    repo = Path(__file__).resolve().parents[2]
    allowed = (
        os.environ.get("XRML_ENVIRONMENT") in {"development", "test"}
        and os.environ.get("XRML_SHARED_DEPLOYMENT", "0").lower() in {"0", "false", ""}
        and os.environ.get("TOPIC_SCHEDULER_ENABLED", "").lower() in {"0", "false"}
        and os.environ.get("XRML_RUN_EXECUTOR_MODE") == "disabled"
        and urlparse(os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000")).hostname in {"127.0.0.1", "localhost", "::1"}
        and bool(data) and bool(dedicated) and Path(data).is_absolute() and Path(dedicated).is_absolute()
        and Path(data).resolve() == Path(dedicated).resolve() == bootstrap.STORAGE_ROOT.parent.resolve()
        and not Path(data).resolve().is_relative_to(repo)
    )
    if not allowed:
        raise ValueError(error)
