# Operations Guide

검증 기준일: 2026-05-02 (Asia/Seoul)

This app is deployable as a single web service with persistent storage. Scheduler dispatch can run in the default file-backed mode, the optional SQLite DB-backed mode, or the optional SQLite queue-backed mode for competing workers/processes that share the same scheduler database. It is not a horizontally distributed multi-node scheduler platform.

For a compact deployment checklist, supported topology matrix, preflight commands, and post-deploy verification flow, see `docs/DEPLOYMENT_RUNBOOK.md`. For real OIDC/SMTP production activation, use `docs/PRODUCTION_ACTIVATION.md`.

## Local Start

One-command local development:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1
```

Useful URLs:

- Web app: `http://127.0.0.1:5173/app/`
- API health: `http://127.0.0.1:8000/health`
- Readiness and environment status: `http://127.0.0.1:8000/health/ready`
- System settings UI: `http://127.0.0.1:5173/app/settings`

The local start script defaults to `AUTH_MODE=disabled`, `TOPIC_SCHEDULER_MODE=embedded`, and `XRML_DEPLOYMENT_PROFILE=single-service`. This is an internal single-user local mode.

## Runtime Classification

| Area | Production truth |
| --- | --- |
| Scheduler mode | Fully real `single_node_file_backed` scheduling with file-backed storage, heartbeat, leases, duplicate protection, stale lease/orphan recovery, and `nextRunAt`. Optional `db_backed` mode uses the repo SQLite scheduler DB for task definitions, atomic dispatch leases, worker heartbeats, and stale lease recovery between competing workers sharing that DB. Optional `queue_backed` mode adds durable SQLite queue items, queue leases, lease-token-protected completion/failure, queue lease recovery, and queue completion/failure state for competing workers/processes sharing that DB. The worker entrypoint exposes status/one-shot/foreground modes for operators. Horizontally distributed multi-node scheduling is gated. |
| OIDC | `configured` only when issuer, client id/secret, callback/base URL, session secret, and HTTPS shared-deployment expectations pass validation. `unconfigured` is visible. Shared `AUTH_MODE=disabled` is unsafe and strict validation blocks startup. |
| SMTP | `available` when host/from/port and optional auth/TLS settings are valid; `unavailable_missing_env` when host/from are missing; `misconfigured` for invalid env; `failed_delivery` after a real failed send attempt. |
| MCP | Optional and scoped. OAuth/bearer-scope protected tools are safe when configured; ChatGPT-ready OAuth remains gated until public HTTPS/OIDC audience readiness exists. |
| Help/guide | `/app/evidence` is a real product guide over implemented workflows and current capability boundaries. |
| Provider expansion | arXiv/OpenAlex/Crossref are fully real; providers beyond the implemented set remain gated. |
| Report version diff | Completed-report version history and markdown snapshot diff are real; semantic PDF diff remains gated. |
| Workspace/team scope | Deferred. The app is single-workspace with OIDC run ownership, not team membership scoping. |

## Validation

Production activation preflight before startup:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check-production-activation.ps1 -Strict -RequireOidc
```

Add `-RequireSmtp` when SMTP email delivery must be live before go-live. Optional `-CheckOidcDiscovery` and `-CheckSmtpConnectivity` perform network smoke checks without printing secret values; SMTP connectivity does not send email.

CI-like local validation:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/validate-release.ps1
```

Full validation with runtime Playwright screenshots:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/validate-release.ps1 -WithPlaywright
```

The Playwright validation path starts Vite on `127.0.0.1:5173` with `--strictPort`. If that port is occupied, validation fails instead of silently verifying a stale frontend. On Windows, the script stops the spawned backend/frontend process trees after the run.

Validation logs are written under `output/release-validation/<timestamp>` with one log per step and `validation-summary.json` for exit codes, durations, screenshot path, and expected warning classes. See `docs/RELEASE_VALIDATION.md` for flags including `-SkipPlaywright`, `-KeepServers`, `-BaseUrl`, `-OutputLogDir`, and `-ScreenshotDir`.

## Deployment Profile

The checked-in deploy profile is `render.yaml`.

Runtime shape:

- One Render web service.
- Persistent disk mounted at `/opt/render/project/src/storage`.
- `XRML_DATA_DIR=/opt/render/project/src/storage`.
- `AUTH_MODE=oidc`.
- `XRML_DEPLOYMENT_PROFILE=render-single-service`.
- `LOG_FORMAT=json`.
- `TOPIC_SCHEDULER_MODE=embedded`.
- `SCHEDULER_MODE=single_node_file_backed`.

Scheduler contract:

- The default scheduler runtime mode is `single_node_file_backed` with `file_json` schedule/status storage.
- Set `SCHEDULER_MODE=single_node_file_backed` for the default file-backed backend, `SCHEDULER_MODE=db_backed` to store task definitions, dispatch leases, and worker heartbeats in the repo SQLite scheduler DB, or `SCHEDULER_MODE=queue_backed` to store task definitions plus durable queue items/leases in the same scheduler DB. Existing `TOPIC_SCHEDULER_RUNTIME_MODE` and `TOPIC_SCHEDULER_BACKEND` remain accepted compatibility aliases when `SCHEDULER_MODE` is not set.
- `SCHEDULER_MODE=queue_backed` is a real SQLite queue-backed mode for workers/processes sharing the scheduler DB. Its dispatch uniqueness key is `schedule_id + due_at` inside the selected `queue_backed` runtime, enforced by the `scheduler_queue_items` primary/unique keys. It enqueues each due schedule window once, leases queue items durably, records queue completion/failure, rejects stale worker completion/failure after lease recovery by checking the active queue lease token, and uses `automation_queue` run trigger provenance.
- Invalid scheduler runtime modes are explicit readiness warnings locally and strict-validation errors in shared/staging/production. They fall back to `legacy_file_read_only` visibility so operators can inspect existing schedules without accidental dispatch.
- When `db_backed` is first read and the scheduler DB is empty, existing file-backed schedules are imported once with `legacy_source_backend=single_node_file_backed`, `scheduler_backend=db_backed`, and `migration_mode=import_on_first_db_read` metadata. After import, DB-only schedules are not written back into the legacy file store.
- After the scheduler DB has task definitions, `db_backed` and `queue_backed` treat the DB as the authoritative task-definition store. Later file-only schedules are not silently imported, because that would be an implicit migration and can make mode switching ambiguous.
- `queue_backed` uses the same DB task definitions as `db_backed`; queue rows are dispatch events only. Queue mode does not create duplicate task definitions and does not back-write queue-only state into the legacy file store.
- Do not run file-backed and DB/queue-backed schedulers against the same due windows at the same time. Switching modes is safe when only one runtime is active: file mode uses file schedules, DB/queue modes use DB schedules, and the first DB/queue read imports legacy file schedules only if the DB is empty.
- `TOPIC_SCHEDULER_MODE` must be one of `embedded`, `external`, or `disabled`; invalid values are surfaced in readiness and fail strict shared deployments.
- External worker operator commands:
  - `python -m app.jobs.topic_schedule_worker --check` runs a preflight check and exits successfully only when the configured runtime backend is usable and no unsupported distributed mode is requested.
  - `python -m app.jobs.topic_schedule_worker --check --runtime-mode db_backed` validates the SQLite DB-backed scheduler backend before starting workers.
  - `python -m app.jobs.topic_schedule_worker --check --runtime-mode queue_backed` validates the SQLite queue-backed scheduler backend, reports `queue_backend_status`, queue item counts, and fails non-zero when the queue backend is unavailable.
  - `python -m app.jobs.topic_schedule_worker --check --runtime-mode cluster` is intentionally invalid and returns a degraded/read-only legacy fallback payload instead of pretending the mode exists.
  - `python -m app.jobs.topic_schedule_worker --status` prints scheduler runtime JSON, including worker id, runtime mode, storage backend, distributed support flags, heartbeat, due task count, lease health, recent dispatch/success/failure, and orphan recovery counters.
  - `python -m app.jobs.topic_schedule_worker --once --recover-orphans` runs one dispatch/recovery cycle and prints a JSON cycle summary.
  - `python -m app.jobs.topic_schedule_worker --mode external` runs the foreground worker loop.
- `SCHEDULER_DISTRIBUTED=true` or `SCHEDULER_DISTRIBUTED_MODE=enabled` requests horizontally distributed multi-node scheduling. That mode is currently unsupported; readiness/settings/CLI preflight report `distributed_requested=true`, `distributed_support_status=unsupported`, `multi_node_dispatch_safe=false`, and strict shared deployments treat it as a blocking error.
- Automatic execution, `nextRunAt`, heartbeat, file-backed store locks, SQLite DB-backed dispatch leases, SQLite queue-backed queue items when configured, duplicate execution protection, and orphan recovery are real.
- `/health/ready`, `/settings/status`, `/topic-schedules`, `/app/automations`, and `/app/settings` expose `runtime_mode`, `runtime_mode_status`, configured runtime mode raw/source/valid state, storage backend, `queue_backend_status`, queue item counts where available, distributed request/support state, raw scheduler env mode, dispatch safety flags, due task count, lease health, worker id, heartbeat, recent dispatch/success/failure, and orphan recovery count where available.
- This is suitable for the current one-service baseline.
- `db_backed` and `queue_backed` prevent duplicate dispatch for workers/processes sharing the same SQLite scheduler DB. Deterministic concurrency tests cover same-window enqueue races, competing queue lease attempts, stale lease-token acknowledgement rejection, lease expiration/recovery, and failed/missing-run dispatch states. They are not horizontally distributed multi-node scheduler claims.
- If `db_backed` or `queue_backed` cannot open the scheduler DB, the app falls back to `legacy_file_read_only`: existing file-backed schedules remain visible for operator review, but schedule mutation and automatic dispatch are blocked to avoid duplicate or ambiguous execution.
- Do not scale the web service horizontally across nodes without a deployment design that guarantees all workers share the same scheduler DB semantics, storage, and lease expiry behavior.
- Follow-up distributed modes must use an external durable queue service or production database locking design before claiming horizontally distributed multi-node dispatch safety.

## Required Shared Deployment Environment

For shared/staging/production deployments:

- `AUTH_MODE=oidc`
- `XRML_SHARED_DEPLOYMENT=true` for any shared/internal-hosted environment whose `XRML_ENVIRONMENT` name is not exactly `staging` or `production`
- high-entropy `SESSION_SECRET` value, preferably 32+ characters
- HTTPS `APP_BASE_URL`
- `WEB_APP_BASE_PATH=/app`
- absolute http(s) `OIDC_ISSUER`; use HTTPS for shared deployments
- `OIDC_CLIENT_ID`
- `OIDC_CLIENT_SECRET`
- `COOKIE_SECURE=true`
- `TRUST_PROXY_HEADERS=true` when behind a platform proxy
- `XRML_DATA_DIR` pointing to persistent storage

For MCP OAuth readiness:

- `OIDC_AUDIENCE`
- Public HTTPS `APP_BASE_URL`

Optional provider/model execution:

- `XRML_PROVIDER=openai`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_API_BASE`

Optional email delivery:

- `NOTIFICATION_EMAIL_SMTP_HOST`
- `NOTIFICATION_EMAIL_SMTP_PORT`
- valid email-formatted `NOTIFICATION_EMAIL_FROM`
- `NOTIFICATION_EMAIL_SMTP_USERNAME`
- `NOTIFICATION_EMAIL_SMTP_PASSWORD`
- `NOTIFICATION_EMAIL_SMTP_TLS` or `NOTIFICATION_EMAIL_SMTP_SSL`

If SMTP or provider configuration is missing, the UI must show unavailable/fallback states. It should not claim live delivery or live model use.
Email channel state is explicit: `available` for valid SMTP env, `unavailable_missing_env` when host/from are missing, `misconfigured` for invalid port/from/credential/TLS settings, and `failed_delivery` after a real failed SMTP attempt.
If SMTP sender syntax is invalid, username/password are only partially set, or TLS and SSL are both enabled, email remains unavailable and the reason is reported in readiness/settings.

## Observability

Use `/health` for platform liveness.

Use `/health/ready` for operator readiness. It reports:

- deployment readiness: `ready`, `degraded`, or `not_ready`
- storage writability
- auth mode, shared deployment flag, OIDC configured/unconfigured state, callback URL, MCP auth requirement, deployment safety status, and redacted env state
- scheduler architecture and distributed-support status
- scheduler runtime mode, storage backend, raw/valid scheduler env state, dispatch safety flags, worker heartbeat, due task count, active/expired leases, recent dispatch/success/failure, and orphan recovery count
- provider availability
- SMTP/webhook availability and explicit SMTP channel state
- notification delivery health
- scheduler health

Use `/app/settings` for the same information in the product UI.

When `LOG_FORMAT=json`, request logs are structured JSON and include:

- `request_id`
- `method`
- `path`
- `status_code`
- `duration_ms`
- `client_host`

Provider secrets, SMTP passwords, OIDC client secrets, and session secrets are not included in readiness responses or logs.

## Strict Environment Validation

Set `XRML_STRICT_ENV_VALIDATION=true` to fail startup on blocking deployment errors. Current blocking errors include:

- runtime storage is not writable
- `XRML_ENVIRONMENT=staging|production` or `XRML_SHARED_DEPLOYMENT=true` with `AUTH_MODE=disabled`
- OIDC issuer or app base URL that is not an absolute http(s) URL
- shared/staging/production OIDC with non-HTTPS `APP_BASE_URL`
- shared/staging/production OIDC with non-HTTPS `OIDC_ISSUER`
- shared/staging/production OIDC with disabled secure cookies on HTTPS
- weak placeholder `SESSION_SECRET` in shared/staging/production
- invalid `TOPIC_SCHEDULER_MODE` in shared/staging/production
- invalid `SCHEDULER_MODE` / scheduler runtime backend mode in shared/staging/production
- unavailable scheduler runtime backend in shared/staging/production, including missing SQLite DB access for `db_backed` or `queue_backed`
- requested distributed scheduler mode in shared/staging/production while `multi_node_dispatch_safe=false`
- external scheduler mode in shared/staging/production without a running worker heartbeat

Warnings do not fail startup. Examples:

- SMTP is not configured
- OpenAI key is missing and fallback mode is active
- embedded scheduler mode is single-node only
- MCP OAuth is not ChatGPT-ready because `OIDC_AUDIENCE` is missing
