# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.services.render_deploy_config import validate_render_blueprint


def test_render_blueprint_validation_accepts_current_staging_config() -> None:
    errors = validate_render_blueprint(Path("render.yaml"))
    assert errors == []


def test_render_blueprint_validation_rejects_free_plan_without_persistent_contract(tmp_path: Path) -> None:
    render_yaml = tmp_path / "render.yaml"
    render_yaml.write_text(
        """
services:
  - type: web
    name: bad-staging
    runtime: python
    plan: free
    healthCheckPath: /health
    envVars:
      - key: APP_BASE_URL
        value: https://xrml-research-agent-staging.onrender.com
      - key: WEB_APP_BASE_PATH
        value: /app
      - key: XRML_DATA_DIR
        value: /tmp/storage
""".strip()
        + "\n",
        encoding="utf-8",
    )

    errors = validate_render_blueprint(render_yaml)

    assert any("must not use the free plan" in error for error in errors)
    assert any("must define a persistent disk" in error for error in errors)


def test_runtime_base_is_unset_by_default_and_resolves_when_configured(monkeypatch) -> None:
    """`XRML_DATA_DIR` unset means "keep the repository-relative defaults"."""
    import app.domain.constants as constants_module

    monkeypatch.delenv("XRML_DATA_DIR", raising=False)
    assert constants_module._resolve_runtime_base() is None

    monkeypatch.setenv("XRML_DATA_DIR", "/tmp/xrml-data")
    assert constants_module._resolve_runtime_base() == Path("/tmp/xrml-data").resolve()

    monkeypatch.setenv("XRML_DATA_DIR", "   ")
    assert constants_module._resolve_runtime_base() is None, "whitespace must not configure a root"


def test_storage_root_env_override_and_default_shape(tmp_path: Path) -> None:
    """The roots are derived at import time, so the contract is what a fresh process sees.

    Monkeypatching the environment cannot move an already-imported module's
    constants, and reloading the module inside the suite would leak rebound paths
    into every later test. A subprocess is the only faithful observation, and it
    is what a clean clone actually experiences.
    """
    probe = (
        "import json;from app.domain import constants as c;"
        "print(json.dumps({'storage':c.STORAGE_ROOT.as_posix(),"
        "'data':c.LEGACY_DATA_ROOT.as_posix(),'reports':c.LEGACY_REPORTS_ROOT.as_posix()}))"
    )
    repo_root = Path(__file__).resolve().parents[1]

    default_env = {k: v for k, v in os.environ.items() if k != "XRML_DATA_DIR"}
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=repo_root, env=default_env,
        capture_output=True, text=True, check=True,
    )
    default_roots = json.loads(result.stdout)
    assert default_roots["storage"].endswith("/storage/runs")
    assert default_roots["storage"] == (repo_root / "storage" / "runs").as_posix()
    assert default_roots["data"] == (repo_root / "data").as_posix()

    override = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=repo_root, env={**default_env, "XRML_DATA_DIR": str(override)},
        capture_output=True, text=True, check=True,
    )
    override_roots = json.loads(result.stdout)
    assert override_roots["storage"] == (override.resolve() / "runs").as_posix()
    # Every mutable root moves, not just run storage -- that is the whole point of
    # the rename from `_resolve_storage_root` to `_resolve_runtime_base`.
    assert override_roots["data"] == (override.resolve() / "data").as_posix()
    assert override_roots["reports"] == (override.resolve() / "reports").as_posix()
    for value in override_roots.values():
        assert not value.startswith(repo_root.as_posix()), "a configured runtime root must leave the checkout"
