# Production Activation Pack

Verified baseline date: 2026-05-05 (Asia/Seoul)

This guide is the operator checklist for turning the release-gate-passed app into a production-like deployment with real OIDC and SMTP configuration. It does not add fake readiness: OIDC is ready only when the IdP/client/session env is valid, and SMTP is ready only when SMTP env validates and real delivery/test-send attempts succeed.

For the exact variable-by-variable environment contract, see `docs/PRODUCTION_ENVIRONMENT.md`.

## Release Classification Contract

| Capability | Classification |
| --- | --- |
| OIDC configured state | `FULLY_REAL` only when OIDC env validates; `INTENTIONALLY_GATED` / unavailable when env is missing; `unsafe_production_disabled` when shared deployment disables auth. |
| SMTP configured state | `FULLY_REAL` only when SMTP env validates and real attempts/logs are truthful; `INTENTIONALLY_GATED` / unavailable when env is missing. |
| Production activation checks | `FULLY_REAL`; scripts/status evaluate real config and return truthful exit codes without exposing secrets. |
| Cross-node distributed scheduler | `INTENTIONALLY_GATED`; current DB/queue scheduler modes are shared-SQLite worker/process modes only. |
| Workspace/team scoping | Deferred; no workspace/team membership model is implemented. |
| Semantic PDF diff | Deferred; binary artifacts are compared by metadata/hash only. |

## 1. Configure Secrets Outside The Repo

Do not commit `.env` files with real values. Supply secrets through the deployment platform, local secret store, or process environment.

Required for shared production OIDC:

```powershell
$env:AUTH_MODE='oidc'
$env:XRML_ENVIRONMENT='production'
$env:XRML_SHARED_DEPLOYMENT='true'
$env:XRML_STRICT_ENV_VALIDATION='true'
$env:SESSION_SECRET='<32+ char high-entropy secret>'
$env:APP_BASE_URL='https://your-public-host.example.com'
$env:WEB_APP_BASE_PATH='/app'
$env:OIDC_ISSUER='https://issuer.example.com'
$env:OIDC_CLIENT_ID='<client id>'
$env:OIDC_CLIENT_SECRET='<client secret>'
$env:OIDC_SCOPES='openid profile email'
$env:COOKIE_SECURE='true'
$env:TRUST_PROXY_HEADERS='true'
$env:XRML_DATA_DIR='D:\xrml-data'
```

Optional but required for ChatGPT-ready scoped MCP bearer validation:

```powershell
$env:OIDC_AUDIENCE='https://issuer.example.com/xrml-api'
```

Required for SMTP email availability:

```powershell
$env:NOTIFICATION_EMAIL_SMTP_HOST='smtp.example.com'
$env:NOTIFICATION_EMAIL_SMTP_PORT='587'
$env:NOTIFICATION_EMAIL_SMTP_TLS='true'
$env:NOTIFICATION_EMAIL_SMTP_SSL='false'
$env:NOTIFICATION_EMAIL_SMTP_USERNAME='<smtp user if required>'
$env:NOTIFICATION_EMAIL_SMTP_PASSWORD='<smtp password if required>'
$env:NOTIFICATION_EMAIL_FROM='research@example.com'
```

## 2. Run Secret-Safe Activation Preflight

Local development dry-run with safe activation/status checks:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-dev.ps1 -DryRun -Check -CheckScheduler
```

Production-like dry-run without requiring real external OIDC/SMTP env:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -DryRun -Check -CheckScheduler -CheckStorage -CheckMcp
```

Configuration-only production OIDC preflight:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc
```

Configuration-only OIDC + SMTP preflight:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc -RequireSmtp
```

Optional live OIDC discovery smoke, which fetches only the issuer discovery document:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -Strict -RequireOidc -CheckOidcDiscovery
```

Optional live SMTP connectivity smoke, which connects/login/noop but does not send an email:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check-production-activation.ps1 -RequireSmtp -CheckSmtpConnectivity
```

The same checks can be run through startup scripts before enabling real startup:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -DryRun -CheckAuth -CheckSmtp -CheckScheduler -CheckStorage -CheckMcp
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed -CheckActivation -CheckScheduler -CheckStorage
```

Release validation remains local-safe by default and does not require real SMTP/OIDC. To include explicit production activation checks when real environment variables are present:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright -CheckActivation
powershell -ExecutionPolicy Bypass -File scripts\validate-release.ps1 -WithPlaywright -CheckAuth -CheckSmtp
```

Exit codes:

| Exit code | Meaning |
| --- | --- |
| `0` | Requested activation checks passed. Warnings can still be present unless `-FailOnWarnings` is used. |
| `2` | Activation is blocked, unavailable, or unsafe for the requested requirements. |

The activation output contains only state such as `missing`, `blank`, or `set` for secret-bearing variables. It must not print `SESSION_SECRET`, `OIDC_CLIENT_SECRET`, SMTP password values, or API keys.

## 3. Start The App And Worker

Build and start the production-like backend/static SPA service:

```powershell
cd web
npm.cmd run build
cd ..
powershell -ExecutionPolicy Bypass -File scripts\start-prod.ps1 -SkipBuild
```

For external scheduler lifecycle:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-worker.ps1 -RuntimeMode queue_backed
```

Use `SCHEDULER_MODE=db_backed` or `SCHEDULER_MODE=queue_backed` only when every competing process shares the same SQLite scheduler DB and artifact storage path. Cross-node distributed scheduling remains intentionally gated.

## 4. Verify Runtime Status

After startup:

```powershell
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/settings/status
```

Check in `/app/settings`:

- 인증 모드 is `oidc` for shared production.
- OIDC readiness is `configured`; missing env reports `missing_env`, invalid URL/cookie/session config reports `invalid_config`, local disabled auth reports `disabled_dev_mode`, and shared disabled auth reports `unsafe_production_disabled`.
- SMTP readiness is `available`, `unavailable_missing_env`, `invalid_config`, `connection_failed`, or `delivery_failed`.
- Scheduler mode and distributed-support gate match the configured topology.
- Storage/artifact paths are writable and durable.

## 5. Smoke Real OIDC And SMTP Behavior

OIDC smoke:

1. Open `/app`.
2. Start login.
3. Complete the IdP callback.
4. Confirm protected routes load under the server-side product session.
5. Logout and confirm the session cookie is cleared.

SMTP delivery smoke:

1. Open `/app/notifications`.
2. Configure an email destination for a real topic notification rule.
3. Use test send when available, or trigger a real report-completed/digest event.
4. Confirm a delivery record is created with `attempted=true` and `attempts > 0` only when a real SMTP send was attempted.
5. Treat failed SMTP attempts as real failures; do not mark email as active until the delivery log confirms success.

## 6. Unsupported Or Conditional Areas

| Capability | Current truth |
| --- | --- |
| `AUTH_MODE=disabled` in shared deployment | Unsafe and blocked by strict validation. Local/internal only. |
| SMTP without host/from env | `unavailable_missing_env`; no fake email success. |
| SMTP with invalid port/from/auth/TLS settings | `invalid_config`; no fake email success. |
| SMTP connection or delivery failure | `connection_failed` or `delivery_failed`; delivery logs record the real failed attempt. |
| Cross-node distributed scheduler | `INTENTIONALLY_GATED`; DB/queue modes are safe only for processes sharing the same SQLite scheduler DB. |
| Workspace/team scoping | Deferred. |
| Semantic PDF diff | Deferred; binary artifacts compare by metadata/hash. |
