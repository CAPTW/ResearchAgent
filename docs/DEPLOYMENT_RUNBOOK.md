# Deployment Runbook

검증 기준일: 2026-05-07 (Asia/Seoul)

This runbook defines the reproducible deployment baseline for the AI Research Agent. It documents what is supported, what must be configured, and how operators verify the runtime without implying unsupported distributed behavior.

The complete variable-level contract is maintained in `docs/ENVIRONMENT_REFERENCE.md`. Runtime readiness and system settings expose the same contract as `deployment.config_health` without returning secret values.

The persistent storage layout and backup/restore contract are maintained in `docs/STORAGE_PORTABILITY.md`.

The package creation and install-from-package contract is maintained in `docs/PACKAGING_AND_INSTALLATION.md`.

## Containerization Status

Docker/Compose is intentionally not included in the current release baseline. The repo has no Dockerfile/Compose contract today, and the validated deployment path is the script/package plus Render one-service profile:

- Local development: `scripts\start-dev.ps1`
- Production-like same-origin backend/static SPA: `scripts\start-prod.ps1`
- Worker process on the same storage boundary: `scripts\start-worker.ps1`
- Release gate: `scripts\validate-release.ps1 -WithPlaywright`
- Render one-service profile: `render.yaml`

This avoids maintaining an unvalidated second deployment surface. A future Docker/Compose profile must be added only when it can mount durable `XRML_DATA_DIR`, inject OIDC/SMTP/provider secrets safely, run backend and worker services without duplicate scheduler dispatch, preserve same-origin/static SPA behavior or document CORS/cookie/OIDC changes, and keep cross-node distributed scheduling gated unless a real distributed backend exists.

## Release Classification Contract

| Runtime capability | Release classification | Operational notes |
| --- | --- | --- |
| OIDC configured state | `FULLY_REAL` only with valid IdP/client/session/callback env; `INTENTIONALLY_GATED` / unavailable when missing. | `AUTH_MODE=disabled` is local/internal only. Shared production must use configured OIDC or fail strict validation. |
| SMTP configured state | `FULLY_REAL` only with valid SMTP env and truthful delivery attempts/logs; unavailable when missing. | Missing SMTP env is expected to show `unavailable_missing_env`; invalid or failed SMTP must not be shown as active. |
| Production activation checks | `FULLY_REAL`. | Preflight scripts return non-zero for requested unavailable OIDC/SMTP activation and never print secrets. |
| Release validation profile | `FULLY_REAL`. | `scripts\validate-release.ps1 -WithPlaywright` is the one-command release gate. System settings and guide surface the latest summary/status, screenshot path, command reference, and expected warning classes from real status evidence. |
| Containerization | `INTENTIONALLY_GATED` / not included. | Docker/Compose is intentionally absent; the supported deployment path is script/package plus Render one-service until a real container profile is implemented and smoke-tested. |
| Cross-node distributed scheduler | `INTENTIONALLY_GATED`. | `single_node_file_backed`, `db_backed`, and `queue_backed` remain real, but DB/queue modes are shared-SQLite process/worker modes only. |
| Workspace/team scoping | Deferred / `INTENTIONALLY_GATED`. | Current deployment topology is single-workspace with topic-scoped data and OIDC run ownership. |
| Semantic PDF diff | Deferred / `INTENTIONALLY_GATED`. | Report binary artifacts compare by metadata/hash only. |

## Explicit Deployment Profiles

| Profile | Status | Auth contract | Storage contract | Scheduler contract | Notes |
| --- | --- | --- | --- | --- | --- |
| Local development | `FULLY_REAL` | `AUTH_MODE=disabled` is allowed only for localhost/internal single-user use. | Repo-local file/SQLite storage is allowed. | `single_node_file_backed` is default; `db_backed` is also supported for local verification. | SMTP is optional and may show unavailable. Start with `scripts/start-dev.ps1`. |
| Single-node production | `FULLY_REAL` | OIDC is required unless the deployment is an explicitly safe internal single-user deployment. Shared/public production with disabled auth is blocked by strict validation. | Durable `XRML_DATA_DIR` is required. | `db_backed` or `queue_backed` is supported. A worker process may run on the same host. | Render one-service is the current production-like baseline; SMTP remains optional but unavailable/misconfigured states must be visible. |
| Shared-SQLite worker/process mode | `FULLY_REAL` | Same as the enclosing local or single-node production profile. | Every web/worker process must share the same durable scheduler SQLite DB path. | `db_backed` and `queue_backed` allow competing workers/processes with SQLite leases/queue rows. | This is process-safe on one shared storage boundary, not cross-node distributed scheduling. Queue rows are dispatch events, not duplicate task definitions. |
| Cross-node distributed mode | `INTENTIONALLY_GATED` | Would require production auth and node-safe operator controls. | Would require shared artifact storage and a distributed DB or durable queue visible to every node. | Not supported by the current SQLite scheduler. Requires durable lock/lease semantics and node-safe queue acknowledgement/recovery before it can be enabled. | `SCHEDULER_DISTRIBUTED=true` is expected to fail preflight until this backend exists. |

## One-Command Local Start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1
```

`scripts\start-dev.ps1` delegates to `scripts\start-local.ps1`. Use `-SkipInstall` after dependencies are already installed and `-DryRun` to print commands without starting processes. Before backend startup, the local wrapper runs `python -m app.jobs.topic_schedule_worker --lock-check --recover-stale-lock` so stale file-backed schedule locks left by aborted local runs are cleared only when the owner is safely identified as stale.

Local defaults:

- `AUTH_MODE=disabled`
- `APP_BASE_URL=http://127.0.0.1:8000`
- `WEB_APP_BASE_PATH=/app`
- `TOPIC_SCHEDULER_MODE=embedded`
- `TOPIC_SCHEDULER_ENABLED=true`
- `SCHEDULER_MODE=single_node_file_backed`
- `XRML_ENVIRONMENT=local`
- `XRML_DEPLOYMENT_PROFILE=single-service`

Operator URLs:

- Web app: `http://127.0.0.1:5173/app/`
- Liveness: `http://127.0.0.1:8000/health`
- Readiness: `http://127.0.0.1:8000/health/ready`
- System settings: `http://127.0.0.1:5173/app/settings`

## Reproducible Package Bundle

Create a sanitized package directory and archive:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package-release.ps1 -FreshOutputDir
```

Dry-run the package plan without building or copying:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package-release.ps1 -DryRun -SkipBuild
```

The package includes backend/frontend source, built `web/dist` assets when build is not skipped, scripts, docs, tests, `.env.example`, release truth artifacts, and validation fixture storage needed by the current test contracts. It excludes `.env`, `.mcp.json`, `.venv`, `web/node_modules`, `output`, `tmp`, dependency caches, and schedule lock files. `PACKAGE_MANIFEST.json` records supported profiles, gated capabilities, install commands, run commands, and the validation command.

Production operators should set `XRML_DATA_DIR` to a clean durable writable path. Packaged storage is validation/local bootstrap state and must not be treated as a secret-bearing production seed.

## Production Build And Backend Start

Production build:

```powershell
cd web
npm run build
```

Production-like all-in-one backend start:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1
```

`scripts\start-prod.ps1` builds `web/dist` unless `-SkipBuild` is supplied, runs the scheduler schedule-store lock preflight, then starts FastAPI with:

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Frontend static serving strategy:

- Development uses Vite separately at `http://127.0.0.1:5173/app/`.
- Production-like and Render deployments serve the built SPA from FastAPI under `WEB_APP_BASE_PATH` (`/app` by default).
- Separate frontend hosting is not the current supported release baseline. If used later, keep `APP_BASE_URL`, CORS, cookies, and OIDC callback behavior explicit.

## Render One-Service Baseline

The checked-in `render.yaml` is the supported one-service deployment profile.

Required production shape:

- Persistent disk mounted at `/opt/render/project/src/storage`
- `XRML_DATA_DIR=/opt/render/project/src/storage`
- `AUTH_MODE=oidc`
- `XRML_ENVIRONMENT=staging`
- `XRML_DEPLOYMENT_PROFILE=render-single-service`
- `XRML_STRICT_ENV_VALIDATION=true`
- `LOG_FORMAT=json`
- `TOPIC_SCHEDULER_MODE=embedded`
- `SCHEDULER_MODE=single_node_file_backed`

The checked-in Render profile stays conservative with embedded `single_node_file_backed`. For single-node production operators that run a separate worker on the same host/storage boundary, `SCHEDULER_MODE=db_backed` or `SCHEDULER_MODE=queue_backed` is supported when every process uses the same durable `XRML_DATA_DIR` scheduler database. This is still not cross-node distributed scheduling.

Required secret/config values:

- `SESSION_SECRET`
- `APP_BASE_URL`
- `OIDC_ISSUER`
- `OIDC_CLIENT_ID`
- `OIDC_CLIENT_SECRET`
- `COOKIE_SECURE=true`
- `TRUST_PROXY_HEADERS=true`

Optional values:

- `OIDC_AUDIENCE` for ChatGPT-ready MCP OAuth metadata
- `OPENAI_API_KEY` and model/provider env for model-backed runs
- `NOTIFICATION_EMAIL_SMTP_*` for real SMTP email delivery

Missing optional SMTP/model settings must show unavailable/fallback states. They must not report success.

## Scheduler Runtime Modes

Use `SCHEDULER_MODE` for backend selection:

- `single_node_file_backed`: file-backed task/status store, real `nextRunAt`, heartbeat, leases, duplicate protection, and orphan recovery.
- `db_backed`: SQLite scheduler DB task definitions and dispatch leases for competing workers/processes sharing the same scheduler DB.
- `queue_backed`: SQLite scheduler DB task definitions plus durable queue dispatch events keyed by `schedule_id + due_at`.

Compatibility rules:

- Existing file-backed schedules import into DB/queue modes only when the scheduler DB is empty.
- After DB initialization, DB task definitions are authoritative.
- Queue items are dispatch events only and never duplicate task definitions.
- Do not run file-backed and DB/queue-backed dispatchers against the same due windows simultaneously.
- If DB/queue config is unavailable, runtime status degrades to `legacy_file_read_only`; existing file schedules remain visible and mutation/dispatch is blocked.

Distributed boundary:

- `db_backed` and `queue_backed` are shared-SQLite worker/process modes, not cross-node distributed scheduling.
- `SCHEDULER_DISTRIBUTED=true` is expected to fail preflight until a real distributed backend exists.

## Preflight Commands

Scheduler checks:

```powershell
python -m app.jobs.topic_schedule_worker --check
python -m app.jobs.topic_schedule_worker --check --runtime-mode single_node_file_backed
python -m app.jobs.topic_schedule_worker --check --runtime-mode db_backed
python -m app.jobs.topic_schedule_worker --check --runtime-mode queue_backed
python -m app.jobs.topic_schedule_worker --check --runtime-mode cluster
```

Expected behavior:

- default/file/DB/queue configured checks return exit code `0`.
- invalid mode returns exit code `2` with `legacy_file_read_only`.
- unavailable DB/queue returns exit code `2` with read-only legacy schedule visibility.
- distributed requested returns exit code `2` unless a real multi-node-safe backend is implemented.

One-cycle worker verification:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -Check
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -Status
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -LockCheck -RecoverStaleLock
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -Once -RecoverOrphans
```

Queue-backed worker start:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed
```

Use `-Help` on `start-dev.ps1`, `start-prod.ps1`, `start-worker.ps1`, or `validate-release.ps1` for secret-safe operator usage output. Use `-DryRun` on `start-dev.ps1`, `start-prod.ps1`, `start-worker.ps1`, or `validate-release.ps1` to print the exact command or validation plan without starting long-running processes. `start-dev.ps1 -Check` and `start-prod.ps1 -Check` both include activation preflight and scheduler runtime validation. `-LockCheck -RecoverStaleLock` reports lock presence, owner liveness, cleanup result, and read/write availability; live-owner locks are not deleted automatically.

## Release Validation

Full release gate:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright
```

The release validator is git-independent and writes timestamped logs plus `validation-summary.json` under `output/release-validation/<timestamp>` by default. The summary is the CI-like manifest: it records `ciProfileVersion`, per-step exit codes/durations/log paths, backend/frontend/build/focused runtime/Playwright status, screenshot directory, scheduler lock preflight status, scheduler CLI status, output directory mode, and known warning classes. The default non-Playwright path runs scheduler lock preflight/recovery, backend tests, focused runtime/storage/scheduler tests, frontend tests, focused automations/settings/guide tests, and the production build. `-WithPlaywright` repeats scheduler lock preflight before starting local backend/frontend servers, refreshes screenshots, runs the Playwright truth pass, and runs a final safe lock preflight after owned servers stop unless `-KeepServers` is set.

After validation, operators can inspect the same evidence in the product UI. `/app/settings` shows the validation command, latest summary path/status, Playwright screenshot path, and expected warning classes. `/app/evidence` includes release validation as an operator capability card. These surfaces are status evidence only; missing SMTP/OIDC env and gated cross-node scheduling remain unavailable/gated states.

Useful flags:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -SkipPlaywright
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright -BaseUrl http://127.0.0.1:5173
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright -KeepServers
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright -OutputLogDir output\release-validation\manual-run
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -DryRun -WithPlaywright
```

Playwright truth pass only is normally run through the release script. If backend/frontend are already running, the underlying command is:

```powershell
uv run --with playwright python tests/playwright_dashboard_truth.py --base-url http://127.0.0.1:5173 --output-dir output/playwright/truth-pass
```

Equivalent component checks:

```powershell
uv run pytest -q
cd web; npm run test -- --run
cd web; npm run build
uv run pytest tests/test_topic_scheduler.py tests/test_operability.py -q
cd web; npm run test -- AutomationsScreen SettingsScreen GuideScreen --run
```

The Playwright path refreshes screenshots under `output/playwright/truth-pass` and uses Vite `--strictPort` to avoid stale frontend verification. Expected non-blocking warnings are the existing Vite chunk-size warning, SMTP unavailable when SMTP env is not configured, OIDC unavailable/not-ready when production IdP env is not configured, and intentionally gated cross-node distributed scheduler status. They should remain visible but must not be documented as successful SMTP, OIDC, or distributed scheduler readiness.

## Runtime Health Verification

Operators should verify these endpoints after deploy:

- `GET /health`: platform liveness only.
- `GET /health/ready`: deployment readiness, auth/OIDC safety, storage writability, SQLite path status, frontend/API base path contract, provider status, scheduler status, SMTP/webhook availability, and warnings/errors.
- `GET /settings/status`: same operational status for the system settings screen.

Both readiness endpoints include `deployment.config_health`. Secret-bearing variables such as `SESSION_SECRET`, `OIDC_CLIENT_SECRET`, and SMTP credentials are represented only as state booleans or `missing`/`blank`/`set` values.

Storage portability checks in readiness include:

- data root and runs root write access
- same-directory atomic replace support for JSON artifact writes
- SQLite parent directory write access
- scheduler DB and metadata index paths
- persistent layout paths for topic profiles, schedules, runs, report artifacts, report versions, export artifacts, notifications, provider artifacts, analytics artifacts, and Playwright truth artifacts
- Windows path-length safety warning for long local storage roots

In the UI, verify:

- `/app/settings`: auth, scheduler mode, queue backend state, distributed gate, SMTP state, provider availability.
- `/app/automations`: scheduler runtime mode, worker heartbeat, due count, nextRunAt, lease/queue health.
- `/app/evidence`: guide capability cards reflect scheduler/SMTP/provider/auth state truthfully.
- `/app/runs`: queue-triggered runs show `automation_queue` provenance when present.

## Operator Safety Rules

- Do not set `AUTH_MODE=disabled` in shared/staging/production.
- Do not run shared/staging/production without `XRML_DATA_DIR`; repo-local storage is local development only.
- Do not set `SCHEDULER_DISTRIBUTED=true` expecting multi-node scheduling; it is intentionally gated.
- Do not mark SMTP as available without valid SMTP env.
- Do not run two scheduler runtime modes against the same schedules at the same time.
- Do not run shared-SQLite worker/process mode unless every worker and web process points at the same durable scheduler SQLite DB.
- Do not scale across nodes until the scheduler backend is replaced with a production distributed lock/queue contract.
- Stop the app and workers before taking a storage backup; copy the full `XRML_DATA_DIR`, not just `runs/`.
