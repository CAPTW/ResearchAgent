# Open-source preparation

This is development source, not a stable release or a scientific-quality claim.
The local `main` lineage starts this preparation at `3db71003e9aae6c2709c3fc6cfc820716b741c94`.
The private candidate manifest identifies the exact source and byte hashes that
were packaged. The Owner-approved snapshot was published to `main` as the
parentless public commit `720565fc480a6bc28cc6238c23912f5e5a770e6f` on 2026-10-26
UTC (2026-10-27 in Korea). Its 1,161 files matched the reviewed archive bytes and
modes. This was a normal non-force push; no release or deployment was performed.

## Hosting and history

The Owner identified [CAPTW/ResearchAgent](https://github.com/CAPTW/ResearchAgent)
as the intended destination. Read-only inspection on 2026-10-26 confirmed that
it is **public**, with default branch `main` and no remote refs, branches, tags,
Issues, PRs, Releases, Actions runs or artifacts at inspection time. Discussions
are disabled; the Wiki remote returned not found. Repository size was zero.
That initial inspection preceded the approved first push. No remote LFS
references were found in the then-empty ref set;
GitHub-internal unreachable storage cannot be inspected by these APIs.

These are point-in-time observations, not an enduring guarantee. Recheck refs
before any synchronization. The existing public destination being empty does
not make the private development history suitable to push into it.

The development history contains paper PDFs, raw Provider proof, generated
reports, archives and private development records. Current-file exclusions and
`.gitignore` do not remove those historical objects. Publishing the development
repository or a clean branch inside it is not an approved privacy boundary.
Original history and local evidence are retained.

The Owner selected a separately maintained public source snapshot. The reviewed
archive contained no `.git` directory or parent history. Later updates import
reviewed file content into that public repository and retain only its public
parent chain. Do not merge private parents, copy `.git`, push all development
refs or restore private commit metadata. The private development checkout has a
local disabled push URL; ordinary public synchronization uses its separate
checkout and verifies the remote parent before each non-force push.

Creating a destination, changing visibility, rewriting canonical history,
force-pushing or deleting remote refs requires the corresponding explicit Owner
choice. Repository/issue URLs are now recorded in Python package metadata. A monitored
private security-reporting channel still needs to be selected or confirmed.

## Local candidate boundary

The first public [CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40347030351)
failed. Python setup could not find the pinned 3.12.12 runtime for Ubuntu 26.04,
so backend checks did not run. The backend runner now targets Ubuntu 24.04,
which has an [official build for that exact Python version](https://github.com/actions/python-versions/releases/tag/3.12.12-18393146713).
The Python/mypy toolchain and existing error inventory are preserved. The
packaging tool's existing diagnostic is relocated in the baseline after the
publication exclusions moved its source line; this is not a clean typecheck.
Frontend tests recorded 1,078 passed and
one failed keyboard-selection assertion; its production build was skipped.
The receipt test now waits for the initial load's heading focus before selecting
another control with Enter. Subsequent CI results must be read for the exact
public commit; the original failed run remains evidence, not a passing baseline.

The reviewed repair was synchronized as public commit `458e194f`, retaining only
the initial public parent. Its [CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40650994849)
passed all 76 frontend test files (1,079 tests) and the production build. Backend
Python setup, dependency lock/install and Ruff passed, then the mypy verifier
failed on `import-not-found`; backend tests and startup checks were skipped.
The browser collector's optional PDF-engine import was reproduced with the exact
mypy toolchain and that dependency hidden: the original annotation failed, while
the annotation covering both missing and untyped optional imports passed. The
runtime still refuses PDF acceptance without the engine. This bounded reproduction
does not replace the next Linux CI run or establish a clean typecheck.

The next reviewed snapshot, source `bfa823d7`, was published non-force as
`fcebb490`, with only the two preceding public commits as ancestors. All 1,164
archive files matched public Git bytes and modes. Its
[CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40656979547) passed
the 13 extension tests, 1,079 frontend tests and production build on Node 20.
Backend setup, lock/install and Ruff passed, but the mypy baseline differed by
platform; backend regression, PDF and startup checks did not execute. An exact
Python 3.12.12/mypy 1.20.2 reproduction returned 564 errors/20 notes on Windows
and 575 errors/20 notes with an explicit Linux typing target. The full difference
is 16 Windows-API diagnostics added on Linux and five POSIX-API diagnostics absent
there. This diagnosis is a static target comparison on Windows, not Linux runtime
verification. The original three CI failures remain preserved.

The affected code now uses direct platform branches, preserving Windows DPAPI,
file-lock and child-process cleanup behavior. Paired checks on the exact toolchain
produce identical inventories: 559 errors, 20 notes, 579 diagnostics across 69
files. Review confirmed only five inapplicable POSIX diagnostics were removed from
the Windows baseline; 13 existing scheduler diagnostics moved by one source line.
No new diagnostic, global suppression or fixed Windows-only typing target was
introduced. This is still a debt baseline, not mypy-clean code.

On Windows, 295 existing native-host, credential, fake-process, locking, scheduler
and report-transaction tests passed without changing their assertions. The mypy
verifier (154), packaging (132 and 24) and public-link (49) checks passed. The same
run exposed five obsolete static-contract assertions; after correcting the reviewed
runner, diagnostic inventory and expired file pins, that module passed 101 tests.
The adjacent build-authority module passed 53 tests after retiring its duplicate
historical verifier/inventory byte pins; maintained verifier and build properties
remain enforced.
Whole-core Ruff passed. These are affected local scopes, not a full backend suite
or actual Provider/browser acceptance. The next public CI must qualify Linux
runtime behavior and the remaining backend checks.

Source `0b38b7c` was published non-force as `e5610e7`, retaining only the four
public commits. Its [CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40661198290)
passed the actual Linux mypy baseline and verifier self-type checks. Frontend
recorded 13 extension tests passed, 1,078 integration tests passed and one failed;
the build was skipped. Backend collected 6,961 tests but was cancelled at 77%
after multiple failures. No final pytest summary was produced; PDF, compile,
startup and fixture-immutability checks did not run. This is not a passing suite.

The frontend failure exposed another test readiness race: an inspect helper could
click a disabled button while history was loading. A controlled deferred response
reproduced the missed click. The corrected helper waits for the enabled state;
all 35 tests in that module and TypeScript passed locally, with the product and
timeouts unchanged. Other bounded fixture repairs cover synthetic CLI availability,
the current locked-store warning, public-only JSONL absence and executable Git
fixture modes. These do not replace a new CI run.

An actual Linux reproduction identified virtualenv interpreter symlinks in fake
CLI fixtures. Trusted synthetic launchers now resolve their interpreter path;
production symlink rejection and tamper checks remain intact. Child Python can
also add a locale variable on Unix, so the isolation test checks the exact
environment passed to the process separately from that documented startup effect.
Windows PowerShell operator tests now have an independent Windows CI job alongside
native contracts. The general backend/POSIX suite stays on Linux and reports its
first failure with a traceback. Hosted Windows acceptance remains unverified until
that job executes; real Whale connection and installed first-start failure remain
unresolved.

The reviewed `bb5228a` snapshot was synchronized as `0cd4f57`. Its
[CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40669046690) failed
workflow validation before registering any job or running any test: `runner.temp`
was used in a job-level environment expression, where that context is unavailable.
The Windows location is now assigned from a step through `GITHUB_ENV`, where the
runner context is available. This configuration defect was missed by the original
static and independent review; that failed run remains preserved. The bounded
local test evidence above does not replace the next valid workflow execution.
Official actionlint 1.7.12 reproduces the original context error and reports no
diagnostics for the corrected workflow. The 103 workflow-contract and 49 public-link
tests pass. Hosted runner execution remains a separate check.

Local Linux qualification additionally found that the preview immutability test
included a background scheduler's index timestamp writes. A restart with no GET
requests reproduced those writes. The test now disables that unrelated worker in
its own fixture while retaining the original full-database equality and frozen
execution assertions. Product scheduling and its separate tests are unchanged.

The workflow repair and preview fixture were published together as `87414a6`,
retaining seven public-only commits. Its
[CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40671851071) passed
13 extension tests, all 1,080 frontend tests and the production build. Backend
passed its baseline checks, then stopped with 2,604 tests passed, 33 skipped and
one Journal preparation snapshot failure. Windows runtime setup succeeded, then
operator tests stopped with seven passed and one failed. The later backend PDF,
compile and startup steps did not run; no artifacts were produced.

Two additional read-only tests now isolate their background scheduler after
no-request controls reproduced index timestamp writes independently of the HTTP
requests. Their original database/authorization assertions remain. Optional PDF
attachment fixtures skip explicitly without the locked fulltext dependency, and
the separate fulltext CI step now includes attachment tests. That three-module
PDF group passed 90 local Linux tests with the locked engine.

Windows subprocess controls also reproduced a concrete CLI defect: Korean JSON
diagnostics fail when redirected stdout uses cp1252. Both activation and scheduler
CLI output now use lossless JSON escapes; parsed Unicode values and exit codes
are preserved. Local PowerShell reaches all preflight stages with either cp1252
or UTF-8. The original hosted child stderr was not captured, so its exact cause
still requires a subsequent CI observation. Failure assertions now retain step
statuses and fixed exception class names without echoing raw logs or environment
values. These repairs do not establish a full backend or Windows CI pass yet.

The next snapshot, public `ffacf415`, has a completed
[CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40675067225). Windows
passed all 306 selected operator/native tests with exact Python 3.12.12. Frontend
passed 13 extension tests, 1,080 integration tests and its production build.
Backend passed lint and the existing mypy debt checks, then stopped with 4,968
passed, 52 skipped and one failed full-database comparison in the Anthropic
offline default-deny test. It was not a timeout. Later PDF, compile, startup and
fixture-immutability steps were skipped; artifacts were empty.

A bounded control using that exact archive traced independent scheduler writes
to the index timestamp even without HTTP requests. The original case happened
to pass under the instrumented timing, so its repeat success is not the causal
evidence. The shared paper-range fixture now defaults that unrelated scheduler
off, matching the existing app-factory convention. Tests of unsafe configuration
can still explicitly enable it afterward. No production scheduling, shared client,
authorization or whole-database assertion was changed. The original hosted trace
was abbreviated; this local control establishes the fixture's independent writer,
not a captured stack trace from the hosted failure. The next CI result must still
qualify the entire backend suite.
The exact CI7 archive plus this single fixture overlay passed 278 Linux tests
across 15 affected modules. Eleven focused denial/configuration checks overlap
that run. Two other consumers already apply the same scheduler setting before
app construction and were inspected without rerunning them. Independent review,
49 public-link checks and scoped Ruff passed. These are bounded fixture checks,
not a completed whole-backend run or actual Provider execution.

The follow-up source `31a970ad` was published as `bac7a3e2`. Its
[CI run](https://github.com/CAPTW/ResearchAgent/actions/runs/40678066664) was
cancelled: the backend check explicitly reported its 15-minute job limit.
Pytest collected 6,965 tests but produced no final summary; later fulltext,
compile, startup and fixture-immutability steps were skipped. Windows completed
306 tests; frontend completed 1,080 tests, 13 extension fixtures and the build.
The backend job now has a 30-minute overall allowance to reach the full result
and post-test checks. Test selection, assertions, fail-fast behavior, individual
limits and the installed-app startup criterion are unchanged. The new CI outcome
is still unverified; the cancelled run remains evidence.

`tools/package_source_distribution.py` reads the committed Git object tree and
its committed content policy. `--allow-dirty` exports **committed HEAD only**;
it does not include pending changes or let an uncommitted policy admit private
files. Archive members retain committed bytes and executable modes. The adjacent
manifest records hashes, exclusions and source authority and stays with private
review evidence, outside the archive.

The candidate retains application code, tests, lockfiles, build definitions,
AGENTS instructions, licenses and individually classified public documentation.
It excludes runtime databases/state, paper PDF/full text, accumulated research
queues/notes/reports, raw Provider proof, archives, logs and internal development
records. Exact local Gemini configurations, reference/development screenshots
and the local design-intake inventory are also excluded. Existing files are not
deleted or rewritten to achieve that exclusion.

Product icons remain required build assets. Binary files must be format-reviewed
or excluded; text scanning cannot establish that image pixels or archive members
are safe. The initial icon review observed the project's simple X glyph; review
receipts identify the inspected formats and remaining limitations. Historical
design-intake receipts remain private historical evidence. The manual-intake
tests now generate synthetic local PNG/Markdown/JSON inputs and call a local
validator, rather than reading those personal screenshots or their inventory.
It calculates completeness from parsed files and reports missing/malformed inputs;
it does not establish visual quality, external provenance or redistribution rights.

## Repeatable review

Use fresh output directories outside every checkout. The following are local,
read-only inspection/package commands; they do not publish anything:

```powershell
python tools/package_source_distribution.py --output-dir $env:XRML_PUBLIC_REVIEW_DIR
python tools/publication_audit.py --repo . --ref HEAD --report $env:XRML_HISTORY_AUDIT_REPORT
python tools/publication_audit.py --directory $env:XRML_EXTRACTED_SOURCE --report $env:XRML_CONTENT_AUDIT_REPORT
```

Run the history scanner for **each ref actually proposed for publication**,
including annotated tags. It scans unique reachable text objects, removed/renamed
paths, commit messages/trailers, author/committer identities and tagger metadata.
It inventories binary and oversized objects as unreviewed. It does not fetch
remote LFS or inspect remote hosting surfaces. A bounded read failure is an
incomplete audit, not a clean result. Its detailed reports contain rule IDs,
locations and hashes of matched values, never matched credential values; keep
those reports out of public source as well.

The directory scanner covers all candidate text, including code and fixtures,
rather than only configuration examples. Pattern hits in deliberately synthetic
security tests must be reviewed in context. Generic private paths, email contacts,
real user inputs and generated records need content review in addition to secret
patterns. No warning count proves absence of personal data or scientific validity.
If an actual credential is found, notify its owner promptly, distinguish unknown
validity from confirmed use, and arrange revocation/rotation; deleting a file is
not remediation of an exposed credential.

## Verification and limits

The 2026-10-26 browser slice was exercised with synthetic metadata/PDFs and an
isolated local API. The actual web screen completed search, saved job, simulated
authentication interruption, explicit resume, PDF confirmation, Papers refresh
and existing local extraction (one page, 609 characters). A 390-pixel viewport,
keyboard focus and unsaved search preservation across page navigation were also
observed. The simulated download receipt is not a Whale download acceptance.

The later real test-profile attempt reached the extension but failed with
`Specified native messaging host not found`; it did not download a PDF. Registry
and origin preparation corrections passed 30 native-host tests, but browser
invocation remains unresolved. Credential-shaped search inputs are now rejected
before metadata requests or persistence; legacy unsafe query projections are
redacted and cannot issue new connections. Eleven focused UI tests, TypeScript
and a real app-screen synthetic paste check passed after this correction. This
guard does not guarantee detection of every possible secret or private query.
The complete collection module then passed 30 tests on the final guard; scoped
Ruff and three-production-file mypy passed. These overlap earlier collection runs.

The subsequent extension error guidance passed 13 protocol/worker tests and a
synthetic popup browser check of lookup, refusal, exit and unknown-error messages,
including keyboard refresh. It preserves saved attempts and does not echo raw
errors or automatically repeat downloads. These results do not resolve the real
Whale host-lookup failure or qualify a signed-in publisher workflow.

Focused results at this checkpoint: backend collection 20 passed; native bridge
18 passed; extension protocol/worker 7 passed. One frontend run passed 25 tests
across collection, Paper Bank and attachment components; a later collection-only
7-test run checked recovery review fixes. These overlap and are not a full-suite
total. Frontend TypeScript build passed. Publication audit/boundary checks passed
161 cases, with later focused scanner and committed-policy rechecks; their counts
also overlap. The storage and review-input operational documents have separate
provenance pins for their September revisions; the original 416 historical
baseline entries remain unchanged. The current source passes 192 affected
policy/history/required-asset checks. A subsequent source-context correction
validates public document completeness and excluded-file absence in public Git
checkouts and extracted source. Private development anchors keep their live
preservation checks; the general missing-history refusal remains unchanged.
The four affected modules passed 209 tests in each of those three contexts, with
seven overlapping checks after the final refinement. These are repeated scopes,
not a whole backend suite or a privacy clearance. Two finite historical policy
projections retain source provenance without importing private parent history.
Manual design intake passed 22 focused tests, including an isolated copy containing
only its validator and tests, with no historical design directory. That repeat
covers the same 22 cases, not 44 independent tests. PNG checks cover framing/CRC,
not pixel decoding, and supplied-secret-marker checks are not general privacy
clearance. The private screenshots and original inventory remain preserved
outside public source; previously reviewed general design documents still ship.
The full focused PDF-extraction and browser-collection modules subsequently passed
57 tests after correcting false missing-file diagnostics for unbound lookup aliases.
Real missing/changed/invalid originals and attachment errors remain visible. This
57-test run includes the earlier collection cases and is not an additive total.

Saved analyses and review history now begin with a read-only paper brief over
existing preserved fields. It separates stored model interpretations from source
quotations, displays actual input coordinates/truncation and keeps missing values
unrecorded. It does not infer research questions/results/conditions or treat a
response-wide citation as support for a particular sentence. The focused brief,
saved-analysis, input-history and Journal run passed 73 tests after review fixes;
TypeScript passed. A synthetic component in Chrome was checked at normal and
360-pixel container widths with keyboard expansion. That is not an installed
Tauri or complete app-flow acceptance for the brief.

The next-document-plan AI controls now separate checking saved Settings from
applying its model/default budgets to a new draft. Unsaved inputs, saved plans
and unknown-request recovery remain distinct. Ten focused component checks and
TypeScript passed, followed by copy-only guidance edits. A synthetic Chrome
component at normal and 360px widths confirmed draft preservation, explicit
application and an unchanged previously saved model. No Provider call was made.

Startup tracing now separates Python entry, imports, factory, lifespan and owned
readiness without changing the 35-second deadline. One fresh source observation
reached readiness at 16.716 seconds (imports 7.946 s, factory 1.189 s, lifespan
5.528 s), with concurrent Rust linking explicitly observed. This is phase-localization
evidence, not an installed or uncontended cold-start qualification. The existing
installed first-launch failure remains unresolved. No repeated timing trial was
used to replace it.

The current local readiness record carries new test/build results and preserves
older runs separately. Older full-suite counts, Provider calls and earlier
archive checks are not current-candidate acceptance. The installed Windows first
launch exceeding 35 seconds remains unresolved; a later 6.641-second success did
not erase it. Browser/site, native installer and paid Provider verification are
separate from local synthetic tests. Existing mypy debt is not a clean typecheck.

Before publishing, extract the exact reviewed archive into a fresh directory,
install from lockfiles, run affected synthetic tests, build the frontend and
check a basic isolated backend startup. Recheck that policy exclusions did not
remove required code or assets. Preserve failures and skipped/unexecuted checks;
do not combine focused counts into a full-suite result.

Local `.github/workflows/ci.yml` runs on main/master pushes and pull requests.
The staging-smoke and Render deploy workflows are manually invoked; ordinary
push does not invoke those deployment workflows. Workflow trigger statements come from local definitions. Remote Actions history
was empty at inspection time; secrets and external integration hooks were not
read and are not claimed absent.

## License and distribution scope

The existing Owner-selected Apache-2.0 license and CAPTW attribution are retained.
No relicensing or registry publication is performed. Personal/institutional email
metadata is omitted from the public source; no substitute contact is invented.
See [license scope](LICENSE_SCOPE.md), [third-party notices](../THIRD_PARTY_NOTICES.md)
and [security policy](../SECURITY.md). Third-party code/data/models and research
papers are not relicensed by the project's license. Rights review, dependency
vulnerability review, signing, external install feedback and any stable-release
acceptance remain separate work.
