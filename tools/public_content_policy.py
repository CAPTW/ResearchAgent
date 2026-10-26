#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""One authoritative answer to "may this document ship?".

The packager and the path scanner previously each carried their own idea of what
a public document was -- the scanner a hardcoded six-name set, the packager
nothing at all. Two lists meant a document could satisfy one and bypass the
other, and a newly added file satisfied neither, so it shipped by default. That
is the failure this module exists to make impossible.

The policy is data, committed alongside the code, and it is *exhaustive*: every
tracked document-like path is named. A document that is not named has no role,
and a path with no role fails the build rather than defaulting to public. New
documents therefore arrive closed, and opening one is a deliberate commit.

The roles are structural, not editorial. `INTERNAL_HISTORICAL` here means "not
yet individually reviewed", not "judged unfit to publish" -- a later lane
promotes what deserves promoting. Defaulting to internal is what makes that
review safe to defer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath

POLICY_FILENAME = "public_content_policy.json"
POLICY_PATH = Path(__file__).resolve().parent / POLICY_FILENAME
# Tracked repository-relative path of the policy and its loader, so the packager
# can assert both actually ship: an archive carrying documents but not the rule
# that admitted them cannot be re-validated by whoever receives it.
POLICY_TRACKED_PATH = f"tools/{POLICY_FILENAME}"
LOADER_TRACKED_PATH = "tools/public_content_policy.py"

SCHEMA = "xrml_public_content_policy"
SUPPORTED_VERSIONS = ("1.0.0",)

# A disposition records that a path's public-snapshot question is settled. Roles
# say where something belongs; these say nobody still owes it a decision.
DISPOSITION_STATUSES: frozenset[str] = frozenset({
    "public_required_final",
    "public_curated_final",
    "internal_historical_final",
    "internal_artifact_final",
    "test_synthetic_final",
})

# The one disposition each role may carry. Used to reject a policy that answers
# the same question two ways.
ROLE_DISPOSITION: dict[str, str] = {
    "PUBLIC_REQUIRED": "public_required_final",
    "PUBLIC_CURATED": "public_curated_final",
    "INTERNAL_HISTORICAL": "internal_historical_final",
    "INTERNAL_ARTIFACT": "internal_artifact_final",
    "TEST_SYNTHETIC": "test_synthetic_final",
}

ROLES: tuple[str, ...] = (
    "PUBLIC_REQUIRED",
    "PUBLIC_CURATED",
    "INTERNAL_HISTORICAL",
    "INTERNAL_ARTIFACT",
    "TEST_SYNTHETIC",
    "ORDINARY_EXCLUDED",
    "PROHIBITED_TRACKED",
    "FATAL",
)
PUBLIC_ROLES: frozenset[str] = frozenset({"PUBLIC_REQUIRED", "PUBLIC_CURATED"})
# Roles whose members ship. `TEST_SYNTHETIC` is included because the source
# distribution has to be self-testable; that was proven in an earlier checkpoint
# and this policy must not silently undo it.
SHIPPING_ROLES: frozenset[str] = PUBLIC_ROLES | {"TEST_SYNTHETIC"}


class PolicyError(RuntimeError):
    """Raised when the policy is malformed, stale or incomplete."""


@dataclass(frozen=True)
class ContentPolicy:
    version: str
    documents: dict[str, str]
    document_rules: dict[str, str]
    extensions: frozenset[str]
    exact_names: frozenset[str]
    prefixes: tuple[str, ...] = ()
    dispositions: dict[str, str] = field(default_factory=dict)
    reason_codes: dict[str, str] = field(default_factory=dict)
    curated_allowlist: tuple[str, ...] = ()
    curated_cap: int = 12

    def is_document(self, path: str) -> bool:
        """Whether `path` is governed by the document inventory.

        Deliberately a property of the path, not of the inventory: asking the
        inventory whether a path is a document would make every unlisted document
        a non-document, which is precisely the fail-open shape being removed.

        Extension alone was not enough. `docs/audit/` also holds a `.lock.json`
        record, which is documentation by location and by nothing else -- it shipped
        because `.json` is not a document suffix. Everything under a documentation
        prefix is governed regardless of how it is named.
        """
        # Case-insensitive: `Docs/` and `docs/` are one directory on Windows, and a
        # prefix test that missed the capitalised spelling would be a way in.
        lowered = path.lower()
        if any(lowered.startswith(prefix.lower()) for prefix in self.prefixes):
            return True
        # Case-insensitive like the other two tests. `License` and `LICENSE` are one
        # file on Windows, and matching only the shouted spelling let the other
        # spellings through ungoverned -- straight into the archive as source.
        name = PurePosixPath(path).name.lower()
        return (name in {n.lower() for n in self.exact_names}
                or PurePosixPath(path).suffix.lower() in self.extensions)

    def role_of(self, path: str) -> str | None:
        """The document's role, or None when it is a document nobody classified."""
        return self.documents.get(path)

    def public_documents(self) -> frozenset[str]:
        return frozenset(p for p, role in self.documents.items() if role in PUBLIC_ROLES)

    def shipping_documents(self) -> frozenset[str]:
        return frozenset(p for p, role in self.documents.items() if role in SHIPPING_ROLES)

    def unresolved(self, tracked: list[str]) -> list[str]:
        """Governed documents carrying no final disposition.

        A role says where a path belongs; a disposition says the question is
        settled. Keeping them separate is what stops "pending review" from
        quietly becoming an answer.
        """
        return sorted(p for p in tracked
                      if self.is_document(p) and p in self.documents
                      and p not in self.dispositions)

    def unclassified(self, tracked: list[str]) -> list[str]:
        """Tracked documents with no policy entry -- the fail-closed trigger."""
        return sorted(p for p in tracked if self.is_document(p) and p not in self.documents)

    def stale(self, tracked: list[str]) -> list[str]:
        """Policy entries whose document no longer exists.

        Stale entries are an error rather than a harmless leftover: a path that
        stops existing and later returns would silently inherit a role decided
        for a file nobody can now inspect.
        """
        present = set(tracked)
        return sorted(p for p in self.documents if p not in present)


def load(path: Path | None = None, *, data: bytes | None = None) -> ContentPolicy:
    """Read and validate the policy. Every failure here is a build failure."""
    source = path or POLICY_PATH
    try:
        raw = json.loads(data.decode("utf-8") if data is not None else source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"public content policy not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"public content policy is not valid JSON: {exc}") from exc

    if raw.get("schema") != SCHEMA:
        raise PolicyError(f"unexpected policy schema: {raw.get('schema')!r}")
    version = raw.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise PolicyError(f"unsupported policy version: {version!r}")

    documents = raw.get("documents")
    if not isinstance(documents, dict) or not documents:
        raise PolicyError("policy carries no document inventory")

    bad_role = {p: r for p, r in documents.items() if r not in ROLES}
    if bad_role:
        listed = ", ".join(f"{p} [{r}]" for p, r in sorted(bad_role.items())[:5])
        raise PolicyError(f"policy assigns unknown roles: {listed}")

    # JSON objects cannot hold a duplicate key twice, so an overlap can only be a
    # case-fold collision -- two entries that are one path on a Windows checkout.
    folded: dict[str, str] = {}
    for candidate in sorted(documents):
        prior = folded.setdefault(candidate.lower(), candidate)
        if prior != candidate:
            raise PolicyError(f"policy paths collide when case-folded: {prior} / {candidate}")

    statuses = raw.get("dispositions") or {}
    unknown = {p: v for p, v in statuses.items() if v not in DISPOSITION_STATUSES}
    if unknown:
        raise PolicyError(f"policy uses unknown disposition statuses: {sorted(unknown.items())[:3]}")

    absolute = [p for p in documents if p.startswith("/") or (len(p) > 1 and p[1] == ":")]
    if absolute:
        raise PolicyError(f"policy contains absolute paths: {absolute[:3]}")

    # Metadata must describe the inventory, not drift alongside it. An orphan entry
    # is a decision about a path nobody can look at, and a disposition that
    # disagrees with its role means two answers to the same question. Checked after
    # the inventory itself is known sound, so its errors are the ones reported.
    for label, table in (("dispositions", statuses), ("reason codes", raw.get("reason_codes") or {})):
        orphans = sorted(set(table) - set(documents))
        if orphans:
            raise PolicyError(f"policy has {label} for paths not in the inventory: {orphans[:3]}")
    contradictions = sorted(
        (p, documents[p], statuses[p]) for p in statuses
        if statuses[p] != ROLE_DISPOSITION[documents[p]]
    )
    if contradictions:
        raise PolicyError(f"policy disposition contradicts its role: {contradictions[:3]}")
    curated_by_role = sorted(p for p, r in documents.items() if r == "PUBLIC_CURATED")
    declared = sorted(raw.get("curated_public_allowlist") or ())
    if curated_by_role != declared:
        raise PolicyError(
            "curated allowlist disagrees with the roles it is supposed to name: "
            f"roles={curated_by_role[:5]} allowlist={declared[:5]}"
        )
    cap = int(raw.get("curated_public_cap", 12))
    if len(declared) > cap:
        raise PolicyError(f"curated allowlist holds {len(declared)} documents; the cap is {cap}")


    return ContentPolicy(
        version=version,
        documents=dict(documents),
        document_rules=dict(raw.get("reason_codes") or raw.get("document_rules") or {}),
        dispositions=dict(raw.get("dispositions") or {}),
        reason_codes=dict(raw.get("reason_codes") or {}),
        curated_allowlist=tuple(raw.get("curated_public_allowlist") or ()),
        curated_cap=int(raw.get("curated_public_cap", 12)),
        extensions=frozenset(e.lower() for e in raw.get("document_extensions") or ()),
        exact_names=frozenset(raw.get("document_exact_names") or ()),
        prefixes=tuple(raw.get("document_prefixes") or ()),
    )


def generate(tracked: list[str], public_required: frozenset[str] = frozenset(),
             extensions: tuple[str, ...] = (".md", ".txt"),
             exact_names: tuple[str, ...] = ("LICENSE",),
             prefixes: tuple[str, ...] = ("docs/",)) -> dict:
    """A deterministic candidate inventory for `tracked`.

    Proposes; never installs. Writing the result is a deliberate act by whoever
    reviews it -- a build that regenerated its own policy would be able to admit a
    document by noticing it, which is the failure this whole module removes.
    """
    ext = frozenset(e.lower() for e in extensions)
    names = frozenset(exact_names)

    def is_doc(path: str) -> bool:
        return (any(path.startswith(pre) for pre in prefixes)
                or PurePosixPath(path).name in names
                or PurePosixPath(path).suffix.lower() in ext)

    documents = {
        path: ("PUBLIC_REQUIRED" if path in public_required else "INTERNAL_HISTORICAL")
        for path in sorted(tracked) if is_doc(path)
    }
    return {
        "schema": SCHEMA,
        "version": SUPPORTED_VERSIONS[0],
        "roles": list(ROLES),
        "public_roles": sorted(PUBLIC_ROLES),
        "document_extensions": list(extensions),
        "document_exact_names": list(exact_names),
        "document_prefixes": list(prefixes),
        "rules": {},
        "documents": documents,
        # A generated candidate is still a settled one: every path it proposes
        # carries a disposition, so the output can be committed as it stands.
        "dispositions": {p: _GENERATED_STATUS[r] for p, r in documents.items()},
        "reason_codes": {p: ("required_by_public_workflow" if r == "PUBLIC_REQUIRED"
                             else "not_required_by_public_workflow")
                         for p, r in documents.items()},
        "curated_public_allowlist": sorted(p for p, r in documents.items() if r == "PUBLIC_CURATED"),
        "curated_public_cap": 12,
    }


_GENERATED_STATUS = {
    "PUBLIC_REQUIRED": "public_required_final",
    "PUBLIC_CURATED": "public_curated_final",
    "INTERNAL_HISTORICAL": "internal_historical_final",
    "INTERNAL_ARTIFACT": "internal_artifact_final",
    "TEST_SYNTHETIC": "test_synthetic_final",
}


@lru_cache(maxsize=1)
def load_cached() -> ContentPolicy:
    """`load()` memoised for callers that ask once per path rather than per run."""
    return load()


def validate_against(tracked: list[str], policy: ContentPolicy | None = None) -> ContentPolicy:
    """Load the policy and require it to describe `tracked` exactly."""
    resolved = policy or load()
    unclassified = resolved.unclassified(tracked)
    if unclassified:
        listed = "\n".join(f"  {p}" for p in unclassified[:20])
        more = f"\n  ... and {len(unclassified) - 20} more" if len(unclassified) > 20 else ""
        raise PolicyError(
            f"{len(unclassified)} tracked document(s) have no role in {POLICY_TRACKED_PATH}.\n"
            "A document with no role is not published by default -- classify it and commit:\n"
            + listed + more
        )
    unresolved = resolved.unresolved(tracked)
    if unresolved:
        listed = "\n".join(f"  {p}" for p in unresolved[:20])
        raise PolicyError(
            f"{len(unresolved)} governed document(s) carry no final disposition:\n" + listed
        )
    stale = resolved.stale(tracked)
    if stale:
        listed = "\n".join(f"  {p}" for p in stale[:20])
        raise PolicyError(
            f"{len(stale)} policy entr(ies) name a document that no longer exists:\n" + listed
        )
    return resolved


def content_role_for_path(path: str, tier: str, policy: ContentPolicy) -> str:
    """The single final role for `path`, given its packaging `tier`.

    `tier` is what the existing classifier already decided (`fatal`, `prohibited`,
    `excluded`, `internal_artifact`, `included`). Safety tiers win outright: a
    document that is also a secret is fatal, not a document. Only paths the
    packager would otherwise ship are handed to the document policy.
    """
    if tier == "fatal":
        return "FATAL"
    if tier == "prohibited":
        return "PROHIBITED_TRACKED"
    if tier == "internal_artifact":
        return "INTERNAL_ARTIFACT"
    if tier == "excluded":
        return "ORDINARY_EXCLUDED"
    if policy.is_document(path):
        # None means unclassified; the caller has already failed the build via
        # `validate_against`, so reaching here at all is a programming error.
        return policy.role_of(path) or "UNCLASSIFIED"
    # Non-document source that survived every safety tier ships as required
    # product source. Tests keep their own role so the manifest can show that the
    # distribution remains self-testable.
    return "TEST_SYNTHETIC" if _is_test_path(path) else "PUBLIC_REQUIRED"


def _is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return any(part in {"tests", "test", "__tests__", "e2e"} for part in parts)
