# Current v2 Research Workspace architecture rebaseline

## Overall verdict

`635380c7280d704bcc4c9ccf15e24b412323e8d9` remains the owner-approved historical M0 visual/information-architecture baseline. It was not a feature-parity baseline. M1 implementation authority `3c4dd21b4505d94463cd15fb38857c54812719e3` (tree `0df0675d486cd1e6a5c75edbccb280662731c11a`) is now adopted for the bounded Native Research Workspace Foundation.

Owner-approved direction:

> Preserve the topic-centric five-tab information hierarchy; recover checkpoint behavior and tests one vertical slice at a time behind facades; extend existing FastAPI/SQLite/report/evidence/Provider contracts; retain the Tauri host after its bounded M1 feasibility and D-drive-only qualification passed.

Do not revert wholesale to legacy UI, and do not rewrite the backend or frontend wholesale.

## Authority model

| Role | Identity/status |
| --- | --- |
| Adopted M1 implementation authority | branch `release/post-canonical-playwright-truth-pass-notification-empty-state-repair-v1`; commit `3c4dd21b4505d94463cd15fb38857c54812719e3`; tree `0df0675d486cd1e6a5c75edbccb280662731c11a`; current truth is its docs-only successor |
| M1 desktop-host foundation | `8e967ae96553c7fdee9f24b9d90ca49bc2cf481c`, tree `4ea8ed19804c434cb4bbd50ac7ad49656c41d465`; direct parent of `3c4dd21b` |
| M0 predecessor | `634a5967ae63697343ce89ff23f7ea070326f72f`, tree `b9de477bc2cc8adf850d7e86a83238d52c84a550`; fast-forward ancestor of M1 |
| Historical pre-M1 v2 authority | `635380c7280d704bcc4c9ccf15e24b412323e8d9`, tree `1903fb555147d33f657f1c6e9ad09c4c3066f1f5` |
| Historical pre-v2 baseline | `757af0d8caa59feab595d8fec31344ee31ccba42`, tree `23d7735aa96dd5daf20fecff54811b1dbbf38e01` |
| Verified donor checkpoint | `6c2c9cb4aca242bd1d7e7480bd8f221946b8d114`, tree `9e4a555b8e16eaf4d475abe46069a9596e3315e2`, direct child of 757 and ancestor of current |
| Donor classification | `VERIFIED_V2_LEGACY_FUNCTION_RECOVERY_CHECKPOINT` |
| Proven Provider authority | `d060383a441308f842e1e853c5bb2d1912e79a3a`, tree `17daf2011d7f1293dfef30988dd37af69d23fd80`; live acceptance preserved |
| Repair / canonical | `fa29a0f24827006bf647ff1bb042a8d2cf0e0844` / `131d909cfcfe0af9140664277f484783cbac52bb` |

The 40_REF Project Source set is the immediate historical predecessor. Owner approval authorizes a 41_REF M1 package after the adoption docs commit; package generation remains D-drive-only and Project UI application remains owner-manual.

Historical `INITIAL_PROTOTYPE_FEATURE_GOALS_COMPLETE_LOCAL_SCOPE` and the bounded Codex live acceptance at `d060383a` remain preserved; neither proves current v2 UI parity.

## M1 adopted implementation delta

- Exact feature range: `634a5967ae63697343ce89ff23f7ea070326f72f` → `8e967ae96553c7fdee9f24b9d90ca49bc2cf481c` → `3c4dd21b4505d94463cd15fb38857c54812719e3`; two commits, zero merge commits.
- The Tauri host and packaged FastAPI sidecar are implemented and locally qualified on Windows. React/Vite, FastAPI, SQLite, `XRML_DATA_DIR`, same-origin API/session behavior and Provider credential boundaries remain reused.
- System, Light, Dark, Solarized Light and Solarized Dark themes plus persisted appearance, density and motion behavior are implemented.
- Compatibility routes now preserve and disclose route intent instead of silently discarding it; unsupported sub-workflows remain explicit milestone-bound unavailable states.
- Loading, error, empty and unavailable resources are distinct. Fake/inert controls in the M1-touched surface are wired, relabelled, disabled or removed.
- The deterministic M1 mapping delta covers all 105 baseline rows: 77 touched, 0 unresolved. This is M1 closure only; `m2_complete=false`, `m3_complete=false`, `m4_complete=false`.
- D-drive-only requalification passed with one semantic native smoke, three product starts, zero retry and task-owned C-drive delta `0`.

## Historical M0 current-v2 truth baseline

- Default route resolves to the preferred topic’s Dashboard.
- Actual screen surface is five components: Dashboard, Papers, Insights, Reports and Settings.
- 21 routes are declared; 13 compatibility routes collapse into the five views while discarding `view`, `action`, `section`, `collection` or object IDs.
- Only `query` and `provider` are consumed by v2.
- Checkpoint UI referenced 131 API functions; current v2 references 21; at least 112 formerly reachable API functions survive without an active UI.
- Current unit inventory is five files; the v2 view suite has three tests. Current `web/tests/visual` is empty.
- The documented visual pass is a layout comparison, not functional parity evidence.

### Historical M0 UI data-state result

| State | Count |
| --- | ---: |
| `REAL_BACKEND_BOUND` | 25 |
| `REAL_DERIVED_BUT_NOT_PERSISTED` | 3 |
| `PARTIAL_CONTRACT` | 32 |
| `STATIC_OR_MOCK` | 26 |
| `BACKEND_EXISTS_NOT_WIRED` | 7 |
| `NEW_CONTRACT_REQUIRED` | 5 |
| `INTENTIONALLY_UNAVAILABLE` | 7 |
| Unresolved | **0** |
| Total | **105** |

Real/derived-to-static ratio is 28:26. That does not imply parity; partial and hidden capabilities dominate.

## Reuse estimate

Forty-two major capability records were scored by importance (160 points) and class reuse factor. Effective reusable foundation is **106.25/160 = 66.4%**.

| Reuse class | Count |
| --- | ---: |
| `KEEP_AS_IS` | 5 |
| `KEEP_BEHIND_FACADE` | 10 |
| `REFACTOR_INCREMENTALLY` | 10 |
| `EXTEND_EXISTING_CONTRACT` | 8 |
| `REPLACE` | 3 |
| `RETIRE` | 2 |
| `NEW_REQUIRED_CAPABILITY` | 4 |

Backend/persistence reuse is high. Active frontend behavioral parity is low. The reusable frontend asset is primarily information hierarchy, composition and selected state-handling—not the current fake/inert affordances.

## Top assets to preserve

1. FastAPI app factory, same-origin built SPA serving and health/readiness.
2. Run repository, immutable artifacts/events and lineage.
3. Review/full-text/evidence services and evidence search/detail.
4. Immutable report versions, diff/export and transaction/recovery.
5. Provider registry, strict schema/evidence validation, explicit acceptance and deterministic fallback.
6. Job lease/fencing, scheduler idempotency and notification availability truth.
7. Prototype snapshots, Living Topics, gaps/recommendations and bundle contracts behind facades.
8. Current v2 topic sidebar/five-tab hierarchy and responsive composition.
9. Existing path-safety, atomic-write and SQLite integrity/migration primitives.
10. Checkpoint behavior/test history as a donor.

## Historical M0 parity gaps and current remaining work

1. New run, run detail and run comparison lose IDs/context.
2. Evidence provenance and full-text review UI are gone.
3. Report detail, versions, diff, editor, citations and Provider candidate/acceptance are gone.
4. Recommendation decision/promotion and Living Topic workflows are gone.
5. Bundle import/export and admin legacy-run workflows are gone.
6. Schedule CRUD/run-now/recovery and notification delivery history are missing.
7. Paper detail, bookmark, collection, real selection-to-report and pagination are missing.
8. Compatibility routes present a destination but discard user intent.
9. Resource failure can be converted to empty or a blank body.
10. Frontend behavior/visual regression coverage was deleted rather than migrated.

## Historical M0 fake/mock risks

1. Papers year/citation/impact/OA filters, pagination, save/export/bookmark controls are inert.
2. “Add to report” ignores selected paper IDs.
3. Every paper is labelled core/Open Access without a qualifying field.
4. Citation unknown is shown as zero; raw relevance score is labelled influence.
5. Insight maturity/benchmark/industry labels are unrelated proxy values.
6. Topic graph includes index-generated edges rather than observed relationships.
7. Report pages, performance and popularity are synthetic formulas with misleading names.
8. Four report templates and executive summary all trigger the same generic action.
9. Theme/density/animation/chart/language/time-format settings do not persist or often do nothing.
10. Team/roles are hard-coded and must be removed until a real identity/authorization contract exists.

Every static/mock element has a migration, disable or removal disposition in `02_UI_TO_DOMAIN_MAPPING__CURRENT_V2.md`.

## Desktop-host decision and M1 result

| Option | Score | Decision |
| --- | ---: | --- |
| Tauri | **89.5/100** | Adopted after the M1 feasibility spike passed |
| Electron | **84.7/100** | Only fallback if a named Tauri disqualifier occurs |
| Native-widget rewrite | **56.6/100** | Rejected |

Implemented sidecar model: Tauri owns one packaged FastAPI child, binds an ephemeral loopback-only port, passes `XRML_DATA_DIR`, gates webview navigation on `/health/ready`, serves the built SPA at `/app`, preserves same-origin session/API behavior and performs bounded graceful shutdown/orphan/listener/SQLite-sidecar checks.

## Theme verdict after M1

The historical M0 baseline was dark-only with about 20 v2 variables and 139 hex literals. M1 replaced that color authority with semantic tokens and implemented the five approved themes, persisted appearance/density/motion preferences, OS-following System mode, focus-visible treatment, contrast coverage and reduced-motion behavior.

Target: System/Light/Dark/Solarized Light/Solarized Dark; Blue/Cyan/Violet/Green/Amber/System Accent; persisted `WorkspaceAppearancePreferences`; OS tracking; high-contrast/reduced-motion; non-color active state; accessible chart palette; desktop chrome sync.

## Long-term Research Memory

Extend the current repositories into five layers and 32 target entities:

- Bibliographic: canonical Paper/PaperVersion and normalized authors/institutions/venue/dataset/method/topic membership.
- Evidence: claims, evidence spans, results, limitations, contradictions, recommendations and report sections.
- Temporal: TopicSnapshot, ChangeEvent, metric series, clusters, consensus and contradiction state.
- Decision: insights/acceptance, gaps, actions and user decisions.
- Report: CorpusManifest, ReportPlan, candidate/version, SynthesisJob and SynthesisShard.

Eighteen have current equivalents/precursors and are extended; 14 are new; zero existing authority stores are wholesale replaced. Use typed SQLite/junction tables plus immutable filesystem artifacts, dual-read facades and digest comparison. Do not default to a graph database.

## Large-corpus synthesis

Required staged flow:

`Reviewed papers → frozen CorpusManifest → deterministic shard plan → validated SynthesisShards → cluster summaries → evidence/contradiction matrix → section synthesis → cross-section checks → candidate → schema/evidence validation → explicit acceptance → one immutable ReportVersion`.

Reuse run-job lease/fencing, deterministic reporting, Provider routing/output validation, explicit acceptance and report transaction/recovery. A shard output can never become an accepted report directly. Resume only unchanged retryable/missing shards; schema/evidence failure is terminal until an explicit repair/new candidate.

## M1 completed local-Windows scope

1. Tauri sidecar feasibility spike with exact Electron fallback criteria.
2. Semantic theme/appearance persistence and accessibility baseline.
3. Split shell/workspace into independent resource controllers.
4. Restore compatibility route identity/intent without restoring legacy visuals.
5. Remove or explicitly disable every fake metric/control.
6. Port checkpoint behavior characterization and new five-screen visual regression.

All six bounded M1 items above are implemented and locally qualified at `3c4dd21b`. M1 still excludes paper-schema completion, temporal insights, large-corpus execution, updater/tray/signing and external connectors.

## Owner-approved decisions (D1–D10)

1. `635380c7` is the current v2 implementation authority; its docs-only successor is current repository truth.
2. Tauri is recommended subject to an M1 spike; Electron is fallback after a named disqualifier; native rewrite is rejected by default.
3. The topic-centric Dashboard, Papers, Insights, Reports and Settings workspace is approved.
4. The semantic theme and accessibility contract is approved.
5. SQLite/repository-backed Research Memory extensions are approved; no graph database is approved by default.
6. Manifested, resumable and acceptance-separated large-corpus synthesis is approved; a single full-corpus prompt is rejected.
7. Legacy behavior and tests are ported incrementally; no removal occurs before parity and removal gates pass.
8. The bounded seven-path M0 docs-only scope is approved.
9. 40_REF and the 11/12 current Project Sources are approved.
10. M1 was approved and completed only as `NATIVE_RESEARCH_WORKSPACE_FOUNDATION`; M2, M3 and M4 remain not started.

## Implementation boundary

The architecture and D1–D10 decisions remain approved. M1 is implemented, locally qualified and adopted by fast-forward only at `3c4dd21b`. Tauri host/sidecar and semantic workspace foundation are implemented; Long-term Research Memory and Large-Corpus Synthesis are not. Production, staging and external beta remain no-go.
