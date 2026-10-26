# Production Environment Contract

Verified baseline date: 2026-05-05 (Asia/Seoul)

This document is the operator-facing environment contract for production activation. It lists exact variable names, required states, optional integrations, and unsafe combinations. Do not commit real secrets. Use deployment platform secrets, machine-level environment variables, or another secret manager.

## Release Classification Contract

| Capability | Configured classification | Missing/unavailable classification | Operator rule |
| --- | --- | --- | --- |
| OIDC configured state | `FULLY_REAL` when `AUTH_MODE=oidc`, `SESSION_SECRET`, `APP_BASE_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and `OIDC_CLIENT_SECRET` validate. | `INTENTIONALLY_GATED` / unavailable when required env is missing; unsafe when shared production uses disabled auth. | Do not mark shared/production auth ready until the activation preflight and readiness status both report configured/safe state. |
| SMTP configured state | `FULLY_REAL` when SMTP host/from/port/auth/TLS config validates and delivery attempts are logged truthfully. | `INTENTIONALLY_GATED` / unavailable when SMTP host/from env is missing; `invalid_config`, `connection_failed`, and `delivery_failed` remain non-ready states. | Do not mark email delivery active without valid SMTP env and a real successful attempt/log. |
| Production activation checks | `FULLY_REAL`; scripts and Python module evaluate the same backend config/readiness builders without exposing secrets. | Requested missing OIDC/SMTP config returns blocked/unavailable exit status. | Use activation checks before startup; they validate but do not create credentials. |
| Cross-node distributed scheduler | Not available. | `INTENTIONALLY_GATED`. | Current DB/queue scheduler modes are safe only for processes sharing one SQLite scheduler DB and storage boundary. |
| Workspace/team scoping | Not available. | Deferred / `INTENTIONALLY_GATED`. | Do not expose or document workspace/team membership as part of this release. |
| Semantic PDF diff | Not available. | Deferred / `INTENTIONALLY_GATED`. | Only metadata/hash binary artifact comparison is real. |

## Required For Local Development

| Variable | Safe local value | Notes |
| --- | --- | --- |
| `AUTH_MODE` | `disabled` | Local/internal single-user mode only. |
| `APP_BASE_URL` | `http://127.0.0.1:8000` | FastAPI origin. |
| `WEB_APP_BASE_PATH` | `/app` | Built SPA path when served by FastAPI. |
| `XRML_ENVIRONMENT` | `local` | Prevents shared-production safety gates from treating local mode as production. |
| `SCHEDULER_MODE` | `single_node_file_backed` | Local default. `db_backed` and `queue_backed` are also supported when SQLite storage is writable. |

## Required For Shared Production OIDC

| Variable | Requirement | Secret handling |
| --- | --- | --- |
| `AUTH_MODE` | Must be `oidc`. | Not secret. |
| `SESSION_SECRET` | High-entropy value, preferably 32+ characters. | Secret; status exposes only missing/blank/set and strength. |
| `APP_BASE_URL` | Public absolute HTTPS origin. | Public URL. |
| `WEB_APP_BASE_PATH` | Usually `/app`. | Public path. |
| `OIDC_ISSUER` | Absolute HTTPS issuer URL. | Public issuer URL/state. |
| `OIDC_CLIENT_ID` | IdP client id. | State only in activation output. |
| `OIDC_CLIENT_SECRET` | IdP client secret. | Secret; never returned to frontend. |
| `OIDC_AUDIENCE` | Optional for web login, required for ChatGPT-ready scoped MCP bearer validation. | Public/audience identifier. |
| `OIDC_SCOPES` | Optional requested login scopes, default `openid profile email`. | Public scope list. |
| `XRML_ENVIRONMENT` | `production`, `staging`, or another shared env name. | Not secret. |
| `XRML_SHARED_DEPLOYMENT` | `true` for explicit shared deployment. | Not secret. |
| `XRML_STRICT_ENV_VALIDATION` | `true` to block unsafe startup. | Not secret. |
| `COOKIE_SECURE` | `true` for HTTPS deployments. | Not secret. |
| `TRUST_PROXY_HEADERS` | `true` only behind a trusted HTTPS proxy. | Not secret. |
| `XRML_DATA_DIR` | Durable writable storage root. | Path is exposed in operator status. |

## SMTP Email Delivery

| Variable | Requirement | Safe missing behavior |
| --- | --- | --- |
| `NOTIFICATION_EMAIL_SMTP_HOST` | Required for email availability. | Email state is `unavailable_missing_env`. |
| `NOTIFICATION_EMAIL_SMTP_PORT` | Numeric port, default `587`. | Invalid value reports `invalid_config`. |
| `NOTIFICATION_EMAIL_FROM` | Required sender email address. | Missing keeps email unavailable; invalid format reports `invalid_config`. |
| `NOTIFICATION_EMAIL_SMTP_USERNAME` | Required only when the SMTP server requires auth. | Must be paired with password. |
| `NOTIFICATION_EMAIL_SMTP_PASSWORD` | Required only when username is set. | Secret; never returned to frontend. |
| `NOTIFICATION_EMAIL_SMTP_TLS` | STARTTLS flag. | Defaults to enabled unless SSL is enabled. |
| `NOTIFICATION_EMAIL_SMTP_SSL` | SSL flag. | Mutually exclusive with TLS. |

Legacy aliases such as `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USERNAME`, and `SMTP_PASSWORD` are accepted by the backend, but the canonical production contract uses the `NOTIFICATION_EMAIL_*` names above.

SMTP readiness is reported without secret values as:

| State | Meaning |
| --- | --- |
| `available` | Host/from/port/auth/TLS settings validate; real send/test attempts may run. |
| `unavailable_missing_env` | Host or from address is missing; email stays unavailable and no fake send success is reported. |
| `invalid_config` | Port/from/auth-pair/TLS/SSL config is invalid. The channel remains unavailable. |
| `connection_failed` | A real SMTP attempt could not connect or the server disconnected. |
| `delivery_failed` | A real SMTP connection occurred but send/login/delivery failed. |

## Webhook Notifications

| Variable | Requirement | Notes |
| --- | --- | --- |
| `NOTIFICATION_WEBHOOK_ENABLED` | Optional boolean, defaults to `true`. | Delivery success/failure is based on real HTTP attempts and delivery records. |

Webhook destination URLs are stored through notification settings. Secret query parameters are not shown in channel labels.

## Scheduler And Storage

| Variable | Supported values | Production rule |
| --- | --- | --- |
| `TOPIC_SCHEDULER_MODE` | `embedded`, `external`, `disabled` | Use `external` when operators run a separate worker lifecycle. |
| `SCHEDULER_MODE` | `single_node_file_backed`, `db_backed`, `queue_backed` | `db_backed` and `queue_backed` require a writable shared SQLite scheduler DB path for competing local processes. |
| `TOPIC_SCHEDULER_ENABLED` | boolean | Disables automatic scheduling when false. |
| `TOPIC_SCHEDULER_TICK_SECONDS` | positive number | Worker loop interval. |
| `SCHEDULER_DISTRIBUTED` | `false` in this release | `true` is reported unsupported/degraded. |
| `SCHEDULER_DISTRIBUTED_MODE` | empty/reserved | Reserved for a future distributed backend. |
| `XRML_DATA_DIR` | path | Required for shared/staging/production storage durability. |

Current `db_backed` and `queue_backed` modes are real for workers/processes sharing the same SQLite scheduler DB and artifact storage. They are not cross-node distributed scheduling.

## Frontend/API Base URL

| Variable | Requirement |
| --- | --- |
| `APP_BASE_URL` | Backend/API public origin and OIDC callback origin. |
| `WEB_APP_BASE_PATH` | SPA mount path, usually `/app`. |
| `VITE_API_BASE_URL` | Optional dev-only Vite API override when running the frontend separately. Production FastAPI static serving is same-origin. |

## MCP

| Variable | Requirement |
| --- | --- |
| `OIDC_AUDIENCE` | Required for ChatGPT-ready scoped MCP bearer-token validation. |

MCP is optional and scoped. It does not replace product auth. In OIDC mode, protected MCP tools require bearer-token validation and scopes.

## OIDC Readiness States

| State | Meaning |
| --- | --- |
| `configured` | OIDC mode has required issuer/client/session env and callback URL config is valid. |
| `missing_env` | `AUTH_MODE=oidc` is selected but required env such as `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, or `SESSION_SECRET` is missing. |
| `invalid_config` | Required values are present but URL, cookie, HTTPS, or session-safety validation failed. |
| `disabled_dev_mode` | `AUTH_MODE=disabled` is active in a local/internal profile. |
| `unsafe_production_disabled` | Disabled auth is active in a shared/staging/production-like profile and must not be used. |

## Unsafe Combinations

| Combination | Runtime behavior |
| --- | --- |
| Shared/staging/production with `AUTH_MODE=disabled` | Not ready; strict validation blocks startup. |
| `AUTH_MODE=oidc` without `SESSION_SECRET`, `APP_BASE_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, or `OIDC_CLIENT_SECRET` | OIDC activation is blocked. |
| HTTPS `APP_BASE_URL` with `COOKIE_SECURE=false` | Unsafe for shared deployment. |
| Shared/staging/production without `XRML_DATA_DIR` | Not ready; strict validation blocks startup. |
| `SCHEDULER_MODE=db_backed` without writable shared SQLite storage | Unsafe for competing worker/process mode. |
| `SCHEDULER_MODE=queue_backed` without writable shared SQLite storage | Unsafe for queue-backed worker/process mode. |
| `SCHEDULER_DISTRIBUTED=true` | Unsupported/degraded; no cross-node claim. |
| SMTP channel enabled without host/from | Email remains `unavailable_missing_env`. |
| SMTP username without password, or password without username | Email readiness is `invalid_config`. |
| SMTP TLS and SSL both enabled | Email readiness is `invalid_config`. |

## Secret-Safe Validation Commands

Local-safe startup dry-runs:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1 -DryRun -Check -CheckScheduler
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -DryRun -Check -CheckScheduler -CheckStorage -CheckMcp
```

Configuration-only OIDC activation:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc
```

OIDC plus SMTP activation:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc -RequireSmtp
```

Optional live checks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc -CheckOidcDiscovery
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -RequireSmtp -CheckSmtpConnectivity
```

Release validation without real SMTP/OIDC remains:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright
```

Release validation with explicit production activation requirements, for environments where real OIDC/SMTP env is already supplied:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright -CheckAuth -CheckSmtp
```

The activation output reports secret-bearing variables only as `missing`, `blank`, or `set`. It must not print `SESSION_SECRET`, `OIDC_CLIENT_SECRET`, SMTP passwords, or provider API keys.
