# Environment Reference

Verified baseline date: 2026-05-07 (Asia/Seoul)

This document is the deployment environment contract for the AI Research Agent. It documents required variables, optional integrations, local defaults, production requirements, and unsafe combinations. Secret values must be supplied by the deployment platform and must not be committed.

For the concise production activation checklist and exact secret-safe preflight commands, see `docs/PRODUCTION_ACTIVATION.md`. For the variable-by-variable production contract, see `docs/PRODUCTION_ENVIRONMENT.md`.

## Release Classification Contract

| Capability | Classification when configured | Classification when env/config is missing | Truthful boundary |
| --- | --- | --- | --- |
| OIDC configured state | `FULLY_REAL` when `AUTH_MODE=oidc`, issuer/client/callback/session env validate, and strict shared deployment checks pass. | `INTENTIONALLY_GATED` / unavailable when required OIDC env is missing; `unsafe_production_disabled` when shared production uses `AUTH_MODE=disabled`. | Missing or invalid OIDC config must never be described as production ready. Secret values are represented only as missing/blank/set. |
| SMTP configured state | `FULLY_REAL` when SMTP host/from/port/auth/TLS config validates and real attempts/logs distinguish success from failure. | `INTENTIONALLY_GATED` / unavailable when SMTP host/from env is missing; invalid config and delivery failures remain explicit unavailable/failure states. | Missing SMTP env must show `unavailable_missing_env` and must never report fake send success. |
| Production activation checks | `FULLY_REAL` when `scripts/check-production-activation.ps1` or `python -m app.jobs.production_activation_check` evaluates current env and exits with truthful status. | Missing requested OIDC/SMTP config returns blocked/unavailable status, not success. | Checks are preflight validation only. They do not provision secrets, contact SMTP/OIDC unless optional smoke flags are requested, or print secret values. |
| Release validation profile | `FULLY_REAL` when `scripts/validate-release.ps1 -WithPlaywright` completes and writes `validation-summary.json` with CI-like step/status evidence. | Missing optional SMTP/OIDC env remains an expected warning unless activation checks explicitly require them. | Release validation does not require git and does not make missing SMTP/OIDC env a readiness claim. System settings and guide may display the latest summary/status/screenshot evidence without exposing secrets. |
| Cross-node distributed scheduler | Not implemented in this release. | `INTENTIONALLY_GATED`. | `db_backed` and `queue_backed` are shared-SQLite worker/process modes only; no cross-node scheduler readiness claim is allowed. |
| Workspace/team scoping | Not implemented in this release. | Deferred / `INTENTIONALLY_GATED`. | The product remains a single-workspace operations app with OIDC run ownership, not a workspace/team membership model. |
| Semantic PDF diff | Not implemented in this release. | Deferred / `INTENTIONALLY_GATED`. | Binary export artifacts compare by metadata/hash only; no semantic PDF equivalence or extracted-text diff is claimed. |

## Runtime Profiles

| Profile | Status | Required shape | Safe scheduler modes |
| --- | --- | --- | --- |
| Local development | `FULLY_REAL` | `AUTH_MODE=disabled`, localhost `APP_BASE_URL`, repo-local file/SQLite storage, optional/unavailable SMTP allowed. | `single_node_file_backed` or `db_backed`; `queue_backed` can be used for local queue verification when the SQLite scheduler DB is writable. |
| Single-node production | `FULLY_REAL` | OIDC auth unless explicitly internal, durable `XRML_DATA_DIR`, strict env validation, built `web/dist`, one FastAPI service serving API and SPA. | `db_backed` or `queue_backed` are the supported production scheduler backends; the worker can run on the same host/process boundary. |
| Shared-SQLite worker/process mode | `FULLY_REAL` | All scheduler workers/processes share the same scheduler SQLite DB path and artifact storage boundary. | `db_backed` or `queue_backed`; competing workers/processes are allowed, but this is not cross-node distributed scheduling. |
| Cross-node distributed mode | `INTENTIONALLY_GATED` | Unsupported until a distributed DB or external durable queue, durable lock/lease semantics, shared artifact storage, and node-safe queue acknowledgement/recovery exist. | None in this release; `SCHEDULER_DISTRIBUTED=true` must report unsupported/degraded. |

## Required Local Defaults

| Variable | Local value | Notes |
| --- | --- | --- |
| `AUTH_MODE` | `disabled` | Local/internal single-user mode only. |
| `APP_BASE_URL` | `http://127.0.0.1:8000` | Backend/API origin. |
| `WEB_APP_BASE_PATH` | `/app` | SPA base path. |
| `TOPIC_SCHEDULER_MODE` | `embedded` | Starts app-local scheduler lifecycle. |
| `SCHEDULER_MODE` | `single_node_file_backed` | File-backed scheduler runtime. |
| `XRML_ENVIRONMENT` | `local` | Not treated as shared deployment. |
| `XRML_DEPLOYMENT_PROFILE` | `single-service` | Local all-in-one profile. |

## Required Shared/OIDC Deployment Variables

| Variable | Requirement | Exposed in status |
| --- | --- | --- |
| `AUTH_MODE` | Must be `oidc`. | Value only. |
| `SESSION_SECRET` | High-entropy production secret, preferably 32+ chars. | State only: missing/blank/set plus strength boolean. |
| `APP_BASE_URL` | Public absolute HTTPS URL. | Public URL. |
| `WEB_APP_BASE_PATH` | Usually `/app`. | Public path. |
| `OIDC_ISSUER` | Absolute HTTPS issuer URL. | State plus validity, not token data. |
| `OIDC_CLIENT_ID` | IdP client id. | State only. |
| `OIDC_CLIENT_SECRET` | IdP client secret. | State only. |
| `XRML_DATA_DIR` | Durable persistent storage root. | Path and writability. |
| `XRML_STRICT_ENV_VALIDATION` | `true` for shared/prod/staging. | Value/state. |
| `COOKIE_SECURE` | `true` for HTTPS deployments. | Boolean. |
| `TRUST_PROXY_HEADERS` | `true` behind a trusted HTTPS proxy. | Boolean. |

## Optional Integrations

| Variable | Enables | Safe missing behavior |
| --- | --- | --- |
| `OIDC_AUDIENCE` | MCP bearer-token audience validation and ChatGPT-ready MCP status. | MCP shows not ChatGPT-ready. |
| `OIDC_SCOPES` | Requested OIDC login scopes, default `openid profile email`. | Uses repo default. |
| `OPENAI_API_KEY` | Live OpenAI model-backed analysis/reporting. | Provider status shows fallback-only. |
| `OPENAI_MODEL` | OpenAI model selection. | Uses repo default. |
| `OPENAI_API_BASE` | Alternate OpenAI-compatible endpoint. | Uses OpenAI default. |
| `NOTIFICATION_EMAIL_SMTP_HOST` | SMTP email delivery. | Email channel unavailable. |
| `NOTIFICATION_EMAIL_FROM` | SMTP sender address. | Email channel unavailable. |
| `NOTIFICATION_EMAIL_SMTP_PORT` | SMTP port. | Defaults to `587`. |
| `NOTIFICATION_EMAIL_SMTP_USERNAME` | SMTP auth username. | Optional unless password is set. |
| `NOTIFICATION_EMAIL_SMTP_PASSWORD` | SMTP auth password. | Optional unless username is set. |
| `NOTIFICATION_EMAIL_SMTP_TLS` | SMTP STARTTLS. | Defaults enabled unless SSL is enabled. |
| `NOTIFICATION_EMAIL_SMTP_SSL` | SMTP SSL. | Defaults disabled. |
| `NOTIFICATION_WEBHOOK_ENABLED` | Webhook delivery availability. | Defaults enabled; disabled state is explicit. |
| `ADMIN_USER_IDS` | Admin allowlist by product user id. | Empty allowlist. |
| `ADMIN_USER_EMAILS` | Admin allowlist by OIDC email. | Empty allowlist. |

## Scheduler Variables

| Variable | Supported values | Notes |
| --- | --- | --- |
| `TOPIC_SCHEDULER_MODE` | `embedded`, `external`, `disabled` | Scheduler lifecycle mode. External requires a worker process. |
| `SCHEDULER_MODE` | `single_node_file_backed`, `db_backed`, `queue_backed` | Scheduler persistence/dispatch backend. |
| `TOPIC_SCHEDULER_RUNTIME_MODE` | Same as `SCHEDULER_MODE` | Compatibility alias. |
| `TOPIC_SCHEDULER_BACKEND` | Same as `SCHEDULER_MODE` | Compatibility alias. |
| `TOPIC_SCHEDULER_ENABLED` | boolean | Enables/disables automatic scheduling. |
| `TOPIC_SCHEDULER_TICK_SECONDS` | positive number | Worker loop interval. |
| `SCHEDULER_DISTRIBUTED` | `false` only for this release | `true` is reported as unsupported/degraded. |
| `SCHEDULER_DISTRIBUTED_MODE` | empty | Reserved until cross-node distributed scheduling exists. |

## Storage Contract

Runtime artifacts, reports, notifications, sessions, and scheduler state require durable storage.

| Storage item | Location |
| --- | --- |
| Topic profiles | `${XRML_DATA_DIR}/topic_profiles/profiles.json` when configured, otherwise repo `storage/topic_profiles/profiles.json`. |
| File-backed schedules | `${XRML_DATA_DIR}/topic_schedules` when configured, otherwise repo `storage/topic_schedules`. |
| Runs and report artifacts | `${XRML_DATA_DIR}/runs` when configured, otherwise repo `storage/runs`. Preserve each `runs/<run_id>` directory as a unit. |
| Run events and review actions | `${XRML_DATA_DIR}/runs/<run_id>/events.jsonl`, `${XRML_DATA_DIR}/runs/<run_id>/paper_review_actions.json`, and `${XRML_DATA_DIR}/runs/<run_id>/reviews`. |
| Report versions | `${XRML_DATA_DIR}/runs/<run_id>/reports/versions` when configured, otherwise repo `storage/runs/<run_id>/reports/versions`. |
| Export artifact metadata | `${XRML_DATA_DIR}/runs/<run_id>/reports/exports.json` when configured, otherwise repo `storage/runs/<run_id>/reports/exports.json`. |
| Provider artifacts | `${XRML_DATA_DIR}/runs/<run_id>/raw` plus normalized run/triage metadata, otherwise repo `storage/runs/<run_id>/raw`. |
| Analytics artifacts | `${XRML_DATA_DIR}/topic_analytics` when configured, otherwise repo `storage/topic_analytics`. |
| Metadata index | `${XRML_DATA_DIR}/metadata_index.sqlite3` when configured, otherwise repo `storage/metadata_index.sqlite3`. |
| Scheduler DB and queue state | `${XRML_DATA_DIR}/scheduler.sqlite3` when configured, otherwise repo `storage/scheduler.sqlite3`. |
| Notification records | `${XRML_DATA_DIR}/topic_notifications` when configured, otherwise repo `storage/topic_notifications`. |
| Product sessions | `${XRML_DATA_DIR}/auth/product_sessions.json` when configured, otherwise repo `storage/auth/product_sessions.json`. |
| Release validation artifacts | `output/release-validation`, `output/playwright`, and `output/packages`. These are audit/package artifacts, not required for runtime backup. |
| Playwright truth artifacts | `output/playwright/truth-pass`. These are release evidence artifacts, not required for runtime backup. |

Shared/staging/production profiles must set `XRML_DATA_DIR`. Repo-local storage is allowed for local development only.

Readiness validates data-root and runs-root directory creation, read/write access, same-directory atomic replace support, SQLite parent writability, key path reporting, and Windows long-path risk. See `docs/STORAGE_PORTABILITY.md` for backup/restore guidance.

## Unsafe Combinations

| Combination | Runtime behavior |
| --- | --- |
| Shared/prod/staging with `AUTH_MODE=disabled` | Not ready; strict validation blocks startup. |
| `AUTH_MODE=oidc` without OIDC issuer/client/session settings | Auth config validation fails. |
| Shared/prod/staging without `XRML_DATA_DIR` | Not ready; strict validation blocks startup. |
| HTTPS `APP_BASE_URL` with insecure cookies | Not ready for shared deployment. |
| Invalid `TOPIC_SCHEDULER_MODE` | Degraded/read-only state or strict startup block. |
| Invalid `SCHEDULER_MODE` | Degraded legacy read-only state or strict startup block. |
| Shared-SQLite worker/process mode without a shared scheduler DB path | Unsafe topology; workers may not compete over the same lease/queue state. Configure a shared durable `XRML_DATA_DIR` first. |
| `SCHEDULER_DISTRIBUTED=true` | Unsupported/degraded; no cross-node scheduling claim. |
| SMTP username without password, or password without username | Email readiness is `invalid_config`. |
| SMTP TLS and SSL enabled together | Email readiness is `invalid_config`. |

## Runtime Status

Operators should verify:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -Check
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/settings/status
```

The production activation check is a server-start-independent preflight for OIDC/SMTP/runtime env. The readiness and settings payloads expose a `deployment.config_health` object after startup. Secret variables are exposed only as `missing`, `blank`, or `set` states.

## Canonical Startup Commands

| Need | Command |
| --- | --- |
| Local development | `powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1` |
| Local development dry-run | `powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1 -DryRun -SkipInstall` |
| Production build | `cd web; npm run build` |
| Production activation preflight | `powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc` |
| Production activation preflight with SMTP required | `powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc -RequireSmtp` |
| Optional OIDC discovery smoke | `powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc -CheckOidcDiscovery` |
| Optional SMTP connectivity smoke | `powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -RequireSmtp -CheckSmtpConnectivity` |
| Production-like backend/static SPA | `powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1` |
| Production-like dry-run | `powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -DryRun -SkipBuild` |
| Scheduler worker | `powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1` |
| Queue-backed scheduler worker | `powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed` |
| Worker preflight | `powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -Check` |
| Schedule lock preflight | `powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -LockCheck -RecoverStaleLock` |
| Release validation | `powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright` |
| Playwright truth pass against running app | `uv run --with playwright python tests/playwright_dashboard_truth.py --base-url http://127.0.0.1:5173 --output-dir output/playwright/truth-pass` |

Release validation logs are written to `output/release-validation/<timestamp>` by default and summarized in `validation-summary.json`. See `docs/RELEASE_VALIDATION.md` for supported flags such as `-SkipPlaywright`, `-KeepServers`, `-BaseUrl`, `-OutputLogDir`, and `-ScreenshotDir`.
