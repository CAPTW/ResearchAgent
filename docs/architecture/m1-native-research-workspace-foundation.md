# M1 native Research Workspace foundation

## Scope

M1 packages the existing React/Vite client and FastAPI application as an internal Windows desktop workspace. It retains the browser-hosted `/app` mode and the existing SQLite, `XRML_DATA_DIR`, Provider, evidence, report, and scheduler contracts. It does not implement the M2 paper bank, M3 temporal memory, M4 large-corpus synthesis, or M5 operations expansion.

The native architecture is intentionally small:

```text
Tauri v2 host
  -> owns one packaged FastAPI sidecar process
  -> sidecar reserves 127.0.0.1 on an OS-assigned port
  -> sidecar serves the production web build and API on one origin
  -> host accepts one structured ready event
  -> host navigates its WebView to /app
```

There is no normal external-browser launch, updater, auto-start, tray, or release-signing claim in M1.

## Windows development prerequisites

- Windows x64 with the Microsoft Edge WebView2 Runtime
- Node.js 20 and npm 10
- Python 3.12.12 and `uv`
- Rust stable with the `x86_64-pc-windows-msvc` target
- Microsoft Visual C++ build tools and Windows SDK
- the project-pinned Tauri CLI and PyInstaller versions from the committed lockfiles

Use task-local caches and build roots on `D:`. A representative PowerShell setup is:

```powershell
$env:TEMP = 'D:\XRT\XRML_M1\temp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:XRML_DATA_DIR = 'D:\XRT\XRML_M1\runtime-data'
$env:UV_PROJECT_ENVIRONMENT = 'D:\XRT\XRML_M1\python-env'
$env:UV_CACHE_DIR = 'D:\XRT\XRML_M1\uv-cache'
$env:PYTHONPYCACHEPREFIX = 'D:\XRT\XRML_M1\python-cache'
$env:npm_config_cache = 'D:\XRT\XRML_M1\npm-cache'
$env:CARGO_HOME = 'D:\XRT\XRML_M1\cargo-home'
$env:CARGO_TARGET_DIR = 'D:\XRT\XRML_M1\cargo-target'
```

Do not point `XRML_DATA_DIR` at the repository.

## Browser-compatible development

The ordinary browser workflow remains supported:

```powershell
uv sync --frozen --extra dev --extra desktop
$env:WEB_APP_BASE_PATH = '/app'
npm --prefix web ci
npm --prefix web run build
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/app`. The desktop path does not require a separate API-origin mode.

## Sidecar build and lifecycle

Build the production web client before the sidecar, because the sidecar inventory includes `web/dist`:

```powershell
$env:WEB_APP_BASE_PATH = '/app'
npm --prefix web run build
uv run --extra desktop python scripts/build_desktop_sidecar.py `
  --repository-root . `
  --output-root D:\XRT\XRML_M1\sidecar
```

The build script checks the pinned PyInstaller version, records a deterministic input inventory, and packages the production frontend. The current build uses PyInstaller **onedir**: `dist/xrml-sidecar/xrml-sidecar.exe` and its sibling `sidecar-runtime/`. No system Python is needed. Keep the directory and executable together; copying the EXE alone is no longer a complete package.

The script stages the target-triple executable and the complete runtime into the ignored Tauri `binaries/` directory. Tauri maps `binaries/sidecar-runtime/` to `sidecar-runtime/` next to the installed external executable. The V2 build receipt records both locations and a per-file runtime inventory. Staging prepares the next pair before replacement, removes stale generated dependencies, and restores the prior pair on replacement failure. Close the app before rebuilding its bundle.

For a portable local build, copy `xrml-desktop-host.exe`, rename/copy the target-triple sidecar to `xrml-sidecar.exe`, and copy the **whole** `sidecar-runtime/` directory beside them. `scripts/start_local_desktop.ps1` rejects a missing Python runtime before launching. Use a fresh D: workspace for qualification. Packaging eliminates the one-file bootloader's per-launch extraction step; it does not eliminate all filesystem or application initialization costs. The native readiness limit remains **35 seconds**, and the service still confirms completed bootstrap and usable stores before readiness. Timing evidence belongs to the current checkpoint, not this build recipe.

At launch the host passes only structured arguments: a task-owned data directory and readiness timeout. The sidecar pre-binds IPv4 loopback with port `0`, configures the existing application, waits for `/health/ready`, and emits a bounded JSON ready event. The host rejects non-loopback, non-`/app`, and external navigation. Closing or restarting the workspace sends `shutdown` to the owned child, waits briefly, and then enforces termination. Startup timeout, pre-ready termination, and post-ready crash have distinct user-visible states.

Readiness retains its HTTP 200 diagnostic envelope; consumers must inspect the
body. The top-level status is `not_ready` when required stores are unavailable or
the current application's bootstrap failed, even if old databases remain readable.
`runtime_bootstrap.status` is derived from server state (`completed`, `failed`, or
`not_observed`), never a client approval flag. Direct observation without a running
application reports `not_observed`. The sidecar requires both completed bootstrap
and usable stores before emitting `ready`; a confirmed initialization failure
emits redacted `app_startup_failure` and exits without starting workers. The native
host maps the six fatal startup codes to fixed Korean messages; unknown codes and
transport errors use a fixed fallback, without displaying raw details. The first
pre-ready failure survives that child's termination or readiness deadline, and a
late ready event from a failed child cannot navigate into the workspace. A fresh
child starts with no inherited failure.

Lifecycle state changes and native UI setter enqueue are ordered under the same
sidecar lock, including the replacement child's loading navigation. The existing
async event/deadline paths use nonblocking Tauri setters; process waits and kills
remain outside that state lock. An async operation gate serializes each restart's
stop, spawn and registration. Registration never overwrites an owned child, and
final close permanently rejects late registrations and disposes rejected children
outside the lock. Initial loading navigation does not show or focus the hidden
window. The 35-second host deadline and 1.5-second graceful wait are unchanged.
The 2026-10-02 qualification includes deterministic lifecycle/ownership contracts,
Windows debug compilation, and an actual native-window recovery journey using
that debug host plus a freshly packaged sidecar in an isolated D-drive bundle.
It observes initialization failure through child exit/deadline, keyboard retry,
post-ready worker crash, double-click retry, close shortly after retry, full host
reopen, preserved snapshot readback, and normal owned-process cleanup. This is
not NSIS installation, a signed release or exhaustive process-race qualification.

These checks do not prove a hard deadline for Python
imports or blocking startup work; the existing native outer deadline remains.
The isolated Prototype launcher also checks completed bootstrap and usable stores
before entering its research journey. A confirmed bootstrap failure produces the
fixed `runtime_bootstrap_failed` error and follows the existing owned-process
cleanup path. Its frontend HTML wait remains a reachability check.

## Native build and launch

From `desktop` with the MSVC environment loaded:

```powershell
npm ci
npm run tauri -- build --debug --no-bundle
npm run build
```

The release build produces the desktop executable and an unsigned internal NSIS installer beneath the configured Cargo target directory. These artifacts are internal development outputs only; they are not signed, released, or production qualified.

For a bounded development installer qualification, use
`npm run tauri -- build --debug --ci --bundles nsis --no-sign -- --locked --offline`
with the pinned dependencies already prepared. This produces a **debug** installer;
it does not establish release-profile or signed-distribution qualification. Cargo's
offline flag does not prevent the Tauri bundler from preparing its official NSIS
toolset. Keep its local tools directory, temporary files and installer on D:.
Install to a new task directory only after checking for an existing XRML install;
`/S /NS /D=<absolute task path>` selects silent installation without new shortcuts.
Omit `/R`, then launch the installed executable with isolated D: data/profile paths
and disabled scheduler/Provider execution. Measure first launch before full file
hashing, reopen the same workspace, and verify normal exit and retained data.

Tauri CLI 2.11.4 patches the main executable's bundle-type marker from
`__TAURI_BUNDLE_TYPE_VAR_UNK` to `__TAURI_BUNDLE_TYPE_VAR_NSS` while producing NSIS,
then restores the unpatched build output. An installed host therefore need not have
the raw build-output hash. Validate the whole expected payload with precisely that
single marker replacement, preserve both hashes, and reject every other difference.
Sidecar/runtime hashes must match directly. The 2026-10-23 installed-startup checkpoint
records fresh Rust tests, actual NSIS installation, first launch/reopen within the
unchanged 35-second limit, data integrity and test-install cleanup. Its measurements
and limitations are in `docs/roadmap/current-local-status.md`.

For the optimized release-profile equivalent, omit `--debug`:
`npm run tauri -- build --ci --bundles nsis --no-sign -- --locked --offline`.
Keep the same isolated install/reopen, exact payload comparison, data integrity
and normal cleanup checks. The 2026-10-23 `release-startup-20261023-171400` checkpoint
completed this path with installed readiness of 29.890 s first / 20.656 s reopen,
without changing the 35-second limit. Those are backend readiness observations,
not UI paint or OS-reboot measurements. Release optimization does not imply signing,
publication, a speedup over a different run, or real-model research validation.

For startup-variance investigation, retain exact binary identities and distinguish
fresh data, reused data, and WebView profile reuse. Keep diagnostic profiler runs
separate from installed-package measurements and preserve deadline misses. A warm
reopen is not an OS-cold-boot result. The follow-up `startup-variance-20261023-184800`
checkpoint identified variable native-module loading and fresh SQLite commits;
it defers JWT/crypto loading until actual OIDC verification, retaining the same
strict verifier and packaged dependencies. Local startup avoids that unused cost;
OIDC's first verification pays it instead. No database durability or timeout was
relaxed. Updated installed readiness was 19.609 s first and 2.344 s / 3.703 s reopen,
which qualifies this bounded run without proving a general speedup.

Compare old and new installers at the **same installed path**, with the same
initial disposable data/profile conditions and normal uninstall between versions.
Keep the first-start measurement before full runtime hashing; run no task build or
disk scan alongside it. A copied directory versus a freshly installed directory
also changes file-cache history and bundle metadata, so that is a separate probe,
not evidence of a version regression. The `startup-installed-20261023-212000`
checkpoint uses old/current/old ordering: all six installed launches passed, while
a separate old copied-path launch missed the external gate and completed later.
Interleaved old-sidecar `--help` runs also varied without constructing an app or DB.
These observations do not establish a unique OS cause or a universal timing bound.

The subsequent `startup-idle-20261023-214500` checkpoint observed no known host
build/test process during preflight and each external startup interval. The first
installed launch still missed 35 s and showed the native preparation-timeout page;
the later launch reached readiness in 6.641 s. Whole-payload hashing between these
launches may have warmed caches. The first launch created no data directory, so
the later one initialized fresh DBs and is not retained-DB reopen evidence. This
rejects build contention as a complete explanation, without identifying a unique
loader/import/OS cause. Sampling known host tools does not control VM workloads,
all OS background activity or cache state. Preserve the failed first result.

OS restart is optional additional qualification, not a development or use gate.
The observed build-free first-launch miss already supports targeted diagnostics
of loading, imports and early initialization under the unchanged 35-second limit.
For an optional OS-restart check, use the prepared `REBOOT-GUIDE-KO.md` and two
explicit launch wrappers in that evidence folder. The boot guard requires a new
Windows boot for the first run and the same boot for its reopen. Keep the verified
installation, separate disposable data/profile and 35-second external deadline;
do not rehash the runtime before the first measurement. The user performs Windows
Restart. No automatic restart, autostart, scheduled check or model call is needed.
If the first run fails before creating data, label the later run as fresh-DB
initialization rather than claiming persistence verification.

Record the measurement clock explicitly. The external qualification clock starts
before spawning the host and requires usable backend readiness within **35 s**.
The existing native 35-second wait is armed after sidecar registration, and the
Python 30-second readiness poll follows application construction. A later usable
dashboard does not turn a missed external deadline into a PASS. Preserve child,
listener, data/DB appearance and readiness milestones separately; database file
appearance is not a completed migration or proof of usable storage. Never relax
durability, authorization or any deadline to make an observation pass.

When only Python app inputs change, rebuild the sidecar, then use Tauri
`bundle --ci --bundles nsis --no-sign` with the same pinned release target and
configuration. This reuses the already compiled host; it is not a fresh Rust build.
Verify the new installed sidecar/runtime against their new receipt and the host
against its unchanged release bytes plus the exact NSIS marker patch. Requalify
actual startup/data retention, and record reused tests separately from new runs.

## Appearance model

M1 stores a typed preference object in local storage. Supported values are:

- theme: `system`, `light`, `dark`, `solarized-light`, `solarized-dark`
- accent: `blue`, `cyan`, `violet`, `green`, `amber`; `system` is shown as unavailable until a platform value can be proven
- density: `comfortable`, `compact`
- motion: `system`, `reduced`, `full`

`system` follows live operating-system color-scheme and reduced-motion changes. An explicit theme or motion choice overrides those changes. Semantic surface, text, border, navigation, status, focus, chart, and graph tokens drive the M1 shell. The Tauri surface receives the resolved light/dark chrome preference when supported. Visible focus, forced-colors fallback, and theme/accent contrast are automated contracts.

## Routes and truthful states

Dashboard, Papers, Insights, Reports, and Settings remain the stable topic tabs. Compatibility paths retain topic, run, and report identifiers plus `view`, `action`, `section`, `collection`, `provider`, and `query` intent. A path without an M1 screen renders an explicit compatibility state and names the owning later milestone; it is not silently redirected to an unrelated capability.

Resource state is independent for topic profile/base data and each workspace section. Loading, ready, empty, unavailable, permission denied, error, retrying, and retained-stale states are distinct. A section success cannot clear a base error, and a retry targets the resource that failed. Unsupported controls are removed or disabled with an accessible reason. No M1 control fabricates a mutation, connection, citation, Open Access status, influence metric, page count, popularity value, or selected-paper report contract.

## Historical M1 scope boundaries

- paper faceting, collections, full review/evidence workspace, and persisted selection are M2
- temporal snapshots, Research Memory, clusters, consensus, and contradictions are M3
- corpus manifests, sharded synthesis, report version/diff workspace, and new export plans are M4
- expanded notification, scheduling, and operational workflows are M5
- team RBAC, App OAuth/OIDC, public MCP, Gemini/Claude adapters, weighted orchestration, distributed scheduling, staging, production, external beta, signing, and update delivery remain out of scope

These bullets describe the original M1 scope, not the current implementation
inventory. Paper Bank, snapshot analysis and offline review-to-Journal paths have
since advanced; see [current local status](../OPEN_SOURCE_READINESS.md) for
implemented behavior, validation limits and remaining live-execution decisions.
