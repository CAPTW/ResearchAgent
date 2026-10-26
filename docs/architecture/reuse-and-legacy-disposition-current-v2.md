# Reuse and legacy disposition — current v2

Authority capsule: adopted M1 implementation authority `3c4dd21b4505d94463cd15fb38857c54812719e3`, tree `0df0675d486cd1e6a5c75edbccb280662731c11a`; desktop-host foundation `8e967ae96553c7fdee9f24b9d90ca49bc2cf481c`; M0 predecessor `634a5967ae63697343ce89ff23f7ea070326f72f`; historical pre-M1 v2 authority `635380c7280d704bcc4c9ccf15e24b412323e8d9`; preserved Provider implementation authority `d060383a441308f842e1e853c5bb2d1912e79a3a`; canonical historical baseline `131d909cfcfe0af9140664277f484783cbac52bb`. Owner approved the reuse-first disposition and adopted M1 by fast-forward only. `INITIAL_PROTOTYPE_FEATURE_GOALS_COMPLETE_LOCAL_SCOPE` remains preserved. M1 is complete for local Windows scope; Long-term Memory, Large-Corpus Synthesis, production, staging and external beta remain unimplemented or no-go.

## Verdict

`635380c7` remains the historical M0 five-view baseline, not a feature-parity rebaseline. M1 followed the approved strategy—**revive contracts, not legacy JSX**—and added a Tauri/sidecar foundation, semantic appearance system, compatibility intent handling and truthful resource states without restoring the deleted giant legacy visual layer. The remaining M2–M5 work continues behind typed facades.

## Commit range

| Range | Commits | Files/stat | Meaning |
| --- | ---: | --- | --- |
| `757af0d8..6c2c9cb4` | 1 | 24 files, +2,073/-194 | Added common `ProductShell`, shell CSS, route visual spec and 12 snapshots while retaining detailed screens. |
| `6c2c9cb4..635380c7` | 1 | 97 files, +2,758/-40,634; A11/D80/M6 | Replaced detailed frontend with five v2 views; backend and API client unchanged. |
| `757af0d8..635380c7` | 2 | 84 paths, +2,845/-38,842; A12/M6/D66 | Full audited range; zero merge commits. |

Exact commits:

1. `6c2c9cb4aca242bd1d7e7480bd8f221946b8d114`, tree `9e4a555b8e16eaf4d475abe46069a9596e3315e2`, parent `757af0d8...`, subject `chore: checkpoint unified research shell`.
2. `635380c7280d704bcc4c9ccf15e24b412323e8d9`, tree `1903fb555147d33f657f1c6e9ad09c4c3066f1f5`, subject `feat: replace legacy research UI with v2 workspace`.

Checkpoint classification:

`VERIFIED_V2_LEGACY_FUNCTION_RECOVERY_CHECKPOINT`

It is a direct current ancestor and a recoverable donor. It is **not** proof that its capabilities survived into active v2.

### Replacement details

- Deleted 16 screen source files: 10,175 lines.
- Deleted 22 component source files: 9,841 lines.
- Deleted 24 unit-test files: 10,982 lines.
- Deleted four stylesheets: 9,004 lines.
- Deleted two visual specs and 12 snapshots.
- Added nine non-test v2 files: 2,455 lines, plus two v2 test files: 190 lines.
- `web/src/lib/api.ts` remains 7,047 lines, with 181 exported functions and 361 exported types.
- Checkpoint screens/components referenced 131 API functions; active App/v2 references 21. At least **112** formerly UI-reachable API functions are now backend-only.

## Historical M0 route disposition

At M0 the app declared 21 routes but exposed only five real view components. Only `query` and `provider` were consumed; `view`, `action`, `section` and `collection` were ignored. M1 now preserves those intents in typed compatibility state and renders an explicit real/unavailable destination, but it does not claim the M2–M4 target workflows are complete.

| Previous user workflow | Active v2 destination | Disposition |
| --- | --- | --- |
| `/`, `/topics/:id/dashboard` | Dashboard | Direct v2 family; partial parity. |
| `/portfolio` | Dashboard `?view=portfolio` | Compatibility intent ignored; restore portfolio controller/card drill-down. |
| `/provenance` | Papers `?view=provenance` | Evidence search/detail lost; port legacy behavior. |
| `/evidence` | Papers `?view=evidence` | Guide misroutes; restore a real guide and separate evidence route. |
| `/new-run` | Dashboard `?action=new-run` | Action ignored; restore run creation. |
| `/runs`, `/runs/:runId` | Insights | Run ID and workbench discarded; restore run controller. |
| `/topics/:topicId/runs/:runId` | topic Dashboard | Run ID discarded; compatibility defect. |
| `/reports` | Reports | Direct v2 family; list/download/toggle only. |
| `/reports/:reportId` | Reports `?view=detail` | Report ID/detail/version discarded. |
| `/recommendations` | Insights | Decision/promotion/create-topic flow removed. |
| `/compare` | Insights | Comparison state and rerun planning removed. |
| `/automations` | Reports | Existing toggle only; CRUD/run-now/recovery absent. |
| `/settings`, `/settings/notifications` | Settings | Basic settings direct; section intent ignored, notification subset only. |
| `/project-bundle` | Settings | Import/export workflow absent. |
| `/admin/legacy-runs` | Settings | Admin migration/audit workflow absent; must remain admin-only. |

## Capability reuse matrix

Importance-weight method: 42 records receive product importance points totaling 160. Reuse factors are 1.0/`KEEP_AS_IS`, 0.9/`KEEP_BEHIND_FACADE`, 0.7/`REFACTOR_INCREMENTALLY`, 0.65/`EXTEND_EXISTING_CONTRACT`, 0.15/`REPLACE`, and zero for `RETIRE` or net-new. Effective reusable foundation is **106.25/160 = 66.4%**. Confidence is high for source/backend findings and medium for the unimplemented desktop host.

| ID | Exact path | Capability | Reachability / tests | Current state | Target / prerequisite | Compatibility and completion condition | Class | Confidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| C01 | `app/auth`; `app/api/auth.py` | Auth/session | Active; auth/navigation tests | real | Desktop same-origin session; no precursor | Preserve cookie/OIDC; complete when host uses unchanged contract | `KEEP_AS_IS` | high |
| C02 | `app/domain/path_safety.py`; `app/repositories/shared_state.py` | Safe paths/atomic writes | Backend active; storage suites | real | All new stores use primitives | Fail closed; no bypass | `KEEP_AS_IS` | high |
| C03 | `app/services/report_transaction.py`; `report_recovery.py` | Report transaction/recovery | Active; transaction/recovery tests | real | M4 final commit; needs manifest/job IDs | Preserve one accepted version and recovery journal | `KEEP_AS_IS` | high |
| C04 | `app/services/provider_output_acceptance.py`; `provider_report_integration.py` | Schema/evidence validation | Backend active, UI hidden; Provider tests | backend-not-wired | Every Provider stage; needs typed stages | Canonical Pydantic/allowed evidence unchanged | `KEEP_AS_IS` | high |
| C05 | `app/services/prototype_live_provider.py` | Candidate acceptance fencing | Backend active, UI hidden; live-provider tests | backend-not-wired | Report workspace; needs candidate UI | Candidate never authority before explicit decision | `KEEP_AS_IS` | high |
| C06 | `app/repositories/runs.py`; `app/api/runs.py` | Run lifecycle/lineage | Backend active, UI removed; run suites | backend-not-wired | Topic run controller | Preserve IDs/events/artifacts; complete at run-detail parity | `KEEP_BEHIND_FACADE` | high |
| C07 | `app/services/topic_profiles.py`; `topic_configurations.py` | Topic lifecycle/config | Partly active; topic tests | partial | Topic facade; reconcile identities | Dual-read profile/topic IDs until backfill | `KEEP_BEHIND_FACADE` | high |
| C08 | `app/services/prototype_workspace.py` | Snapshots/changesets | Backend active, UI removed; workspace tests | backend-not-wired | Typed temporal repository | Preserve immutable IDs/digests; retire whole JSON only after parity | `KEEP_BEHIND_FACADE` | high |
| C09 | `paper_review.py`; `full_text_inventory.py`; `evidence.py` | Review/full text/evidence | Backend active, v2 summary; evidence tests | partial | M2 detail/provenance; needs canonical Paper | Preserve locator/text/content digests | `KEEP_BEHIND_FACADE` | high |
| C10 | `report_versions.py`; `exports.py` | Versions/diff/export | Backend active, v2 list only; report tests | backend-not-wired | M4 detail controller | Immutable version lineage; complete at preview/diff/export parity | `KEEP_BEHIND_FACADE` | high |
| C11 | `run_jobs.py`; `run_executor.py`; `topic_scheduler.py` | Job leases/scheduling | Backend active, task subset; job/scheduler tests | partial | M4 stage checkpoints | Preserve fencing/idempotency; extend, do not replace | `KEEP_BEHIND_FACADE` | high |
| C12 | `app/services/topic_notifications.py` | Notifications/deliveries | Settings subset; notification tests | partial | M5 center/history | Unavailable SMTP must remain unavailable | `KEEP_BEHIND_FACADE` | high |
| C13 | `prototype_project_bundle.py`; `app/api/prototype.py` | Portable bundle | Backend active, UI removed; bundle tests | backend-not-wired | Desktop file dialogs | Bundle remains projection, never runtime backup | `KEEP_BEHIND_FACADE` | high |
| C14 | `report_draft_assist_registry.py`; `providers.py` | Provider select/status/execute | Backend active, status fragment; Provider suites | partial | Truthful report settings/execution UI | Retain deterministic fallback and activation proof | `KEEP_BEHIND_FACADE` | high |
| C15 | `prototype_research_gaps.py`; `prototype_living_topics.py` | Gaps/recommendations/Living Topics | Summary only; gap/living tests | partial | M3 temporal facade | Preserve corpus-relative wording/decision records | `KEEP_BEHIND_FACADE` | high |
| C16 | `metadata_index.py`; `evidence_search.py` | Index/search | Backend active, evidence UI removed; search tests | backend-not-wired | Snapshot-bound cursor/indexes | Preserve result IDs; replace broad in-memory ranking gradually | `REFACTOR_INCREMENTALLY` | high |
| C17 | `topic_analytics.py`; `topic_dashboard.py` | Analytics/dashboard | Active; analytics tests | partial | Append-only metric series | No invented labels; calculation metadata required | `REFACTOR_INCREMENTALLY` | high |
| C18 | `app/services/report_hub.py` | Report hub projection | Active; report-hub tests | partial | Real events/telemetry | Unknown stays unavailable | `REFACTOR_INCREMENTALLY` | high |
| C19 | `web/src/v2/V2Shell.tsx`; `V2TopicWorkspaceScreen.tsx` | V2 shell/controller | Active; minimal tests | partial | Per-resource controllers | Preserve auth/URLs; finish when no intent is ignored | `REFACTOR_INCREMENTALLY` | high |
| C20 | `V2DashboardView.tsx` | Dashboard view | Active; shallow coverage | partial | Real metric/actions | Preserve hierarchy; remove fake selectors/actions | `REFACTOR_INCREMENTALLY` | high |
| C21 | `V2PapersView.tsx` | Papers view | Active; search/selection test | partial | M2 paper bank | Preserve q/provider URLs; all controls real/disabled | `REFACTOR_INCREMENTALLY` | high |
| C22 | `V2InsightsView.tsx` | Insights view | Active; no semantic formula test | partial | M3 temporal insights | Preserve accessible fallback; remove proxy semantics | `REFACTOR_INCREMENTALLY` | high |
| C23 | `V2ReportsView.tsx` | Reports view | Active; layout test | partial | M4 report workspace | Preserve real downloads; no inert actions | `REFACTOR_INCREMENTALLY` | high |
| C24 | `V2SettingsView.tsx` | Settings view | Active; one save test | partial | Persisted preferences/integrations | Unsupported integrations stay disabled | `REFACTOR_INCREMENTALLY` | high |
| C25 | `web/src/lib/api.ts` | Browser API client | Active, 7,047 lines; partial tests | real | Domain modules; characterize first | Old module re-exports until all consumers migrate | `REFACTOR_INCREMENTALLY` | high |
| C26 | `app/api/dashboard.py`; `V2PapersView.tsx` | Paper query/filter/page | q/provider only | partial | M2 server cursor/facets | Stable query digest/tie-breakers | `EXTEND_EXISTING_CONTRACT` | high |
| C27 | `metadata_index.py`; `runs.py` | Canonical paper/version identity | Run-scoped only; dedup tests | new gap | Identity policy/backfill | Map all old run-paper IDs before cutover | `EXTEND_EXISTING_CONTRACT` | high |
| C28 | `evidence.py`; `paper_review.py` | Normalized claim/evidence | Embedded/projection | partial | M2/M3 typed links | Preserve text hashes/locators | `EXTEND_EXISTING_CONTRACT` | high |
| C29 | `prototype_workspace.py`; `prototype_living_topics.py` | Temporal memory | Snapshot fragments | partial | M3 typed repositories | Preserve snapshot digests/decisions | `EXTEND_EXISTING_CONTRACT` | high |
| C30 | `reporting.py`; `provider_report_integration.py` | Plan/candidate/version | Single-run path | partial | M4 manifest/shards | Never bypass validation/acceptance | `EXTEND_EXISTING_CONTRACT` | high |
| C31 | `V2SettingsView.tsx`; future XRML repository | Appearance preference | Session-only/inert | static | Semantic tokens + stored preference | localStorage cache only; server/local store authority | `EXTEND_EXISTING_CONTRACT` | high |
| C32 | `V2Shell.tsx`; dashboard/evidence APIs | Global search | Topic-first only | partial | Typed result kinds/routes | Retain topic URLs; add paper/evidence/run | `EXTEND_EXISTING_CONTRACT` | high |
| C33 | `source_providers.py`; `app/api/dashboard.py` | Source registry | arXiv/OpenAlex/Crossref current | partial | Per-source readiness and extension point | Unimplemented never appears connected | `EXTEND_EXISTING_CONTRACT` | high |
| C34 | `V2InsightsView.tsx` | Proxy analytics labels | Active, untested truth | partial | Provenance-bound metrics | Show real formula or unavailable | `REPLACE` | high |
| C35 | `V2ReportsView.tsx` | Static templates/actions | Active, layout-only test | static/mock | Typed template/action registry | Disable unsupported export/templates | `REPLACE` | high |
| C36 | `web/src/v2/v2.css`; `V2Primitives.tsx` | Dark-only colors | Active; no current visual suite | static | Semantic theme system | Incremental DOM reuse; replace color authority | `REPLACE` | high |
| C37 | `6c2c9cb4:web/src/screens`; `components` | Legacy visual shells | Deleted; deleted tests | backend-not-wired | V2 feature modules | Use only behavior/tests; retire after parity | `RETIRE` | high |
| C38 | `web/src/App.tsx` aliases | Ignored route intent | Active but lossy; untested | static | Real compatibility controllers | Preserve IDs/state, then retire query shims | `RETIRE` | high |
| C39 | No tracked host | Native desktop host | Absent; no tests | new | Tauri spike | React/Vite/FastAPI/SQLite preserved | `NEW_REQUIRED_CAPABILITY` | medium |
| C40 | `run_jobs.py` precursor | Staged large-corpus synthesis | Monolithic only | new | CorpusManifest/Job/Shard | Reuse leases and final acceptance | `NEW_REQUIRED_CAPABILITY` | high |
| C41 | Gap/analytics precursors | Cluster/consensus/contradiction lifecycle | Ephemeral/absent | new | M3 temporal entities | Typed SQLite; no default graph DB | `NEW_REQUIRED_CAPABILITY` | high |
| C42 | No current authority | Team permissions/external telemetry | Presentation placeholders | intentionally unavailable | Future owner-authorized contract | Remain disabled until real identity/privacy policy | `NEW_REQUIRED_CAPABILITY` | high |

Counts:

| Class | Count |
| --- | ---: |
| `KEEP_AS_IS` | 5 |
| `KEEP_BEHIND_FACADE` | 10 |
| `REFACTOR_INCREMENTALLY` | 10 |
| `EXTEND_EXISTING_CONTRACT` | 8 |
| `REPLACE` | 3 |
| `RETIRE` | 2 |
| `NEW_REQUIRED_CAPABILITY` | 4 |

## M1 adopted capability overlay

The 42-record classification and **106.25/160 = 66.4%** score above remain the historical M0 planning baseline; they are not recomputed after implementation. M1 changed the following bounded capability dispositions while keeping the remaining capability rows and future milestone assignments intact.

| Capability | M0 state/class | M1 adopted state | Implementation/test evidence | Remaining boundary |
| --- | --- | --- | --- | --- |
| C39 Native desktop host | absent; `NEW_REQUIRED_CAPABILITY` | Tauri host and packaged FastAPI sidecar implemented at `8e967ae9`, completed at `3c4dd21b` | Rust host/sidecar tests, native smoke, web/Tauri builds, D-drive requalification | unsigned local installer only; signing/updater/tray/release remain excluded |
| C31 Appearance preference | session-only/inert; `EXTEND_EXISTING_CONTRACT` | persisted appearance, density and motion preferences implemented | `AppearanceProvider`, appearance contract/contrast tests and five-theme validation | visualization and domain-specific analysis preferences remain later milestones |
| C36 Dark-only colors | `REPLACE` | semantic System/Light/Dark/Solarized Light/Solarized Dark authority implemented | semantic token paths plus theme contrast and view tests | no claim that M2–M4 data semantics are complete |
| C19 V2 shell/controller | partial; `REFACTOR_INCREMENTALLY` | independent resource states, route focus and compatibility intent disclosure implemented | M1 shell/workspace/default-route tests | paper, memory and synthesis sub-workspaces remain M2/M3/M4 |
| C38 Ignored route intent | active but lossy; `RETIRE` | compatibility shims preserve typed IDs/query intent and truthfully disclose unsupported destinations | route-intent and legacy-boundary tests | shim retirement waits for the corresponding milestone workflow |

M1 also repaired or disabled the 77 touched UI mappings without inventing capability completion: `total=105`, `touched=77`, `unresolved=0`, `m2_complete=false`, `m3_complete=false`, `m4_complete=false`.

## Historical M0 per-tab truth and defects

### Dashboard

Real: dashboard projection, collect/analyze/report actions, existing task run/toggle, favorite persistence. Partial/static: period selectors are inert; new task only opens generic Settings; list cards cannot open details. Portfolio, Prototype workspace, run creation/detail, Provider proof and provenance drill-down are absent.

### Papers

Real: server q/provider fetch and external source links. Local-only: status/sort over at most 50 rows, selection state, reading-state derivation. False affordances: year/citation/impact, save, export, bookmark, pagination and help. “Add to report” ignores selected IDs. Every row claims Open Access. Paper detail/review/evidence exists in backend but is hidden.

### Insights

The inputs are real dashboard/recommendation projections, but labels are not: average impact becomes “maturity”, review coverage becomes “benchmark”, trend delta becomes “industry”, and generic time-series values become named research themes. The cluster is a deterministic star plus index-generated edges, not an observed relation graph.

### Reports

Real: list/lifecycle, existing download URL, schedule toggle and generic report action. Synthetic: pages=`versions*4`, performance count=array index, popularity=version count. Templates collapse to one action. Preview/share/copy/new schedule/PDF/PPT are inert. Backend detail/version/diff/editor/citation/Provider candidate/lineage contracts are hidden.

### Settings

Real: profile fields, queries/cadences, current provider readiness, supported notification changes. Session-only: weights, density, animation. Inert: theme, chart type, language, time format, add source/member/help. Future connector rows are correctly disabled. Team/role rows are fabricated presentation and must be removed.

### Loading/error/accessibility defects

1. `V2TopicWorkspaceScreen` uses one shared error, then section loading can clear a base failure; rendering still requires `dashboard`, producing a blank body.
2. `Promise.allSettled` and multiple `.catch(() => null/empty)` paths conflate unavailable with empty.
3. Retry always calls `loadBase`, not the failed tab resource.
4. Many focusable buttons/selects have no behavior.
5. Modal lacks Escape, focus trap/restore and background inerting.
6. Data grids are div/article structures rather than semantic tables.
7. `⌘K` is shown without a key listener.
8. Sidebar “더보기” clears search but never expands the seven-item slice.
9. No skip link, route-focus management or explicit `:focus-visible` system.

## Legacy recovery map

| Capability | Pre-v2/checkpoint donor | Current v2 | Backend/tests survive? | Parity | Recovery/retirement |
| --- | --- | --- | --- | --- | --- |
| Topic/run creation | PrototypeWorkspace/Home/RunDetail | Topic create only | APIs/backend yes; UI tests deleted | partial | Port run controllers and characterization tests; retain deep links. |
| Paper intake | Dashboard/RunDetail/PaperTable | generic collect + list | yes | partial | Port source/readiness/progress; keep v2 table composition. |
| Full-text review | PaperDetail/RunDetail/RealCorpus panels | absent | backend/tests yes | absent | New v2 detail drawer behind review facade. |
| Evidence navigation | EvidenceProvenance/ProvenanceDetail | misrouted to Papers | backend/search tests yes | absent | Restore dedicated evidence route and result/detail focus. |
| Report generation | ReportWorkspace/Provider panels | generic topic action | backend/tests yes | partial | Typed ReportPlan and Provider state UI. |
| Report versions/diff | ReportDetail/ReportDiff | absent | endpoints/tests yes | absent | Version timeline/diff under Reports. |
| Report export | ReportDetail/Workspace | existing links only | backend/tests yes | partial | Capability-aware export; no fake PPT. |
| Provider select/status | Settings/Provider panels | tiny status fragment | backend/tests yes | partial | Port activation/selection/candidate/decision state, not old JSX. |
| Candidate/acceptance | PrototypeProviderProofPanel | absent | backend/tests yes | absent | Restore separate candidate and acceptance views. |
| Provenance | RealCorpus/Provenance panels | absent | backend/tests yes | absent | Run/report/evidence lineage inspector. |
| Schedules | Automations/Notification Settings | list/toggle | backend/tests yes | partial | CRUD/run-now/health/recovery controller. |
| Living Topics | PrototypeWorkspace | absent | backend/tests yes | absent | M3 temporal workspace. |
| Recommendations | RecommendationsScreen | gap cards/actions | backend/tests yes | partial | Restore accept/hold/reject/promote/create-topic. |
| Bundle import/export | ProjectBundleScreen | absent | backend/tests yes | absent | M1/M5 native file-dialog workflow. |
| Notifications | NotificationSettings | rules/channels subset | backend/tests yes | partial | Items/deliveries/test delivery and unavailable state. |
| Settings | SettingsScreen/ProfileDrawer | mixed real/static | backend partly | partial | Split real preferences from integrations; remove team mock. |
| Error/loading/empty | per-screen legacy contracts | shared base/section state | many tests deleted | partial | Independent resource state machines and migrated tests. |

Unresolved classifications: **0**. “Absent” is a decided migration disposition, not an unknown.

## Giant-module facade plan

- `web/src/lib/api.ts` (7,047 lines): preserve exports while implementations/types move to `auth`, `topics`, `papers`, `runs`, `evidence`, `reports`, `schedules`, `providers`, `prototype-bundle`, and `admin` clients. Delete the facade only when import scans and characterization tests show zero consumers.
- `V2TopicWorkspaceScreen.tsx` (356 lines): extract `useTopicBaseController` plus per-tab controllers with independent loading/error/empty/retry.
- `V2Shell.tsx` (307 lines): extract topic gateway, typed global search, modal and session menu.
- Donor giants—`ReportWorkspace.tsx` 2,730 lines, `ResearchDashboard.tsx` 2,055, `DashboardScreen.tsx` 1,856, `RunDetailScreen.tsx` 950, `CompareScreen.tsx` 939, `ReportDetailScreen.tsx` 909 and `ProfileDrawer.tsx` 832—must not return wholesale. Port controllers, state machines, truth labels and tests into feature modules.

## Historical M0 test and visual-regression truth

- Frontend unit inventory fell from 27 files at checkpoint to five files at current HEAD.
- Current v2 view coverage is three tests: paper search/selection, report layout, and one settings save.
- `V2LegacyBoundary.test.ts` treats deletion as success, not parity.
- Current `web/tests/visual` has zero files although Playwright config still targets it.
- Five remaining Playwright specs still assert deleted UI, so they are stale—not current v2 evidence.
- `design-qa.md` records manual visual comparison and zero console errors only. Its P0/P1/P2 zero cannot support functional parity.

M1 restored bounded behavior-level shell/view/theme/route/state contracts and added semantic visual baselines for its declared scope. Legacy snapshot files themselves remain donor history; M2–M4 behavior and visual contracts are still restored only as their real vertical slices land.

## Persistence disposition

Keep current run files, profile JSON, Prototype workspace JSON, review stores, SQLite projections, report artifacts and scheduler state behind repositories during migration. Add explicit authority/projection ownership before expanding the schema. Dual-read and digest-compare before any backfill cutover. No duplicate database and no default graph database.

## Final reuse verdict

- Backend reuse: high; operational contracts survive and account for most weighted value.
- Persistence reuse: high behind facades, but authority overlap must be resolved.
- Frontend component reuse: low at current v2; information hierarchy is reusable, behavior parity is not.
- Test reuse: backend high; frontend behavioral donor tests valuable but deleted/stale and must be ported.
- Overall importance-weighted reusable foundation: **66.4%**.
- M1 foundation parity: **complete for declared local Windows scope**.
- M2/M3/M4 product parity: **not started**.
- Recommended action: proceed with `M2_REAL_TOPIC_PAPER_BANK` using checkpoint-guided vertical-slice recovery, not revert and not rewrite.

Repair evidence remains anchored at `fa29a0f24827006bf647ff1bb042a8d2cf0e0844`; this read-only audit does not move that ref.
