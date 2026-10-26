# Packaging And Installation

검증 기준일: 2026-05-07 (Asia/Seoul)

This document describes how to create, install, run, and verify a reproducible package for the current AI Research Agent release candidate. It does not add new runtime capabilities. It packages the already validated app, scripts, docs, tests, and built frontend assets without baking in secrets or claiming unsupported production features.

## Supported Package Profiles

| Profile | Status | How to run |
| --- | --- | --- |
| Local development | `FULLY_REAL` | `scripts\start-dev.ps1` with local/internal `AUTH_MODE=disabled`. |
| Single-node production-like | `FULLY_REAL` | `scripts\start-prod.ps1` with durable `XRML_DATA_DIR` and production activation checks as needed. |
| Shared-SQLite worker/process mode | `FULLY_REAL` | `scripts\start-worker.ps1 -RuntimeMode db_backed` or `-RuntimeMode queue_backed` where all processes share one scheduler SQLite DB/storage boundary. |
| Release validation profile | `FULLY_REAL` | `scripts\validate-release.ps1 -WithPlaywright` records backend/frontend/build/focused runtime/Playwright, lock preflight, scheduler CLI, screenshots, and known warning classes in `validation-summary.json`. |
| Containerization | `INTENTIONALLY_GATED` / not included | No Dockerfile/Compose profile is shipped in this package; script/package plus Render one-service remains the validated path. |
| Cross-node distributed scheduler | `INTENTIONALLY_GATED` | Not packaged as supported. `SCHEDULER_DISTRIBUTED=true` must remain unsupported/degraded until a distributed DB/queue/lease contract exists. |

OIDC and SMTP are env-driven. They are `FULLY_REAL` only when valid environment configuration is supplied and validated. Missing OIDC/SMTP env remains unavailable/gated and must not be documented as ready.

## Containerization Decision

Docker/Compose is not part of the supported release bundle for this candidate. This is intentional.

Decision basis:

- The repository does not currently contain Dockerfile or Compose files.
- The validated release path is already script-based: package creation, local startup, production-like startup, worker startup, activation checks, and Playwright release validation are covered by PowerShell/operator scripts.
- The production-like deployment baseline is same-origin FastAPI static serving plus optional same-host/shared-SQLite worker processes, with `XRML_DATA_DIR` as the durable storage boundary.
- A useful Compose setup would need to define backend, worker, persistent storage, secret injection, and validation behavior without pretending cross-node distributed scheduling exists.
- Adding a partial container setup now would risk becoming a second unvalidated deployment path and could imply production readiness that the release has not verified.

Use `scripts\package-release.ps1`, `scripts\start-prod.ps1`, `scripts\start-worker.ps1`, `render.yaml`, and `scripts\validate-release.ps1` as the supported release path. If containerization becomes a roadmap requirement, add it as a separate validated profile with explicit volume mounts for `XRML_DATA_DIR`, secret injection through environment/secret manager, worker service semantics, and validation commands.

## Create A Package

Create a package directory and zip archive:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package-release.ps1 -FreshOutputDir
```

Create a package directory only:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package-release.ps1 -FreshOutputDir -NoArchive
```

Print actions without building or copying:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package-release.ps1 -DryRun -SkipBuild
```

Default output:

```text
output/packages/xrml-research-agent-release/
output/packages/xrml-research-agent-release.zip
```

Each package contains `PACKAGE_MANIFEST.json` with included paths, excluded secret/dependency/runtime paths, supported profiles, gated capabilities, install commands, run commands, and validation command.

## What The Package Includes

- Backend source: `app/`, `config/`
- Frontend source and built assets: `web/` including `web/dist` when build is not skipped
- Operator scripts: `scripts/`
- Docs: `docs/`, `README.md`, `ui_truth_inventory.md`, `route_contract_matrix.md`
- Test and validation contracts: `tests/`
- Runtime fixture/storage state needed by the current validation contracts: `storage/`, excluding lock files
- Environment template: `.env.example`
- Package/dependency manifests: `pyproject.toml`, `uv.lock`, `web/package.json`, `web/package-lock.json` if present

The package intentionally excludes:

- `.env`
- `.mcp.json`
- `.venv/`
- `web/node_modules/`
- `output/`
- `tmp/`
- schedule lock files such as `storage/topic_schedules/schedules.lock`
- generated log files and dependency caches

Do not add real secrets to the package. Use deployment platform secrets, machine-level environment variables, or another secret manager.

## Install From A Package

From the package root:

```powershell
uv sync --extra dev
cd web
npm ci
cd ..
```

Optional extras:

```powershell
uv sync --extra fulltext
```

`fulltext` is optional for extended PDF/text extraction workflows. Deterministic report PDF export uses the base project ReportLab dependency; there is no separate `pdf` extra in this repository.

## Configure Environment

Start from `.env.example`, but do not rename a real secret-bearing file into the package archive.

Local development defaults:

```powershell
$env:AUTH_MODE="disabled"
$env:APP_BASE_URL="http://127.0.0.1:8000"
$env:WEB_APP_BASE_PATH="/app"
$env:SCHEDULER_MODE="single_node_file_backed"
```

Production-like minimum:

```powershell
$env:XRML_DATA_DIR="D:\xrml-data"
$env:AUTH_MODE="oidc"
$env:SESSION_SECRET="<secret-from-secret-manager>"
$env:APP_BASE_URL="https://your-app.example.com"
$env:OIDC_ISSUER="https://issuer.example.com"
$env:OIDC_CLIENT_ID="<client-id>"
$env:OIDC_CLIENT_SECRET="<secret-from-secret-manager>"
$env:COOKIE_SECURE="true"
$env:TRUST_PROXY_HEADERS="true"
```

SMTP activation is optional:

```powershell
$env:NOTIFICATION_EMAIL_SMTP_HOST="smtp.example.com"
$env:NOTIFICATION_EMAIL_SMTP_PORT="587"
$env:NOTIFICATION_EMAIL_SMTP_TLS="true"
$env:NOTIFICATION_EMAIL_FROM="research@example.com"
```

If SMTP env is absent, email remains `unavailable_missing_env` by design.

## Run The App

Local development:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1 -Help
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1 -DryRun -Check
```

Production-like all-in-one backend/static SPA:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -Help
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -DryRun -Check
```

Use built assets without rebuilding:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -SkipBuild
```

External worker:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -Help
```

Worker/status checks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed -Check
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -LockCheck -RecoverStaleLock
```

## Post-Start Checks

After startup, verify the runtime from real status surfaces:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready
Invoke-RestMethod http://127.0.0.1:8000/settings/status
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed -Check
```

Operator UI checks:

- System settings: `/app/settings` should show auth/OIDC, MCP, storage, scheduler, provider, SMTP/webhook, and deployment profile state without secret values.
- Release validation status: `/app/settings` should show the validation command, latest summary/status path, Playwright screenshot path, and expected warning classes without treating missing SMTP/OIDC env as readiness.
- Scheduler state: `/app/automations` should show current scheduler mode, next run state, worker/queue status, and any read-only/degraded lock state truthfully.
- Notification state: `/app/settings/notifications` should show in-app/webhook/email channel state, including `unavailable_missing_env` for email when SMTP host/from are absent.
- Guide: `/app/evidence` should show the same deployment profile, scheduler gate, OIDC/SMTP availability, and release validation capability state as operator guidance.

## Validate The Package

Normal release validation:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -Help
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -DryRun -WithPlaywright
```

Validation without Playwright:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -SkipPlaywright
```

Production activation checks when real env is supplied:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc -RequireSmtp
```

Expected local warnings:

- Vite chunk-size warning during production build
- SMTP unavailable without `NOTIFICATION_EMAIL_SMTP_HOST` and `NOTIFICATION_EMAIL_FROM`
- OIDC unavailable/not-ready without production IdP/client/session env
- Cross-node distributed scheduler gated/unsupported

These warnings are truthful availability states, not production capability claims.

The generated `validation-summary.json` is the CI-like profile manifest. It records `ciProfileVersion=release-validation-ci-v1`, required step names, per-step status/log paths, backend/frontend/build/focused runtime/Playwright status, screenshot path, scheduler lock preflight status, scheduler CLI status, output directory mode, and known warning classes. System settings and guide read the latest release-validation evidence through existing status APIs; they do not expose secrets or infer readiness from missing env.

## Unsafe Or Gated Configuration

| Configuration | Expected behavior |
| --- | --- |
| `AUTH_MODE=disabled` in shared/staging/production | Unsafe. Strict production validation must block or report not-ready. Use `AUTH_MODE=oidc` with real IdP/session env. |
| `AUTH_MODE=oidc` without issuer/client/session env | OIDC remains `missing_env` or `invalid_config`; do not mark product auth production-ready. |
| SMTP without `NOTIFICATION_EMAIL_SMTP_HOST` and `NOTIFICATION_EMAIL_FROM` | Email remains `unavailable_missing_env`; no fake delivery success is recorded. |
| `SCHEDULER_DISTRIBUTED=true` | Unsupported/degraded. Cross-node distributed scheduling is intentionally gated. |
| `SCHEDULER_MODE=db_backed` or `queue_backed` without shared durable SQLite storage | Unsafe for competing workers. Configure durable `XRML_DATA_DIR` first. |
| Missing or non-writable `XRML_DATA_DIR` in shared production | Not ready. Persistent topic, run, report, artifact, scheduler, and notification state must live on durable writable storage. |
| Missing report/export artifact file | UI/API must show missing/unavailable artifact state, not a fake download. |

## Persistent Storage And Backup

Production-like deployments must set `XRML_DATA_DIR` to a durable writable path. Preserve that directory for backup/restore. It contains topic profiles, schedules, scheduler DB/queue state, runs, reports, report versions, export artifacts, notification logs, provider artifacts, analytics artifacts, and metadata indexes.

The package may include validation fixture/storage state so release validation can run from the package. Do not treat packaged storage as a production seed. For a clean deployment, set `XRML_DATA_DIR` to an empty durable path before startup.

Runtime backups must preserve run directories as whole units. Paper review actions, workbench events, provider payloads, report previews, completed versions, and export artifacts all live under `runs/<run_id>/` and are expected to move together. Validation output under `output/` is release evidence only and is not required to restore product state.

See `docs/STORAGE_PORTABILITY.md` for the full backup/restore contract.
