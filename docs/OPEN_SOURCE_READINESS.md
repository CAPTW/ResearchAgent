# Open-source preparation

This is development source, not a stable release or a scientific-quality claim.
The local `main` lineage starts this preparation at `3db71003e9aae6c2709c3fc6cfc820716b741c94`.
The candidate manifest identifies the exact later commit and byte hashes that
were actually packaged. No GitHub publication, push, release or deployment has
been performed by this preparation.

## Hosting and history

The Owner identified [CAPTW/ResearchAgent](https://github.com/CAPTW/ResearchAgent)
as the intended destination. Read-only inspection on 2026-10-26 confirmed that
it is **public**, with default branch `main` and no remote refs, branches, tags,
Issues, PRs, Releases, Actions runs or artifacts at inspection time. Discussions
are disabled; the Wiki remote returned not found. Repository size was zero.
The local `origin` now names that exact repository. No push, publication, release
or deployment occurred. No remote LFS references were found in the empty ref set;
GitHub-internal unreachable storage cannot be inspected by these APIs.

These are point-in-time observations, not an enduring guarantee. Recheck refs
before any synchronization. The existing public destination being empty does
not make the private development history suitable to push into it.

The development history contains paper PDFs, raw Provider proof, generated
reports, archives and private development records. Current-file exclusions and
`.gitignore` do not remove those historical objects. Publishing the development
repository or a clean branch inside it is not an approved privacy boundary.
Original history and local evidence are retained.

The prepared source archive contains no `.git` directory or parent history.
After content and build review, the Owner can choose either:

- a confirmed existing destination whose reachable refs and hosted content have
  also been reviewed, using ordinary non-force synchronization where suitable;
- a separately maintained public source snapshot, keeping development history
  private and importing reviewed file content without merging private parents,
  copying `.git`, pushing all refs, or restoring private commit metadata.

Creating a destination, changing visibility, rewriting canonical history,
force-pushing or deleting remote refs requires the corresponding explicit Owner
choice. Repository/issue URLs are now recorded in Python package metadata. A monitored
private security-reporting channel still needs to be selected or confirmed.

## Local candidate boundary

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
design-intake tests that require excluded screenshots/inventory still need a
synthetic-fixture migration before claiming an entire archive suite is runnable.
Do not weaken those checks or distribute the private assets to obtain green CI.

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

Focused results at this checkpoint: backend collection 20 passed; native bridge
18 passed; extension protocol/worker 7 passed. One frontend run passed 25 tests
across collection, Paper Bank and attachment components; a later collection-only
7-test run checked recovery review fixes. These overlap and are not a full-suite
total. Frontend TypeScript build passed. Publication audit/boundary checks passed
161 cases, with later focused scanner and committed-policy rechecks; their counts
also overlap. The pre-existing frozen `STORAGE_PORTABILITY` document mismatch
remains a failure, and excluded private design assets limit historical tests.
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
