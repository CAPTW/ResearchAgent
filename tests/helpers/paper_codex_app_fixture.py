"""Explicit fake-process browser composition. No authority HTTP endpoint."""
import os
from pathlib import Path
import sys
import tempfile

from app.domain.constants import STORAGE_ROOT
from app.main import create_app as product_app
from app.services.providers.codex_journal_process import CliRuntime
from app.services.review_codex_execution import CodexPaperRuntime
from tests.test_codex_prepared_review import contract, runtime_at


def create_app():
    root = Path(os.environ["XRML_DATA_DIR"]).resolve(strict=True)
    scratch = Path(tempfile.gettempdir()).resolve(strict=True)
    if (root == scratch or not root.is_relative_to(scratch) or root != STORAGE_ROOT.parent
            or root.drive.upper() != "D:" or os.environ.get("AUTH_MODE") != "disabled"
            or os.environ.get("TOPIC_SCHEDULER_ENABLED") != "0"
            or os.environ.get("XRML_RUN_EXECUTOR_MODE") != "disabled"):
        raise ValueError("Synthetic browser runtime requires the isolated D-drive fixture")
    fake = root / "fake-paper-codex"
    if not fake.exists():
        local_runtime = runtime_at(fake, contract())
    else:
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
        environment.update(CODEX_HOME=str(fake / "empty-home"), TEMP=str(fake), TMP=str(fake), PYTHONIOENCODING="utf-8")
        local_runtime = CliRuntime((sys.executable, str(fake / "fake_cli.py")), environment, fake / "attempts")
    runtime = CodexPaperRuntime(runtime=local_runtime, model="fixture-paper-model",
        trusted_issuers=frozenset({("fixture-paper-issuer", "synthetic-policy-e\u0301")}),
        reasoning_effort="low", timeout_seconds=30, input_origin="FIXTURE_SYNTHETIC")
    return product_app(paper_codex_runtime=runtime)
