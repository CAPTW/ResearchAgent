"""Browser-only composition: empty profile + explicit fake Python, no issuer API."""
import os
from pathlib import Path
import tempfile

from app.domain.constants import STORAGE_ROOT
from app.main import create_app as product_app
from app.services.journal_codex_dispatch import CodexJournalRuntime
from tests.test_codex_journal import runtime_at


def create_app():
    root = Path(os.environ["XRML_DATA_DIR"]).resolve(strict=True)
    scratch = Path(tempfile.gettempdir()).resolve(strict=True)
    if (root == scratch or not root.is_relative_to(scratch) or root != STORAGE_ROOT.parent
            or root.drive.upper() != "D:" or os.environ.get("AUTH_MODE") != "disabled"
            or os.environ.get("TOPIC_SCHEDULER_ENABLED") != "0"
            or os.environ.get("XRML_RUN_EXECUTOR_MODE") != "disabled"):
        raise ValueError("Synthetic browser runtime requires the isolated D-drive fixture")
    runtime = CodexJournalRuntime(runtime=runtime_at(root / "fake-codex-app"),
        trusted_issuers=frozenset({("fixture-only-issuer", "synthetic-policy")}),
        reasoning_effort="low", timeout_seconds=30)
    return product_app(journal_codex_runtime=runtime)
