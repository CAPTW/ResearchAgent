# ADR-0002 — Current v2 Research Workspace rebaseline and reuse-first native migration

## Status

Accepted

## Date

2026-08-26

## Context

The operational branch is `release/post-canonical-playwright-truth-pass-notification-empty-state-repair-v1`. Commit `635380c7280d704bcc4c9ccf15e24b412323e8d9` (tree `1903fb555147d33f657f1c6e9ad09c4c3066f1f5`) established the historical M0 topic-centric v2 baseline. The owner later adopted M1 implementation authority `3c4dd21b4505d94463cd15fb38857c54812719e3` (tree `0df0675d486cd1e6a5c75edbccb280662731c11a`) by fast-forward only from M0 predecessor `634a5967ae63697343ce89ff23f7ea070326f72f`.

The audited lineage is `757af0d8caa59feab595d8fec31344ee31ccba42` → `6c2c9cb4aca242bd1d7e7480bd8f221946b8d114` → `635380c7280d704bcc4c9ccf15e24b412323e8d9`. The middle commit is classified as `VERIFIED_V2_LEGACY_FUNCTION_RECOVERY_CHECKPOINT`: it is a read-only behavior and test donor, not a UI restoration target. The audit mapped 105 UI elements, classified 42 capabilities, and estimated `106.25/160 = 66.4%` capability-weighted reuse.

The initial Prototype local-scope milestone remains `INITIAL_PROTOTYPE_FEATURE_GOALS_COMPLETE_LOCAL_SCOPE`. Codex Provider live acceptance remains attributed to `d060383a441308f842e1e853c5bb2d1912e79a3a` (tree `17daf2011d7f1293dfef30988dd37af69d23fd80`). Neither statement proves current v2 UI parity. Repository `STATUS.md` remains `INTERNAL_HISTORICAL_BYTE_FROZEN_NOT_CURRENT_AUTHORITY`.

The owner approved D1–D10 by the exact token `XRML_CURRENT_V2_ARCHITECTURE_REBASELINE_APPROVED_ALL_RECOMMENDED_DECISIONS`.

## Decision

### D1 — Current v2 baseline

`635380c7280d704bcc4c9ccf15e24b412323e8d9` remains the historical pre-M1 v2 authority. `3c4dd21b4505d94463cd15fb38857c54812719e3` is the adopted `M1_NATIVE_RESEARCH_WORKSPACE_IMPLEMENTATION_AUTHORITY`; the docs-only successor containing this adoption record is current repository truth without replacing technical attribution to `3c4dd21b`.

### D2 — Desktop host

The target is Tauri plus the existing React/Vite workspace and a packaged FastAPI sidecar. Commit `8e967ae96553c7fdee9f24b9d90ca49bc2cf481c` implemented the host/sidecar foundation, and `3c4dd21b` completed M1. The bounded Tauri spike passed without a named disqualifier; Electron was not used. A native-widget rewrite remains rejected by default.

### D3 — Workspace information architecture

The topic-centric Dashboard, Papers, Insights, Reports and Settings tabs are retained. Compatibility routes remain until their IDs, query intent and navigation semantics reach parity.

### D4 — Theme and accessibility contract

The approved modes—System, Light, Dark, Solarized Light and Solarized Dark—are implemented with semantic tokens, persisted appearance/density/motion preferences, OS-following System mode, visible focus, contrast checks and reduced-motion handling. This M1 implementation does not imply M2/M3 data-contract completion.

### D5 — Long-term Research Memory

Current topic/run/review/evidence/report lineage is extended into Bibliographic, Evidence, Temporal, Decision and Report Memory. Typed SQLite/repository extensions are the baseline. A graph database is not approved by default and requires measured evidence.

### D6 — Large-Corpus Synthesis

The approved design uses `CorpusManifest`, resumable `SynthesisJob`, `SynthesisShard`, `ClusterSummary`, Evidence/Contradiction Matrix, Section Synthesis and cross-section consistency. Execution, candidate/schema validity, evidence validity, acceptance and persistence remain separate. A single full-corpus prompt architecture is rejected.

### D7 — Legacy disposition

The 42-capability audit classification is accepted. No legacy route, component or capability is deleted in M0. M1 ports behavior, controllers, state machines, route semantics and tests incrementally; retirement requires explicit parity and removal gates. The old visual layer is not restored wholesale. Static or mock controls must be wired, explicitly disabled or retired.

### D8 — M0 documentation scope

M0 is limited to README and six architecture/roadmap documents. It changes no product source, tests, workflows, lockfiles or runtime data.

### D9 — Project Source rebaseline

The M0 Project Source hierarchy used 40_REF as top factual authority and added current 11 UI-mapping and 12 reuse/disposition sources. After M1 adoption, the next bounded package uses 41_REF as top authority; 40_REF becomes the immediate historical predecessor, with 39_REF and earlier retained as older history. Package generation remains D-drive-only and UI application remains owner-manual.

### D10 — M1 starting scope

M1 is `NATIVE_RESEARCH_WORKSPACE_FOUNDATION`: Tauri feasibility, FastAPI sidecar lifecycle, desktop window and single instance, topic shell and five tabs, semantic themes, window state, compatibility routes, and truthful loading/error/empty/unavailable states. This bounded scope is implemented and locally qualified on Windows at `3c4dd21b`.

M1 excludes Paper Bank completion, Temporal Research Memory, Large-Corpus Synthesis, Gemini/Claude adapters, weighted orchestration, App OAuth/OIDC, public MCP, staging, production and external beta.

## M1 implementation and adoption status

- The exact adopted range is `634a5967ae63697343ce89ff23f7ea070326f72f` → `8e967ae96553c7fdee9f24b9d90ca49bc2cf481c` → `3c4dd21b4505d94463cd15fb38857c54812719e3`; it contains two commits and no merge commit.
- The Tauri feasibility spike passed. React/Vite, FastAPI, SQLite, `XRML_DATA_DIR`, same-origin API/session behavior and the external Provider credential boundary were retained; no broad authentication rewrite or Electron fallback was required.
- The host starts one packaged FastAPI sidecar on loopback, gates navigation on readiness, owns bounded shutdown and single-instance/window-state behavior, and exposes narrowly scoped native file-dialog commands while backend path validation remains authoritative.
- The D-drive-only requalification accepted the native host, packaged sidecar and unsigned installer artifacts with task-owned C-drive delta `0`. The unsigned installer is local validation evidence, not a signed release.
- M1 closed its semantic-theme, compatibility-route and truthful resource-state scope. It did not implement the M2 Paper Bank, M3 Temporal Research Memory or M4 Large-Corpus Synthesis.
- The current release branch was advanced to `3c4dd21b` by `FAST_FORWARD_ONLY_M1_ADOPTION_NO_MERGE_COMMIT`; the feature branch is retained at the same implementation commit.

## Consequences

- Current v2 is not treated as complete functional parity.
- Mock/static cards and controls cannot remain deceptively active.
- Backend, persistence, Provider, evidence, report and acceptance contracts are reused behind facades.
- `web/src/lib/api.ts` remains a temporary compatibility facade while domain clients are extracted progressively.
- Future changes preserve the Provider/evidence/report truth boundaries and accepted historical data.
- Graph storage remains optional and evidence-gated; SQLite is the baseline.
- The M1 Tauri host, packaged sidecar, semantic themes, compatibility routing and truthful resource states are implemented and locally qualified. Research Memory and Large-Corpus Synthesis remain planned and unimplemented.
- Production, staging, external beta, stable OSS v1.0, live GitHub Actions and Ubuntu timing parity remain unproven or no-go.

## Compatibility and rollback

Each milestone preserves existing IDs and repositories behind facades. Schema changes require backup, dual-read and digest comparison before authority cutover. A failed new slice rolls back through its facade or feature boundary without rewriting accepted reports or historical runtime data.

## Supersession rule

Replace this ADR only with a later accepted decision that names the implementation authority, current-truth commit, evidence and migration boundary. Do not silently promote a plan or Project Source package into implemented product truth.
