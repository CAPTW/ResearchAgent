# ADR 0001: Source identity and current authority

## Status

Accepted historical decision (2026-08-24); current-status clarification added
2026-09-28. The acceptance evidence below retains its original scope.

## Date

2026-08-24

## Current-status clarification (2026-09-28)

Active development is on local `main` in the
`xrml-m3b-snapshot-bound-analysis-insights` worktree. M3-B and its subsequent
continuation are already part of that history; the worktree name does not mark
an unadopted candidate. The historical release ref below is not the current
development checkout. Read actual Git branch/HEAD and
[current local product status](../OPEN_SOURCE_READINESS.md) together for
the current implementation, applicable validation and remaining work.

Current local features include the Paper Bank, immutable snapshot/diff and
policy-bound analysis, frozen extractive drafts, human cited drafts, journal
import/review/acceptance, comparison/export, working-copy recovery and original
Unicode preservation. Isolated offline Journal approval/dispatch is now connected
to saved results, human review and export; use the living status record for its
source/API/browser qualification and default-disabled limits. Historical live acceptance at
`d060383a` proves only its bounded Codex `report_draft_assist` path. It does not
prove new Journal dispatch, full-text review, another Provider or current release
readiness.

The first Prototype's present goal remains AI review of acquired paper bodies,
explicit review coverage and evidence persisted for Journal reports and grounded
research proposals, with a topic dashboard, no Docker requirement and replaceable
supported CLI backends. Manual or synthetic flows alone do not complete that goal.
Implementation and old execution evidence do not grant permission for new
accounts, external source transmission, Provider costs or deployment.

The rest of this ADR preserves the 2026-08-24 decision and evidence. Its
"current" labels refer to that date; later main development is not limited to a
docs-only successor of `d060383a`. `STATUS.md` remains historical material.

## Historical context

The parent container is not a Git root. Multiple worktrees share one object
database. The previous docs-only current-truth commit was
`24dc9847e8661b57a3c6d90427cce00eacf13109`. The completed Codex Provider
feature then advanced through `2cd676f1` and the strict-schema repair
`d060383a`, where bounded automated and live acceptance evidence was completed.

## Historical decision

- The parent container is not a Git root.
- The operational current branch is
  `release/post-canonical-playwright-truth-pass-notification-empty-state-repair-v1`.
- Adopted implementation authority is `d060383a441308f842e1e853c5bb2d1912e79a3a`
  (tree `17daf2011d7f1293dfef30988dd37af69d23fd80`).
- Its adopted ancestry is `24dc9847 → 2cd676f1 → d060383a`.
- The current release branch advanced from `24dc9847` to `d060383a` by
  fast-forward only; no merge commit was created.
- Canonical historical baseline remains `131d909cfcfe0af9140664277f484783cbac52bb`.
- `repair/pytest-fresh-validation-r1` remains locally retained as an evidence
  branch at `fa29a0f24827006bf647ff1bb042a8d2cf0e0844`.
- `feature/initial-prototype-codex-provider-vertical-slice` remains retained at
  `d060383a` as feature evidence and review history unless the owner later
  decides otherwise.
- A docs-only commit may advance repository HEAD without changing implementation
  authority. That docs-only HEAD may therefore differ from implementation
  authority `d060383a`.
- Repository `STATUS.md` remains
  `INTERNAL_HISTORICAL_BYTE_FROZEN_NOT_CURRENT_AUTHORITY`. Current truth
  surfaces are README, this ADR, and
  `docs/OPEN_SOURCE_READINESS.md`.
- Current evidence requires commit, command, environment, and date.
- The official Codex CLI's existing OAuth/session-backed
  `report_draft_assist` path is proven locally on `d060383a`, including typed
  candidate generation, validation, acceptance, persistence, and provenance.
- Initial Prototype functional goals are complete only within the declared
  local scope.
- Local proof does not prove staging, production, external beta, live GitHub
  Actions, Ubuntu timing parity, App OAuth/OIDC, or public MCP. Gemini/Claude
  adapters and weighted orchestration are not implemented.

## Historical authority hierarchy

1. Canonical historical baseline: `131d909cfcfe0af9140664277f484783cbac52bb`
2. Previous current-truth commit: `24dc9847e8661b57a3c6d90427cce00eacf13109`
3. Initial Codex Provider feature commit: `2cd676f1fd3a95d9c5f8ef60c918d40b21f85093`
4. Adopted implementation authority: `d060383a441308f842e1e853c5bb2d1912e79a3a`
5. Current repository truth HEAD: the docs-only successor on the current branch,
   if present

## Repository-container rule

`Rsearch Agent` is a directory of worktrees. It is not itself a Git repository.
Shared objects live in the common Git directory of the starter worktree.

## Canonical, previous truth, and adopted implementation commits

- Canonical: `131d909cfcfe0af9140664277f484783cbac52bb` on
  `rc/h008d115-l10-current-chain`
- Previous current truth: `24dc9847e8661b57a3c6d90427cce00eacf13109`
- Initial Provider feature: `2cd676f1fd3a95d9c5f8ef60c918d40b21f85093`
- Adopted implementation: `d060383a441308f842e1e853c5bb2d1912e79a3a`

## Historical branch disposition

Operational current branch:
`release/post-canonical-playwright-truth-pass-notification-empty-state-repair-v1`.
It advanced to `d060383a` by fast-forward only. No merge commit was created.
The feature branch remains at `d060383a`; canonical and repair refs are
unchanged.

## Repair-branch preservation

`repair/pytest-fresh-validation-r1` stays at
`fa29a0f24827006bf647ff1bb042a8d2cf0e0844` as local evidence.

## Historical validation evidence summary

Automated validation is attributed to the `d060383a` feature history. The
initial `2cd676f1` feature validation passed backend
`2984 collected / 2980 passed / 4 skipped / 0 failed / 0 errors`, frontend
`27 files / 190 tests`, production build, Ruff, mypy, and secret/artifact
review. The `d060383a` strict-schema repair passed `82` targeted tests, backend
`2991 collected / 2987 passed / 4 skipped / 0 failed / 0 errors`, the same
`27 files / 190 tests`, production build, Ruff, and mypy. Its compiled report
schema SHA-256 is
`c730cffb7f72423c9ad7fe840e1860c82195efda088ceedb28848a084cd3bee8`
with seven object nodes and zero complete-required violations.

Successful live Provider acceptance is attributed to `d060383a`. One bounded
contract probe and one product-level `report_draft_assist` call used the
installed official Codex CLI and existing OAuth/session. The typed candidate
passed canonical schema and paper/evidence validation, was accepted separately,
and persisted provider-assisted report v3 with complete
execution/run/candidate/report/evidence provenance. The accepted execution used
`fallback=false` and retained deterministic versions. The bounded record is
owner-held in task-owned D-drive validation storage and is not repository
source.

Docs/truth-targeted validation belongs to the docs-only successor HEAD, not as a
full-CI or live-Provider rerun.

## Windows timeout / Ubuntu parity limitation

`WINDOWS_BACKEND_TIMEOUT_BUDGET_RISK_UBUNTU_PARITY_UNPROVEN`
`UBUNTU_RUNNER_TIMEOUT_PARITY_VALIDATION_PENDING`

## Historical product stage / no-go

`LOCAL_INTERNAL_RC_OR_BETA_VALIDATION`.
Milestone: `INITIAL_PROTOTYPE_FEATURE_GOALS_COMPLETE_LOCAL_SCOPE`.
No-go: staging, production, external beta, live GitHub Actions claim, Ubuntu
parity claim, App OAuth/OIDC, public MCP, and distributed scheduler.
Gemini/Claude adapters and weighted orchestration remain unimplemented.

## Consequences

Current-status documents must distinguish implementation authority from
repository truth HEAD. Current truth surfaces exclude `STATUS.md`.
Live acceptance is attributed to `d060383a` and does not transfer a production,
staging, App OAuth/OIDC, or public MCP claim to the docs-only successor.

## Supersession / update rule

Preserve this decision's original implementation and validation attribution.
Architecture changes belong in a later ADR; ordinary development status belongs
in the living status record with its commit, command, environment and date.
Do not silently retitle an older commit as current or transfer its live
acceptance to a new execution path.
