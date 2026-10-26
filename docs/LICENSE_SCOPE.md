# License Scope

The project is licensed **Apache-2.0** (SPDX: `Apache-2.0`), `Copyright 2026 CAPTW`.
The full text is in [`LICENSE`](../LICENSE) at the repository root.

This page exists because "the repository is Apache-2.0" would be an inaccurate
sentence. The license covers the project's own source. It does not, and cannot,
grant rights over the third-party and generated material that also lives here.

## What the license covers

Project-owned material in the public source snapshot:

- application source (`app/`), frontend source (`web/src/`), tooling (`tools/`),
  scripts (`scripts/`);
- tests;
- project-authored documentation in `docs/` and the root Markdown files;
- example and template configuration (`.env.example`, `.mcp.json.example`);
- build and packaging configuration.

## What the license does **not** cover

**Dependencies.** No dependency code is bundled, vendored, or redistributed by
this project — not in the repository and not in the source archive. Every
dependency is fetched from its own registry at install time and remains under its
own terms. See [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

One of them deserves a direct warning. The optional `fulltext` extra installs
**PyMuPDF**, whose PyPI metadata reads *"Dual Licensed - GNU AFFERO GPL 3.0 or
Artifex Commercial License"*. It is not installed by default and the application
runs without it — every call site imports it lazily inside `try`/`except` and
degrades to a `missing_dep` result. If you install `[fulltext]` and deploy the
result, the terms of that combination are yours to resolve, and this project's
Apache-2.0 grant does not settle them. That question is unaffected by which
license this project chose.

**Retrieved research content.** Article PDFs, article full text, JATS XML, and
abstracts are third-party works retrieved from arXiv, PMC and similar sources.
Each carries its own terms — arXiv per-paper licenses vary, and hosting does not
imply a redistribution right. The project's full-text path accepts only CC0 or
CC BY sources at retrieval time, which is a *runtime* rights check on retrieved
content and says nothing about this project's license.

**Datasets and corpora**, including any private or owner corpus.

**Runtime data**: `storage/**`, runtime databases, queue and profile state,
notification records.

**Generated output**: reports (Markdown and PDF), weekly journals, run
artifacts, experiment scaffolds, exported bundles, archives.

**Raw Provider output**, including captured proof artifacts.

**Anything excluded from the public source package.** The packaging tool reports
these explicitly; see [`OPEN_SOURCE_READINESS.md`](OPEN_SOURCE_READINESS.md).

## Applying the license to a file

New and modified project-owned source files carry:

```
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
```

There has been no repository-wide header retrofit. Absence of a header does not
place a project-owned file outside the license — `LICENSE` and the package
metadata govern. Headers are not added to JSON, lockfiles, generated manifests,
binary files, the license text itself, or any file of uncertain ownership.

## No NOTICE file

Apache-2.0 requires propagating a `NOTICE` file only when one exists. This
project has none: it carries no inherited attribution obligations, has no
vendored third-party source, and bundles no dependency code. Creating an empty
one would imply obligations that do not exist.

## Not legal advice

This document and the dependency inventory are technical records assembled from
lockfiles and official registry metadata. They are not legal advice and not a
license-compatibility clearance. Before any official release or redistribution,
have the dependency terms and the retrieved-content rights reviewed by someone
qualified to do that.
