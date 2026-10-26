#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Build a deterministic source-distribution candidate from committed Git state.

This is not a release. It produces a reviewable candidate archive plus an adjacent
manifest, so the source that would be published can be inspected before anyone
publishes anything.

Design notes:

- The file list comes from ``git ls-files`` at an exact commit, never from the
  working tree. A dirty checkout, a stray runtime file or an untracked secret can
  therefore never reach the archive.
- Exclusion is *fail closed*. A tracked path matching a forbidden category aborts
  the build with a non-zero exit rather than being quietly dropped, because a
  silent skip is how prohibited content ends up published.
- The archive is written outside every repository and worktree. Writing it inside
  the tree would make the next build's input depend on the previous build's output.
- The manifest lives beside the archive rather than inside it, so the archive hash
  can be recorded without a self-hash cycle.

Usage:
    python tools/package_source_distribution.py --output-dir <dir outside the repo>
    python tools/package_source_distribution.py --check-only
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

# Static and runtime views are split deliberately: mypy resolves the canonical `tools.*`
# modules, while the runtime keeps the same-directory fallback that lets this file run as
# a standalone script inside an extracted source with no package context.
if TYPE_CHECKING:
    from tools import git_object_source
else:
    try:  # imported as `tools.package_source_distribution` by the test suite
        from tools import git_object_source
    except ImportError:  # run directly as a script, where `tools` is not a package root
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import git_object_source  # type: ignore[no-redef]

# The owner-path vocabulary already exists; configuration examples are judged
# against that same definition rather than a private copy of it. Optional so the
# packager still runs if only this module is present.
if TYPE_CHECKING:
    from tools import public_path_scan
else:
    try:
        from tools import public_path_scan
    except ImportError:  # pragma: no cover - exercised only outside the package layout
        try:
            import public_path_scan  # type: ignore[no-redef]
        except ImportError:
            public_path_scan = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from tools import public_content_policy
else:
    try:
        from tools import public_content_policy
    except ImportError:  # pragma: no cover - exercised only outside the package layout
        import public_content_policy  # type: ignore[no-redef]

# Tier 1 - fatal. These must never be tracked at all. Finding one means the
# repository itself has a problem that a packaging filter must not paper over, so
# the build aborts rather than quietly excluding it.
FATAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("**/*.sqlite3", "runtime database"),
    ("**/*.sqlite3-wal", "runtime database sidecar"),
    ("**/*.sqlite3-shm", "runtime database sidecar"),
    ("**/*.db", "runtime database"),
    ("**/*.pem", "private key material"),
    ("**/*.key", "private key material"),
    ("**/*.pfx", "private key material"),
    ("**/id_rsa", "private key material"),
)

# Tier 2 - excluded and reported. These categories are legitimately unsuitable for
# a public source distribution but are already tracked in this repository's
# history. Deleting or untracking them is an owner decision, not a packaging
# decision, so the build excludes them, records every one in the manifest, and
# surfaces them for disposition. Silence in either direction would be wrong.
PROHIBITED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("storage/**", "runtime storage root"),
    ("output/**", "generated output"),
    ("reports/**", "generated report output"),
    ("**/*.pdf", "licensed or generated document"),
    ("**/*.xrmlbundle", "generated project bundle"),
    ("**/*.zip", "generated archive"),
    ("**/*.log", "log output"),
    # A tracked native executable is excluded by its path alone. Deciding this from
    # the suffix rather than from the bytes is the point: the file is reported and
    # dropped without the packager ever opening -- let alone running -- a payload it
    # has no way to vouch for. Only `.exe` is claimed here; a wider native-binary
    # group would need its own inventory and owner decision.
    ("**/*.exe", "tracked native executable"),
    ("**/*.trace.zip", "browser trace"),
    ("**/*.webm", "recorded video"),
    ("**/node_modules/**", "dependency directory"),
    ("**/.venv/**", "dependency directory"),
    ("**/__pycache__/**", "bytecode cache"),
    ("**/.pytest_cache/**", "test cache"),
)

# Tier 2b - internal artifacts. Generated internal evidence that nothing in the
# product, the tests or the public documentation references. Reported under its
# own heading rather than folded into "prohibited" (which means owner-disposition
# pending) or "excluded" (ordinary build/runtime noise), because the distinction
# is what the reader needs: this is internal record-keeping, not a runtime leak.
#
# Deliberately exact paths, not a directory glob. A broad `design/**` rule would
# also drop genuine public design documentation, and this checkpoint is scoped to
# the one artifact whose role was actually established.
INTERNAL_ARTIFACT_PATHS: tuple[tuple[str, str], ...] = (
    ("design/stitch/mcp-visibility-report.json",
     "generated internal MCP visibility evidence; referenced by no source, test or public doc"),
    (".gemini/settings.json", "local agent configuration with machine-specific connection settings"),
    (".gemini/settings.local.json", "local agent configuration with machine-specific connection settings"),
    ("design/stitch/manual-export-inventory.json", "historical local design intake inventory with machine path"),
    ("Source/AI Research Agent.png", "reference image; redistribution rights not established"),
    ("design/input/current-dashboard-scroll-1.png", "internal development screenshot"),
    ("design/input/current-dashboard-scroll-2.png", "internal development screenshot"),
    ("design/input/current-dashboard-ui-debug.png", "internal development screenshot"),
    ("design/input/current-dashboard-wide.png", "internal development screenshot"),
    ("design/stitch/manual/stitch-narrow.png", "historical design screenshot; redistribution rights not established"),
    ("design/stitch/manual/stitch-wide-1440.png", "historical design screenshot; redistribution rights not established"),
    ("design/stitch/manual/stitch-wide-1920.png", "historical design screenshot; redistribution rights not established"),
    ("design/stitch/stitch-screen-wide.png", "historical design screenshot; redistribution rights not established"),
    ("design/stitch/stitch-thumbnail.png", "historical design screenshot; redistribution rights not established"),
)


# Tier 3 - ordinary omissions. Local working state that simply does not belong in
# a source distribution and raises no disposition question.
EXCLUDED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("data/queues/**", "accumulated local research queue state"),
    ("data/papers/**", "local paper corpus state"),
    ("notes/**", "generated local notes"),
    ("**/__snapshots__/**", "visual regression snapshots"),
)

# Real configuration is fatal at any depth and under any suffix. Enumerating
# `.env`, `.env.local`, `.env.production` was not enough: `.env.staging` and a
# nested `app/.env` both walked past the list. Match the *stem* instead and carve
# out only the placeholder variants, so a suffix nobody has thought of yet still
# fails closed.
SECRET_CONFIG_STEMS: tuple[tuple[str, str], ...] = (
    (".env", "actual environment file"),
    (".mcp.json", "actual MCP configuration"),
)
PLACEHOLDER_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template", ".dist")

# `.envrc` needs a stricter rule than the other two, and gets its own branch below
# rather than a row in the table above. Two reasons. It is *executed* -- direnv
# sources it on directory entry -- so shipping one is worse than shipping an inert
# key/value file. And the suffix carve-out that is safe for `.env` is not safe
# here: only these three exact basenames are public, so `.envrc.dist`,
# `.envrc-template` and `.envrc.example.bak` all stay fatal instead of slipping
# through on a suffix that merely looks like a placeholder.
ENVRC_PREFIX = ".envrc"
ENVRC_EXAMPLE_NAMES: frozenset[str] = frozenset({".envrc.example", ".envrc.sample", ".envrc.template"})


def _secret_config_reason(path: str) -> str | None:
    """Return why `path` is real configuration, or None if it is a placeholder."""
    name = PurePosixPath(path).name.lower()
    # Checked before the placeholder carve-out so the exact allowlist wins.
    if name.startswith(ENVRC_PREFIX):
        return None if name in ENVRC_EXAMPLE_NAMES else "actual direnv environment file"
    if name.endswith(PLACEHOLDER_SUFFIXES):
        return None
    for stem, why in SECRET_CONFIG_STEMS:
        if name == stem or name.startswith(f"{stem}."):
            return why
    return None


def is_placeholder_config(path: str) -> bool:
    """Whether `path` is a configuration example that ships publicly."""
    name = PurePosixPath(path).name.lower()
    if name in ENVRC_EXAMPLE_NAMES:
        return True
    if name.startswith(ENVRC_PREFIX):
        return False
    return name.endswith(PLACEHOLDER_SUFFIXES) and any(
        name.startswith(stem) for stem, _ in SECRET_CONFIG_STEMS
    )


# Configuration examples are the one secret-adjacent family that ships, so they are
# the one family whose *contents* are checked. The vocabulary deliberately mirrors
# the product's own secret detector (`app/services/evidence_manifest_admission.py`)
# instead of inventing a second, weaker one; `tests/test_source_distribution_packaging.py`
# pins that the two stay aligned.
# The product's `_SECRET_KEYS` plus `passwd`; a strict superset, kept deliberately
# narrow. An earlier draft also matched `auth`, which flagged `AUTH_MODE=disabled`
# -- an ordinary setting whose value is meant to be readable.
SECRET_KEY_WORDS: tuple[str, ...] = (
    "secret", "token", "api_key", "apikey", "password", "passwd",
    "private_key", "credential",
)
EXAMPLE_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private-key-block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("openai-key", r"\bsk-(?:live|test|proj)?-?[A-Za-z0-9_-]{16,}\b"),
    ("github-token", r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}\b"),
    ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("bearer-token", r"\bBearer\s+[A-Za-z0-9._-]{12,}\b"),
    ("slack-token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)

# An assignment's value is safe when it is empty or visibly a stand-in. The real
# `.env.example` leaves every secret key empty, which is the intended shape.
# Horizontal whitespace only. `\s*` after the separator matches a newline, so an
# empty `SESSION_SECRET=` borrowed the *next* line as its value and reported the
# real `.env.example` as credential-bearing.
_ASSIGNMENT = re.compile(r'(?m)^[ \t]*(?:export[ \t]+)?"?([A-Za-z_][A-Za-z0-9_]*)"?[ \t]*[:=][ \t]*(.*)$')
# Matched against the WHOLE cleaned value, never as a prefix. Prefix matching let
# `API_KEY=changeme_actual_secret_12345` pass as a placeholder because it merely
# started with one -- which is exactly the shape a half-edited example file takes.
_PLACEHOLDER_VALUE = re.compile(
    r"""|<[^>]*>|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Z_]+%"""
    r"""|your[-_ ][A-Za-z0-9_ -]*|x{3,}|\.{3}|change[-_ ]?me|place[-_ ]?holder"""
    r"""|replace[-_ ]?(?:me|this)?|todo|example|dummy|fake|sample|none|null|unset|\*{3,}""",
    re.IGNORECASE,
)


def _is_placeholder_value(raw: str) -> bool:
    """Whether an assignment's value is a stand-in rather than a real one."""
    value = raw.split("#", 1)[0].strip().strip("\"'").strip()
    return _PLACEHOLDER_VALUE.fullmatch(value) is not None


def example_config_findings(data: bytes) -> list[str]:
    """Rule ids for real-looking values in a configuration example.

    Returns rule identifiers only. A matched literal is never returned, logged or
    raised -- reporting a leaked credential by printing it would republish it.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["not-utf8-text"]
    found: list[str] = []
    for rule, pattern in EXAMPLE_SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(rule)
    for key, value in _ASSIGNMENT.findall(text):
        if not any(word in key.lower() for word in SECRET_KEY_WORDS):
            continue
        if _is_placeholder_value(value):
            continue
        found.append("credential-like-value")
        break
    if public_path_scan is not None and public_path_scan.OWNER_ROOTS.search(text):
        found.append("owner-machine-path")
    return found


REQUIRED_PATHS: tuple[str, ...] = (
    "pyproject.toml",
    "README.md",
    "app/main.py",
    "web/package.json",
    # A source distribution without its license terms is not redistributable, so a
    # missing LICENSE is a build failure rather than a documentation gap.
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/LICENSE_SCOPE.md",
    "uv.lock",
    "web/package-lock.json",
)

# The SPDX expression the project is released under. Recorded in the manifest so a
# candidate can be traced to its terms without opening the archive.
PROJECT_LICENSE = "Apache-2.0"
PROJECT_COPYRIGHT = "Copyright 2026 CAPTW"


class PackagingError(RuntimeError):
    """Raised when the distribution cannot be built safely."""


@dataclass
class BuildResult:
    base_head: str
    base_tree: str
    included: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    prohibited: list[tuple[str, str]] = field(default_factory=list)
    internal_historical: list[tuple[str, str]] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)
    unpacked_bytes: int = 0
    archive_path: Path | None = None
    archive_sha256: str | None = None
    archive_bytes: int = 0
    manifest_path: Path | None = None
    source_epoch: int = 0
    entries: list = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _matches(path: str, pattern: str) -> bool:
    """Case-insensitive glob match over a POSIX-style repository path.

    Case folding is deliberate. Git on Windows and macOS is case-insensitive by
    default, so a secret can be committed as ``.ENV`` or ``Secret.PEM`` and a
    case-sensitive filter would wave it straight through into the archive.

    One implementation is used on every Python version. ``PurePath.full_match``
    is 3.13+, and keeping a second matcher for newer interpreters would mean the
    tests exercise one code path while some contributors run another.
    """
    return _glob_match(path.lower(), pattern.lower())


def _glob_match(path: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase

    if pattern.startswith("**/"):
        tail = pattern[3:]
        if tail.endswith("/**"):
            stem = tail[:-3]
            return any(part == stem for part in PurePosixPath(path).parts[:-1])
        return fnmatchcase(path, pattern) or fnmatchcase(PurePosixPath(path).name, tail) or fnmatchcase(path, f"*/{tail}")
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatchcase(path, pattern)


def _resolves_inside(repo: Path, candidate: Path) -> bool:
    try:
        return candidate.resolve().is_relative_to(repo.resolve())
    except OSError:
        return False


INTERNAL_ARTIFACT_LOOKUP = {path: why for path, why in INTERNAL_ARTIFACT_PATHS}


def internal_artifacts(paths: list[str]) -> list[tuple[str, str]]:
    """Internal artifacts among `paths`, reported under their own manifest heading.

    Kept beside `classify` rather than folded into its return so the four-way
    tier contract every caller already depends on stays intact; these paths are
    also present in `classify`'s excluded list, which is what actually keeps them
    out of the archive.
    """
    return [(path, INTERNAL_ARTIFACT_LOOKUP[path]) for path in paths if path in INTERNAL_ARTIFACT_LOOKUP]


def classify(paths: list[str]) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """Split tracked paths into (included, excluded, prohibited, fatal).

    Tiers are evaluated in severity order so a path is reported under the most
    serious category it matches.
    """
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    internal: list[tuple[str, str]] = []
    prohibited: list[tuple[str, str]] = []
    fatal: list[tuple[str, str]] = []
    for path in paths:
        reason = _secret_config_reason(path)
        if reason:
            fatal.append((path, reason))
            continue
        hit = next(((path, why) for pat, why in FATAL_PATTERNS if _matches(path, pat)), None)
        if hit:
            fatal.append(hit)
            continue
        hit = next(((path, why) for pat, why in PROHIBITED_PATTERNS if _matches(path, pat)), None)
        if hit:
            prohibited.append(hit)
            continue
        why = INTERNAL_ARTIFACT_LOOKUP.get(path)
        if why:
            internal.append((path, why))
            continue
        hit = next(((path, why) for pat, why in EXCLUDED_PATTERNS if _matches(path, pat)), None)
        if hit:
            excluded.append(hit)
            continue
        included.append(path)
    return included, excluded + internal, prohibited, fatal


def build(repo: Path, output_dir: Path | None, check_only: bool = False,
          allow_dirty: bool = False) -> BuildResult:
    # Source authority is the commit tree, not the index and not the directory.
    # `git ls-files` reports the index, which agrees with the commit only while the
    # tree is clean -- and even then it says nothing about the bytes on disk.
    try:
        head, tree, source_epoch = git_object_source.resolve_commit(repo)
        entries = git_object_source.read_tree(repo, head)
    except git_object_source.GitObjectError as exc:
        raise PackagingError(str(exc)) from exc
    if not entries:
        raise PackagingError("no entries resolved from the commit tree")
    entry_by_path = {entry.path: entry for entry in entries}
    tracked = [entry.path for entry in entries]

    # A clean checkout is the default review contract. With --allow-dirty, the
    # committed tree still supplies every byte, including its content policy;
    # pending edits never become an undocumented part of the candidate.
    dirty = [line[3:] for line in _git(repo, "status", "--porcelain", "--untracked-files=no").splitlines() if line.strip()]
    if dirty and not allow_dirty:
        listed = "\n".join(f"  {path}" for path in sorted(dirty))
        raise PackagingError(
            "tracked files are modified; commit the intended candidate first or "
            f"pass --allow-dirty to package only committed {head[:12]}:\n" + listed
        )

    included, excluded, prohibited, fatal = classify(tracked)
    internal = internal_artifacts(tracked)

    if fatal:
        listed = "\n".join(f"  {path}  [{why}]" for path, why in sorted(fatal))
        raise PackagingError(
            "tracked paths must never exist in this repository; refusing to build:\n" + listed
        )

    # Every tracked document must carry a role before anything ships. An unlisted
    # document fails the build here rather than defaulting into the archive, which
    # is the whole point: new documentation arrives closed.
    # The policy itself must come from the same commit. Reading the checkout
    # here let an uncommitted policy promote private docs under --allow-dirty.
    policy_entry = entry_by_path.get(public_content_policy.POLICY_TRACKED_PATH)
    try:
        with git_object_source.BlobReader(repo) as blobs:
            loaded = public_content_policy.load(data=blobs.read(policy_entry)) if policy_entry is not None else None
        policy = public_content_policy.validate_against(tracked, loaded)
    except (public_content_policy.PolicyError, git_object_source.GitObjectError) as exc:
        raise PackagingError(str(exc)) from exc

    # Documents the policy does not admit leave `included` for their own heading.
    # Their bytes stay in the repository untouched; only the public snapshot omits
    # them, pending the individual review a later lane performs.
    withheld = {p for p in included
                if policy.is_document(p) and policy.role_of(p) not in public_content_policy.SHIPPING_ROLES}
    internal_historical = sorted(
        (p, policy.document_rules.get(p, "internal pending individual review")) for p in withheld
    )
    included = [p for p in included if p not in withheld]
    result_roles = {p: public_content_policy.content_role_for_path(p, "included", policy) for p in included}

    # Name-based policy got the examples this far; only their contents can settle
    # whether they are actually placeholders. Just those few blobs are read, and
    # before any archive work, so `--check-only` refuses them too.
    examples = sorted(p for p in included if is_placeholder_config(p))
    if examples:
        unsafe: list[tuple[str, str]] = []
        try:
            with git_object_source.BlobReader(repo) as blobs:
                for path in examples:
                    unsafe += [(path, rule) for rule in example_config_findings(blobs.read(entry_by_path[path]))]
        except git_object_source.GitObjectError as exc:
            raise PackagingError(str(exc)) from exc
        if unsafe:
            listed = "\n".join(f"  {path}  [{rule}]" for path, rule in unsafe)
            raise PackagingError(
                "configuration examples must contain no real values; refusing to build:\n" + listed
            )

    missing = [p for p in REQUIRED_PATHS if p not in included]
    if missing:
        raise PackagingError(f"required source paths missing from the candidate: {', '.join(missing)}")

    result = BuildResult(base_head=head, base_tree=tree, included=included, excluded=excluded,
                         prohibited=prohibited, internal_historical=internal_historical,
                         roles=result_roles)
    # Sizes come from the tree, not `stat()`: a checkout can materialize a blob at a
    # different length than the object it came from, which is the whole defect.
    result.unpacked_bytes = sum(entry_by_path[p].size for p in included)
    result.source_epoch = source_epoch
    result.entries = [entry_by_path[p] for p in included]

    if check_only:
        return result

    if output_dir is None:
        raise PackagingError("--output-dir is required unless --check-only is used")
    resolved_out = output_dir.expanduser().resolve()
    repo_resolved = repo.resolve()
    if resolved_out == repo_resolved or repo_resolved in resolved_out.parents or resolved_out.is_relative_to(repo_resolved):
        raise PackagingError("output directory must be outside the repository")
    resolved_out.mkdir(parents=True, exist_ok=True)

    # Every input below comes from the commit: no clock, no host, no checkout.
    stem = f"xrml-research-agent-source-{head[:12]}"
    archive = resolved_out / f"{stem}.tar.gz"

    # `tarfile.open(mode="w:gz")` stamps the gzip header with the current time and
    # embeds the output filename; both differ between runs. Drive gzip explicitly.
    members: list[dict[str, object]] = []
    digest = hashlib.sha256()
    raw = io.BytesIO()
    try:
        with git_object_source.BlobReader(repo) as blobs:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=source_epoch, compresslevel=9) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                    for entry in sorted(result.entries, key=lambda e: e.path):
                        payload_bytes = blobs.read(entry)
                        info = tarfile.TarInfo(f"{stem}/{entry.path}")
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mtime = source_epoch
                        if entry.is_symlink:
                            target = payload_bytes.decode("utf-8", errors="strict")
                            if not git_object_source.link_target_is_safe(target):
                                raise PackagingError(
                                    f"symlink {entry.path} points outside the archive; refusing to build"
                                )
                            info.type = tarfile.SYMTYPE
                            info.linkname = target
                            info.size = 0
                            info.mode = 0o777
                            tar.addfile(info)
                        else:
                            info.type = tarfile.REGTYPE
                            info.size = len(payload_bytes)
                            # Mode comes from the tree, so the executable bit survives
                            # checkouts on filesystems that cannot represent it.
                            info.mode = entry.archive_mode
                            tar.addfile(info, io.BytesIO(payload_bytes))
                        digest.update(entry.path.encode("utf-8") + b"\0")
                        digest.update(hashlib.sha256(payload_bytes).digest())
                        members.append({
                            "path": entry.path,
                            "archive_path": f"{stem}/{entry.path}",
                            "git_mode": entry.mode,
                            "object_type": entry.object_type,
                            "object_id": entry.oid,
                            "object_bytes": entry.size,
                            "member_sha256": hashlib.sha256(payload_bytes).hexdigest(),
                            "classification": "included",
                        })
    except git_object_source.GitObjectError as exc:
        raise PackagingError(str(exc)) from exc
    payload = raw.getvalue()
    archive.write_bytes(payload)

    # A digest over (path, object bytes) pairs. Unlike the archive hash this ignores
    # container framing, so it still matches if the tar format ever changes.
    content_digest = digest.hexdigest()
    result.archive_path = archive
    result.archive_bytes = len(payload)
    result.archive_sha256 = hashlib.sha256(payload).hexdigest()

    manifest = {
        "schema": "xrml_source_distribution_manifest",
        "version": "1.0.0",
        "commit_oid": head,
        "tree_oid": tree,
        "source_byte_origin": "git_object_database",
        "base_head": head,
        "base_tree": tree,
        "archive_filename": archive.name,
        "archive_bytes": result.archive_bytes,
        "archive_sha256": result.archive_sha256,
        "file_count": len(included),
        "unpacked_bytes": result.unpacked_bytes,
        "root_prefix": stem,
        "project_license": PROJECT_LICENSE,
        "project_copyright": PROJECT_COPYRIGHT,
        # From the archived member, not from disk. Reading the checkout here was the
        # last surviving working-tree dependency and made the manifest differ between
        # two clean checkouts even after the archive itself became identical.
        "license_file_sha256": next(
            (m["member_sha256"] for m in members if m["path"] == "LICENSE"), None
        ),
        "source_date_epoch": source_epoch,
        "package_content_digest": content_digest,
        "members": members,
        "reproducible": True,
        "excluded": [{"path": p, "reason": why} for p, why in sorted(excluded)],
        "internal_artifacts_excluded": [{"path": p, "reason": w} for p, w in sorted(internal)],
        "internal_historical_excluded": [{"path": p, "rule": r} for p, r in internal_historical],
        "prohibited_tracked_excluded": [{"path": p, "reason": why} for p, why in sorted(prohibited)],
        "fatal_found": [],
        # Role accounting. Every tracked path lands in exactly one bucket, and the
        # included subtotals add back up to `file_count`; a build that cannot say
        # that has lost track of something.
        "content_policy_version": policy.version,
        "curated_public_allowlist": list(policy.curated_allowlist),
        "role_counts": {
            "included_public_required": sum(1 for r in result_roles.values() if r == "PUBLIC_REQUIRED"),
            "included_public_curated": sum(1 for r in result_roles.values() if r == "PUBLIC_CURATED"),
            "included_test_synthetic": sum(1 for r in result_roles.values() if r == "TEST_SYNTHETIC"),
            "internal_historical_excluded": len(internal_historical),
            "internal_artifacts_excluded": len(internal),
            "ordinary_excluded": len(excluded) - len(internal),
            "prohibited_tracked": len(prohibited),
            "fatal": 0,
            "unclassified_documents": 0,
            "unresolved_dispositions": 0,
        },
        "manifest_in_archive": False,
        "built_from_clean_tree": not dirty,
        "is_official_release": False,
        "notes": "Local source-distribution candidate for review. Not a release.",
    }
    manifest_path = resolved_out / f"{stem}.manifest.json"
    manifest_path.write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    result.manifest_path = manifest_path
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--check-only", action="store_true", help="validate the boundary without writing an archive")
    parser.add_argument("--allow-dirty", action="store_true", help="package committed HEAD only, ignoring pending working-tree edits")
    args = parser.parse_args(argv)

    try:
        result = build(args.repo, args.output_dir, check_only=args.check_only,
                       allow_dirty=args.allow_dirty)
    except PackagingError as exc:
        print(f"source-distribution check failed: {exc}", file=sys.stderr)
        return 2

    print(f"base head      {result.base_head}")
    print(f"base tree      {result.base_tree}")
    print(f"included files {len(result.included)}")
    print(f"excluded files {len(result.excluded)}")
    print(f"prohibited tracked (excluded, needs owner disposition) {len(result.prohibited)}")
    for path, why in sorted(result.prohibited):
        print(f"  - {path}  [{why}]")
    print(f"unpacked bytes {result.unpacked_bytes}")
    if result.archive_path:
        print(f"archive        {result.archive_path.name}")
        print(f"archive bytes  {result.archive_bytes}")
        print(f"archive sha256 {result.archive_sha256}")
        print(f"manifest       {result.manifest_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
