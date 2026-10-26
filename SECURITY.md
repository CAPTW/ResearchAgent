# Security Policy

## Reporting a vulnerability

The public repository is designated, but a monitored private vulnerability
reporting channel has not yet been confirmed.
Use the maintainer contact already available to you for a private report. The
publication checklist must name a monitored private reporting channel before
public hosting; this source candidate does not invent a contact address.
Please do not open a public issue for an unfixed vulnerability.

Include what you have: affected version or commit, reproduction steps, and impact.
A partial report is more useful than none. Expect an acknowledgement within a few
business days; this is a research project, not a staffed product, so please treat
timelines as best-effort.

Do not include real credentials, API keys, or private research corpus content in a
report. A redacted excerpt is enough.

## Supported versions

This project has not made a tagged release. Only the current default branch is
supported. There is no backport channel.

## Security model

The agent is designed to run locally against a runtime root you control.

**Secrets.** Configuration is read from environment variables. `.env` and
`.mcp.json` are git-ignored and must never be committed; the tracked
`.env.example` and `.mcp.json.example` files carry the shape of the
configuration, never values. `tools/package_source_distribution.py` treats a
tracked `.env`, `.mcp.json`, private key, or runtime database as a *fatal*
condition that aborts packaging rather than something to filter out quietly.

**Runtime data.** All run artifacts, reports, and storage live beneath the
runtime root selected by `XRML_DATA_DIR`. Set it to a disposable directory to
keep an experiment from touching real research data. Runtime state is not part of
the source distribution.

**Artifact references.** Report and run payloads address files through portable
`run:` / `storage:` / `project:` references rather than native paths. The parser
in `app/repositories/artifact_refs.py` rejects `..` traversal and any segment
containing `:`, which closes drive-relative paths (`C:x`) and NTFS alternate data
stream syntax (`report.md:$DATA`) on Windows. Path resolution for report
snapshots is containment-checked against the run's reports directory.

**Providers and network.** Provider execution is opt-in. Without credentials the
system runs in `fallback_only` mode and makes no outbound provider calls. MCP
servers are launched from your own local configuration.

**What is out of scope.** Attacks requiring an attacker who already controls your
runtime root, your environment variables, or the machine's user account. This
project assumes a single trusted local operator.

## Known gaps

- No dependency-vulnerability scanning runs in CI.
- No signed releases or published checksums exist yet, because no release exists.
- The project is Apache-2.0, but retrieved article content, datasets and
  generated output are not covered by it and are not cleared for
  redistribution. See [`docs/LICENSE_SCOPE.md`](docs/LICENSE_SCOPE.md).
