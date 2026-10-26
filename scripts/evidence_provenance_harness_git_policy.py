from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Iterable


POLICY_VERSION = "evidence_provenance_multi_context_v3"

SOURCE_BRANCH = "revive/evidence-provenance-real-backend-browser"
RC_BRANCH = "rc/h008d115-l10-current-chain"

BASE_OID = "b11ce3316cf89060d98a3261b92a839ddefb9701"
SAFE_LINK_OID = "224c8b48652944b6b444d1a1114ae9e2813c2546"
FEATURE_OID = "d0c9707b5b66207c73f1aa313a30d679b536a314"
INTEGRATION_OID = "fc360525fb113f050447636ce4f27d870a7a84f6"
POLICY_BASE_OID = "029307e4c704d55766b1664684700c89310890cf"
PREVIOUS_POLICY_FEATURE_OID = "dd6f9f6e90c4bf4de168d1345aab6bbcc1e2b836"
PREVIOUS_CHECKPOINT_OID = "df52d5b9c15e8dc8d166b092ef95189a0a7f4775"
CURRENT_STATUS_OID = "d1ad512bf5e757c69cfec3e58237f467fe87fddb"
CURRENT_STATUS_TREE = "44cbc5a6ef18e7c104177b07797baa11ac98e917"
CONTAINMENT_OID = "80bd691d1acad3382445ffb017d05d81f71efd16"
CONTAINMENT_TREE = "e732bc041fcc533c8b657ae180af7cc0370906b7"

SAFE_LINK_SUBJECT = "Allow topic-scoped run links in provenance detail"
FEATURE_SUBJECT = "Add real-backend evidence provenance browser test"
INTEGRATION_SUBJECT = "Merge real-backend evidence provenance browser test"
POLICY_FEATURE_SUBJECT = "Generalize real-backend harness Git policy"
POLICY_MERGE_SUBJECT = "Merge reusable real-backend harness policy"
CURRENT_STATUS_SUBJECT = "Record reusable real-backend harness policy validation"
CONTAINMENT_SUBJECT = "Block unscoped provider output activation"
TRANSITION_SUBJECT = "Extend harness policy for provider containment checkpoint"
CHECKPOINT_TWO_SUBJECT = "Merge provider output gate containment checkpoint"
CHECKPOINT_STATUS_SUBJECT = "Record provider output gate containment validation"

TRANSITION_ID = "provider_output_gate_containment"
CHECKPOINT_GENERATION = 2

GENERATION3_PREVIOUS_CHECKPOINT_OID = "ed1cd924c825e446976307ee69a18a049ee47207"
GENERATION3_PREVIOUS_CHECKPOINT_TREE = "b5e8b5a267fb100dc54ec0868647638415258373"
GENERATION3_TRANSITION_FEATURE_OID = "3934330ef2c26ea7be124cc28330c816a2c86f08"
GENERATION3_STATUS_BASE_OID = "6084abe9d61a5c1698f0f1242529423b6b1dd15a"
GENERATION3_STATUS_BASE_TREE = "bc20b5d2e62fcc25f6ce7880d87850ade13b8ead"
PROVIDER_ROUTING_FEATURE_OID = "a345c5ca04fe35837597cdf76d4ee6bad9d82cac"
PROVIDER_ROUTING_FEATURE_TREE = "15a6e299e4bad826e2a3ed87172bcb05f3a06057"

GENERATION3_STATUS_BASE_SUBJECT = "Record provider output gate containment validation"
PROVIDER_ROUTING_FEATURE_SUBJECT = "Implement fail-closed provider routing contract"
GENERATION3_POLICY_FEATURE_SUBJECT = "Prepare generation-3 provider routing checkpoint policy"
GENERATION3_CHECKPOINT_SUBJECT = "Merge provider multi-backend routing generation-3 checkpoint"
GENERATION3_STATUS_SUBJECT = "Record provider multi-backend routing checkpoint validation"
GENERATION3_TRANSITION_ID = "provider_multi_backend_routing_contract"
GENERATION3_CHECKPOINT_GENERATION = 3

STATUS_PATHS = frozenset({"STATUS.md"})
SAFE_LINK_PATHS = frozenset(
    {
        "web/src/components/ProvenanceDetailPanel.tsx",
        "web/src/screens/__tests__/EvidenceProvenanceScreen.test.tsx",
    }
)
FEATURE_PATHS = frozenset(
    {
        "scripts/test_evidence_provenance_real_backend.py",
        "tests/helpers/evidence_provenance_fixture.py",
        "web/playwright.real-backend.config.ts",
        "web/tests/real-backend/evidence-provenance.real-backend.spec.ts",
    }
)
INTEGRATION_PATHS = SAFE_LINK_PATHS | FEATURE_PATHS
POLICY_PATHS = frozenset(
    {
        "scripts/test_evidence_provenance_real_backend.py",
        "scripts/evidence_provenance_harness_git_policy.py",
        "tests/test_evidence_provenance_real_backend_harness_policy.py",
    }
)
CONTAINMENT_PATHS = frozenset(
    {
        "app/services/providers/__init__.py",
        "tests/test_provider_output_class_proof.py",
    }
)
CHECKPOINT_TWO_PATHS = CONTAINMENT_PATHS | POLICY_PATHS

PROVIDER_ROUTING_FEATURE_BLOB_OIDS = (
    ("app/services/provider_orchestration.py", "eba30c5b2fcf371463261f3a86d5db80a98ffa77"),
    ("app/services/provider_output_acceptance.py", "b8130b5232655908d48cdd52fb3bde2bee29750d"),
    ("app/services/provider_output_class_proof.py", "f5fa43e264736fe942170c8127be54b828a17479"),
    ("tests/test_provider_deterministic_weighted_routing.py", "e28cbf145f74e9dbb54d72ff88d4a1baeac18109"),
    ("tests/test_provider_multi_backend_routing_contract.py", "a35d7292cca38da2a58217d745fce8248e065985"),
    ("tests/test_provider_output_acceptance_contract.py", "76738fc5f7b9c9390e78aefec864dd547b27bf2e"),
)
PROVIDER_ROUTING_FEATURE_PATH_STATUSES = (
    ("app/services/provider_orchestration.py", "M"),
    ("app/services/provider_output_acceptance.py", "A"),
    ("app/services/provider_output_class_proof.py", "M"),
    ("tests/test_provider_deterministic_weighted_routing.py", "A"),
    ("tests/test_provider_multi_backend_routing_contract.py", "A"),
    ("tests/test_provider_output_acceptance_contract.py", "A"),
)
PROVIDER_ROUTING_FEATURE_PATHS = frozenset(path for path, _oid in PROVIDER_ROUTING_FEATURE_BLOB_OIDS)
GENERATION3_CHECKPOINT_PATH_STATUSES = tuple(
    sorted((*PROVIDER_ROUTING_FEATURE_PATH_STATUSES, *((path, "M") for path in POLICY_PATHS)))
)
GENERATION3_CHECKPOINT_PATHS = PROVIDER_ROUTING_FEATURE_PATHS | POLICY_PATHS

CONTAINMENT_BLOB_OIDS = {
    "app/services/providers/__init__.py": "f2af2c1e4916502fe536558a36fee00469d7f688",
    "tests/test_provider_output_class_proof.py": "0778c666ac70d71583b51abc23836525855df251",
}

TRUSTED_BLOB_OIDS = {
    "web/src/components/ProvenanceDetailPanel.tsx": "46163cb7d5623a6aee4b40de1f1f038ae11cc9fe",
    "web/src/screens/__tests__/EvidenceProvenanceScreen.test.tsx": "636d2bd38862bddc72270add3a9a5bbfd4bdc962",
    "scripts/test_evidence_provenance_real_backend.py": "5afa8dd8f659ef263995345b5ae1969bf6b1e85f",
    "tests/helpers/evidence_provenance_fixture.py": "d147f55506ea290e85a825f547e595b6ce45e665",
    "web/playwright.real-backend.config.ts": "8c012ce65f1c148a45a5520624384fa0104024f3",
    "web/tests/real-backend/evidence-provenance.real-backend.spec.ts": (
        "73b9b91e6b4dda361f5884fab9cffbcf731f4124"
    ),
}
TRUSTED_BLOB_SHA256 = {
    "scripts/test_evidence_provenance_real_backend.py": (
        "97E3A87A48BBC0BB789D4DB0D0E7F597392E9FD004B2E0F87DCF5D3A165E40AA"
    ),
    "tests/helpers/evidence_provenance_fixture.py": (
        "F514718C3330E40E623D4BA6CD181C72CD18714B7A1A7DCB7FE515EE4A250FDD"
    ),
    "web/playwright.real-backend.config.ts": (
        "1BA04552C9189331D841B21EB242F87DF16FA69E5651A1FF4C6C54B2181E7CCC"
    ),
    "web/tests/real-backend/evidence-provenance.real-backend.spec.ts": (
        "A7FEC6190566F97D6D5DF1E1BC0160E2C149162EE7492D46B0B96330C1FAC7EF"
    ),
}

GENERATION3_PROOF_BLOB_OIDS = tuple(
    (path, oid)
    for path, oid in TRUSTED_BLOB_OIDS.items()
    if path != "scripts/test_evidence_provenance_real_backend.py"
)

_OID = re.compile(r"^[0-9a-f]{40}$")
_PROTECTED_PREFIXES = (
    "app/",
    "scripts/",
    "tests/",
    "web/src/",
    "web/tests/",
    ".github/",
)
_PROTECTED_EXACT = frozenset(
    {
        "STATUS.md",
        "pyproject.toml",
        "uv.lock",
        "web/package.json",
        "web/package-lock.json",
        "web/vite.config.ts",
        "web/vite.config.js",
        "web/vite.config.d.ts",
        "web/playwright.config.ts",
        "web/playwright.smoke.config.ts",
        "web/playwright.real-backend.config.ts",
    }
)


class PolicyContext(str, Enum):
    COMMITTED_FEATURE_HEAD = "committed_feature_head"
    RC_INTEGRATION_MERGE_HEAD = "rc_integration_merge_head"
    RC_STATUS_DESCENDANT = "rc_status_descendant"
    RC_POLICY_CHECKPOINT_HEAD = "rc_policy_checkpoint_head"
    RC_POLICY_STATUS_DESCENDANT = "rc_policy_status_descendant"


class ExecutionMode(str, Enum):
    HISTORICAL_PROVENANCE_ONLY = "historical_provenance_only"
    FULL_HARNESS_ALLOWED = "full_harness_allowed"


@dataclass(frozen=True)
class CommitRecord:
    oid: str
    parents: tuple[str, ...]
    subject: str
    changed_paths: frozenset[str]
    tree_oid: str
    changed_path_statuses: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BlobRecord:
    path: str
    oid: str
    sha256: str | None = None


@dataclass(frozen=True)
class OwnerArtifactRecord:
    path: str
    kind: str
    size: int
    mtime_ns: int
    sha256: str | None


@dataclass(frozen=True)
class CheckoutRecord:
    branch: str
    head: str
    head_commit: CommitRecord
    first_parent_commits: tuple[CommitRecord, ...]
    related_commits: tuple[CommitRecord, ...]
    blobs: tuple[BlobRecord, ...]
    checkpoint_blobs: tuple[BlobRecord, ...]
    verified_ancestors: frozenset[str]
    tracked_clean: bool
    index_empty: bool
    protected_untracked_paths: tuple[str, ...] = ()
    containment_blobs: tuple[BlobRecord, ...] = ()
    provider_routing_blobs: tuple[BlobRecord, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    context: PolicyContext | None
    execution_mode: ExecutionMode | None
    reason: str | None
    status_commit_count: int = 0
    checkpoint_oid: str | None = None
    previous_checkpoint_oid: str | None = None
    checkpoint_generation: int | None = None
    transition_id: str | None = None
    proof_critical_blobs_verified: bool = False


def _reject(reason: str) -> PolicyDecision:
    return PolicyDecision(
        accepted=False,
        context=None,
        execution_mode=None,
        reason=reason,
    )


def _accept(
    context: PolicyContext,
    execution_mode: ExecutionMode,
    *,
    status_commit_count: int = 0,
    checkpoint_oid: str | None = None,
    previous_checkpoint_oid: str | None = None,
    checkpoint_generation: int | None = None,
    transition_id: str | None = None,
    proof_critical_blobs_verified: bool = False,
) -> PolicyDecision:
    return PolicyDecision(
        accepted=True,
        context=context,
        execution_mode=execution_mode,
        reason=None,
        status_commit_count=status_commit_count,
        checkpoint_oid=checkpoint_oid,
        previous_checkpoint_oid=previous_checkpoint_oid,
        checkpoint_generation=checkpoint_generation,
        transition_id=transition_id,
        proof_critical_blobs_verified=proof_critical_blobs_verified,
    )


def _normalize_path(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or ":" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        return None
    parts = PurePosixPath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def is_protected_untracked_path(path: str) -> bool:
    normalized = _normalize_path(path)
    if normalized is None:
        return True
    return normalized in _PROTECTED_EXACT or normalized.startswith(_PROTECTED_PREFIXES)


def compare_owner_inventories(
    before: Iterable[OwnerArtifactRecord],
    after: Iterable[OwnerArtifactRecord],
) -> tuple[bool, tuple[str, ...]]:
    before_map = {item.path: item for item in before}
    after_map = {item.path: item for item in after}
    changed = tuple(
        sorted(
            path
            for path in before_map.keys() | after_map.keys()
            if before_map.get(path) != after_map.get(path)
        )
    )
    return not changed, changed


def _valid_commit(record: CommitRecord) -> bool:
    statuses = record.changed_path_statuses
    status_paths = tuple(path for path, _status in statuses)
    return (
        bool(_OID.fullmatch(record.oid))
        and bool(_OID.fullmatch(record.tree_oid))
        and all(_OID.fullmatch(parent) for parent in record.parents)
        and all(_normalize_path(path) is not None for path in record.changed_paths)
        and all(
            _normalize_path(path) is not None and bool(re.fullmatch(r"[ACDMRTUXB][0-9]*", status))
            for path, status in statuses
        )
        and (not statuses or (len(status_paths) == len(set(status_paths)) and frozenset(status_paths) == record.changed_paths))
    )


def _valid_blob(record: BlobRecord) -> bool:
    return (
        _normalize_path(record.path) is not None
        and bool(_OID.fullmatch(record.oid))
        and (record.sha256 is None or bool(re.fullmatch(r"[0-9A-Fa-f]{64}", record.sha256)))
    )


def _records(state: CheckoutRecord) -> tuple[dict[str, CommitRecord] | None, str | None]:
    records: dict[str, CommitRecord] = {}
    for record in (state.head_commit, *state.first_parent_commits, *state.related_commits):
        if not _valid_commit(record):
            return None, "POLICY_GIT_COMMAND_FAILED"
        previous = records.get(record.oid)
        if previous is not None and previous != record:
            return None, "POLICY_CONTEXT_AMBIGUOUS"
        records[record.oid] = record
    if not _OID.fullmatch(state.head) or state.head != state.head_commit.oid:
        return None, "POLICY_GIT_COMMAND_FAILED"
    return records, None


def _blob_map(records: tuple[BlobRecord, ...]) -> tuple[dict[str, BlobRecord] | None, str | None]:
    result: dict[str, BlobRecord] = {}
    for record in records:
        if not _valid_blob(record):
            return None, "POLICY_GIT_COMMAND_FAILED"
        previous = result.get(record.path)
        if previous is not None and previous != record:
            return None, "POLICY_CONTEXT_AMBIGUOUS"
        result[record.path] = record
    return result, None


def _verify_ancestors(state: CheckoutRecord, required: frozenset[str]) -> str | None:
    return None if required <= state.verified_ancestors else "POLICY_REQUIRED_ANCESTOR_MISSING"


def _verify_safe_feature_chain(records: dict[str, CommitRecord]) -> str | None:
    safe = records.get(SAFE_LINK_OID)
    feature = records.get(FEATURE_OID)
    if safe is None or feature is None:
        return "POLICY_REQUIRED_ANCESTOR_MISSING"
    if safe.parents != (BASE_OID,) or feature.parents != (SAFE_LINK_OID,):
        return "POLICY_PARENT_MISMATCH"
    if safe.subject != SAFE_LINK_SUBJECT or feature.subject != FEATURE_SUBJECT:
        return "POLICY_SUBJECT_MISMATCH"
    if safe.changed_paths != SAFE_LINK_PATHS or feature.changed_paths != FEATURE_PATHS:
        return "POLICY_PATH_CLOSURE_VIOLATION"
    return None


def _verify_integration(record: CommitRecord) -> str | None:
    if record.parents != (BASE_OID, FEATURE_OID):
        return "POLICY_PARENT_MISMATCH"
    if record.subject != INTEGRATION_SUBJECT:
        return "POLICY_SUBJECT_MISMATCH"
    if record.changed_paths != INTEGRATION_PATHS:
        return "POLICY_PATH_CLOSURE_VIOLATION"
    return None


def _verify_historical_blobs(state: CheckoutRecord) -> str | None:
    blobs, error = _blob_map(state.blobs)
    if error is not None:
        return error
    assert blobs is not None
    for path, oid in TRUSTED_BLOB_OIDS.items():
        record = blobs.get(path)
        if record is None or record.oid != oid:
            return "POLICY_PROOF_BLOB_MISMATCH"
        expected_sha256 = TRUSTED_BLOB_SHA256.get(path)
        if expected_sha256 is not None and (record.sha256 or "").upper() != expected_sha256:
            return "POLICY_PROOF_BLOB_MISMATCH"
    return None


def _verify_checkpoint_blobs(state: CheckoutRecord) -> str | None:
    current, error = _blob_map(state.blobs)
    if error is not None:
        return error
    checkpoint, error = _blob_map(state.checkpoint_blobs)
    if error is not None:
        return error
    assert current is not None and checkpoint is not None
    for path, oid in TRUSTED_BLOB_OIDS.items():
        if path == "scripts/test_evidence_provenance_real_backend.py":
            continue
        record = current.get(path)
        if record is None or record.oid != oid:
            return "POLICY_PROOF_BLOB_MISMATCH"
        expected_sha256 = TRUSTED_BLOB_SHA256.get(path)
        if expected_sha256 is not None and (record.sha256 or "").upper() != expected_sha256:
            return "POLICY_PROOF_BLOB_MISMATCH"
    for path in POLICY_PATHS:
        if path not in current or path not in checkpoint or current[path] != checkpoint[path]:
            return "POLICY_PROOF_BLOB_MISMATCH"
    return None


def _verify_status_chain(
    commits: tuple[CommitRecord, ...],
    *,
    parent: str,
) -> str | None:
    previous = parent
    for commit in commits:
        if len(commit.parents) != 1:
            return "POLICY_COMMIT_SHAPE_MISMATCH"
        if commit.parents != (previous,):
            return "POLICY_PARENT_MISMATCH"
        if commit.changed_paths != STATUS_PATHS:
            return "POLICY_PATH_CLOSURE_VIOLATION"
        previous = commit.oid
    return None


def _evaluate_feature(state: CheckoutRecord, records: dict[str, CommitRecord]) -> PolicyDecision:
    if state.head != FEATURE_OID:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    error = _verify_ancestors(state, frozenset({BASE_OID, SAFE_LINK_OID}))
    error = error or _verify_safe_feature_chain(records)
    error = error or _verify_historical_blobs(state)
    if error is not None:
        return _reject(error)
    if state.head_commit != records.get(FEATURE_OID):
        return _reject("POLICY_CONTEXT_AMBIGUOUS")
    return _accept(
        PolicyContext.COMMITTED_FEATURE_HEAD,
        ExecutionMode.HISTORICAL_PROVENANCE_ONLY,
        proof_critical_blobs_verified=True,
    )


def _evaluate_merge(state: CheckoutRecord, records: dict[str, CommitRecord]) -> PolicyDecision:
    error = _verify_ancestors(state, frozenset({BASE_OID, SAFE_LINK_OID, FEATURE_OID}))
    error = error or _verify_safe_feature_chain(records)
    error = error or _verify_integration(state.head_commit)
    error = error or _verify_historical_blobs(state)
    if error is not None:
        return _reject(error)
    return _accept(
        PolicyContext.RC_INTEGRATION_MERGE_HEAD,
        ExecutionMode.HISTORICAL_PROVENANCE_ONLY,
        proof_critical_blobs_verified=True,
    )


def _evaluate_historical_status(state: CheckoutRecord, records: dict[str, CommitRecord]) -> PolicyDecision:
    integration = records.get(INTEGRATION_OID)
    if integration is None:
        return _reject("POLICY_REQUIRED_ANCESTOR_MISSING")
    error = _verify_ancestors(state, frozenset({BASE_OID, SAFE_LINK_OID, FEATURE_OID, INTEGRATION_OID}))
    error = error or _verify_safe_feature_chain(records)
    error = error or _verify_integration(integration)
    error = error or _verify_status_chain(state.first_parent_commits, parent=INTEGRATION_OID)
    error = error or _verify_historical_blobs(state)
    if error is not None:
        return _reject(error)
    if not state.first_parent_commits or state.first_parent_commits[-1].oid != state.head:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    return _accept(
        PolicyContext.RC_STATUS_DESCENDANT,
        ExecutionMode.HISTORICAL_PROVENANCE_ONLY,
        status_commit_count=len(state.first_parent_commits),
        proof_critical_blobs_verified=True,
    )


def _evaluate_policy_context(state: CheckoutRecord, records: dict[str, CommitRecord]) -> PolicyDecision:
    chain = state.first_parent_commits
    if len(chain) < 2 or chain[0].oid != POLICY_BASE_OID:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    policy_merge = chain[1]
    if policy_merge.oid == POLICY_BASE_OID:
        return _reject("POLICY_CONTEXT_AMBIGUOUS")
    if len(policy_merge.parents) != 2:
        return _reject("POLICY_COMMIT_SHAPE_MISMATCH")
    policy_feature = records.get(policy_merge.parents[1])
    if policy_feature is None:
        return _reject("POLICY_REQUIRED_ANCESTOR_MISSING")
    error = _verify_ancestors(
        state,
        frozenset({BASE_OID, SAFE_LINK_OID, FEATURE_OID, INTEGRATION_OID, POLICY_BASE_OID}),
    )
    error = error or _verify_safe_feature_chain(records)
    integration = records.get(INTEGRATION_OID)
    if integration is None:
        error = error or "POLICY_REQUIRED_ANCESTOR_MISSING"
    else:
        error = error or _verify_integration(integration)
    error = error or _verify_status_chain((chain[0],), parent=INTEGRATION_OID)
    if policy_feature.parents != (POLICY_BASE_OID,) or policy_merge.parents != (
        POLICY_BASE_OID,
        policy_feature.oid,
    ):
        error = error or "POLICY_PARENT_MISMATCH"
    if policy_feature.subject != POLICY_FEATURE_SUBJECT or policy_merge.subject != POLICY_MERGE_SUBJECT:
        error = error or "POLICY_SUBJECT_MISMATCH"
    if policy_feature.changed_paths != POLICY_PATHS or policy_merge.changed_paths != POLICY_PATHS:
        error = error or "POLICY_PATH_CLOSURE_VIOLATION"
    if policy_feature.tree_oid != policy_merge.tree_oid:
        error = error or "POLICY_COMMIT_SHAPE_MISMATCH"
    later = chain[2:]
    error = error or _verify_status_chain(later, parent=policy_merge.oid)
    error = error or _verify_checkpoint_blobs(state)
    if error is not None:
        return _reject(error)
    if chain[-1].oid != state.head:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    if later:
        return _accept(
            PolicyContext.RC_POLICY_STATUS_DESCENDANT,
            ExecutionMode.FULL_HARNESS_ALLOWED,
            status_commit_count=len(later),
            checkpoint_oid=policy_merge.oid,
            proof_critical_blobs_verified=True,
        )
    if state.head != policy_merge.oid:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    return _accept(
        PolicyContext.RC_POLICY_CHECKPOINT_HEAD,
        ExecutionMode.FULL_HARNESS_ALLOWED,
        checkpoint_oid=policy_merge.oid,
        proof_critical_blobs_verified=True,
    )


def _modified_statuses(paths: frozenset[str]) -> tuple[tuple[str, str], ...]:
    return tuple((path, "M") for path in sorted(paths))


def _verify_exact_modified_paths(record: CommitRecord, paths: frozenset[str]) -> str | None:
    if record.changed_paths != paths:
        return "POLICY_PATH_CLOSURE_VIOLATION"
    if record.changed_path_statuses != _modified_statuses(paths):
        return "POLICY_COMMIT_SHAPE_MISMATCH"
    return None


def _verify_exact_path_statuses(
    record: CommitRecord,
    expected: tuple[tuple[str, str], ...],
) -> str | None:
    if record.changed_paths != frozenset(path for path, _status in expected):
        return "POLICY_PATH_CLOSURE_VIOLATION"
    if record.changed_path_statuses != expected:
        return "POLICY_COMMIT_SHAPE_MISMATCH"
    return None


def _verify_post_p_prefix(
    state: CheckoutRecord,
    records: dict[str, CommitRecord],
) -> tuple[int | None, str | None]:
    chain = state.first_parent_commits
    positions = tuple(index for index, commit in enumerate(chain) if commit.oid == CURRENT_STATUS_OID)
    if not positions:
        return None, "POLICY_CONTEXT_UNRECOGNIZED"
    if len(positions) != 1:
        return None, "POLICY_CONTEXT_AMBIGUOUS"
    position = positions[0]
    if position != 2 or tuple(commit.oid for commit in chain[:3]) != (
        POLICY_BASE_OID,
        PREVIOUS_CHECKPOINT_OID,
        CURRENT_STATUS_OID,
    ):
        return None, "POLICY_CONTEXT_UNRECOGNIZED"

    policy_base, previous_checkpoint, current_status = chain[:3]
    previous_feature = records.get(PREVIOUS_POLICY_FEATURE_OID)
    if previous_feature is None:
        return None, "POLICY_REQUIRED_ANCESTOR_MISSING"
    error = _verify_ancestors(
        state,
        frozenset(
            {
                BASE_OID,
                SAFE_LINK_OID,
                FEATURE_OID,
                INTEGRATION_OID,
                POLICY_BASE_OID,
                PREVIOUS_CHECKPOINT_OID,
                CURRENT_STATUS_OID,
            }
        ),
    )
    error = error or _verify_safe_feature_chain(records)
    integration = records.get(INTEGRATION_OID)
    if integration is None:
        error = error or "POLICY_REQUIRED_ANCESTOR_MISSING"
    else:
        error = error or _verify_integration(integration)
    error = error or _verify_status_chain((policy_base,), parent=INTEGRATION_OID)
    if previous_feature.parents != (POLICY_BASE_OID,) or previous_checkpoint.parents != (
        POLICY_BASE_OID,
        PREVIOUS_POLICY_FEATURE_OID,
    ):
        error = error or "POLICY_PARENT_MISMATCH"
    if (
        previous_feature.subject != POLICY_FEATURE_SUBJECT
        or previous_checkpoint.subject != POLICY_MERGE_SUBJECT
    ):
        error = error or "POLICY_SUBJECT_MISMATCH"
    if previous_feature.changed_paths != POLICY_PATHS or previous_checkpoint.changed_paths != POLICY_PATHS:
        error = error or "POLICY_PATH_CLOSURE_VIOLATION"
    if previous_feature.tree_oid != previous_checkpoint.tree_oid:
        error = error or "POLICY_COMMIT_SHAPE_MISMATCH"
    if current_status.parents != (PREVIOUS_CHECKPOINT_OID,):
        error = error or "POLICY_PARENT_MISMATCH"
    if current_status.subject != CURRENT_STATUS_SUBJECT:
        error = error or "POLICY_SUBJECT_MISMATCH"
    if current_status.changed_paths != STATUS_PATHS:
        error = error or "POLICY_PATH_CLOSURE_VIOLATION"
    if current_status.tree_oid != CURRENT_STATUS_TREE:
        error = error or "POLICY_COMMIT_SHAPE_MISMATCH"
    return (position, error)


def _verify_containment_anchor(
    state: CheckoutRecord,
    records: dict[str, CommitRecord],
) -> str | None:
    containment = records.get(CONTAINMENT_OID)
    if containment is None:
        return "POLICY_REQUIRED_ANCESTOR_MISSING"
    if containment.parents != (CURRENT_STATUS_OID,):
        return "POLICY_PARENT_MISMATCH"
    if containment.subject != CONTAINMENT_SUBJECT:
        return "POLICY_SUBJECT_MISMATCH"
    if containment.tree_oid != CONTAINMENT_TREE:
        return "POLICY_COMMIT_SHAPE_MISMATCH"
    error = _verify_exact_modified_paths(containment, CONTAINMENT_PATHS)
    if error is not None:
        return error
    blobs, error = _blob_map(state.containment_blobs)
    if error is not None:
        return error
    assert blobs is not None
    if frozenset(blobs) != CONTAINMENT_PATHS:
        return "POLICY_PROOF_BLOB_MISMATCH"
    for path, expected_oid in CONTAINMENT_BLOB_OIDS.items():
        record = blobs.get(path)
        if record is None or record.oid != expected_oid:
            return "POLICY_PROOF_BLOB_MISMATCH"
    return None


def _verify_transition_commit(record: CommitRecord) -> str | None:
    if record.parents != (CONTAINMENT_OID,):
        return "POLICY_PARENT_MISMATCH"
    if record.subject != TRANSITION_SUBJECT:
        return "POLICY_SUBJECT_MISMATCH"
    return _verify_exact_modified_paths(record, POLICY_PATHS)


def _verify_v2_status_chain(
    commits: tuple[CommitRecord, ...],
    *,
    checkpoint_oid: str,
) -> str | None:
    previous = checkpoint_oid
    for index, commit in enumerate(commits):
        if len(commit.parents) != 1:
            return "POLICY_COMMIT_SHAPE_MISMATCH"
        if commit.parents != (previous,):
            return "POLICY_PARENT_MISMATCH"
        if commit.changed_paths != STATUS_PATHS:
            return "POLICY_PATH_CLOSURE_VIOLATION"
        if not commit.subject:
            return "POLICY_SUBJECT_MISMATCH"
        if index == 0 and commit.subject != CHECKPOINT_STATUS_SUBJECT:
            return "POLICY_SUBJECT_MISMATCH"
        previous = commit.oid
    return None


def _evaluate_v2_transition(
    state: CheckoutRecord,
    records: dict[str, CommitRecord],
) -> PolicyDecision:
    position, error = _verify_post_p_prefix(state, records)
    if error is not None:
        return _reject(error)
    assert position is not None
    suffix = state.first_parent_commits[position + 1 :]
    if not suffix:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    if state.head == CONTAINMENT_OID:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    if (
        len(suffix) == 2
        and suffix[0].oid == CONTAINMENT_OID
        and suffix[1].oid == state.head
        and _verify_transition_commit(suffix[1]) is None
    ):
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")

    checkpoint = suffix[0]
    if len(checkpoint.parents) != 2:
        return _reject("POLICY_COMMIT_SHAPE_MISMATCH")
    if checkpoint.parents[0] != CURRENT_STATUS_OID:
        return _reject("POLICY_PARENT_MISMATCH")
    transition = records.get(checkpoint.parents[1])
    if transition is None:
        return _reject("POLICY_REQUIRED_ANCESTOR_MISSING")
    error = _verify_transition_commit(transition)
    error = error or _verify_ancestors(state, frozenset({CURRENT_STATUS_OID, CONTAINMENT_OID}))
    error = error or _verify_containment_anchor(state, records)
    if checkpoint.subject != CHECKPOINT_TWO_SUBJECT:
        error = error or "POLICY_SUBJECT_MISMATCH"
    error = error or _verify_exact_modified_paths(checkpoint, CHECKPOINT_TWO_PATHS)
    if checkpoint.tree_oid != transition.tree_oid:
        error = error or "POLICY_COMMIT_SHAPE_MISMATCH"
    error = error or _verify_checkpoint_blobs(state)
    later = suffix[1:]
    error = error or _verify_v2_status_chain(later, checkpoint_oid=checkpoint.oid)
    if error is not None:
        return _reject(error)
    if state.first_parent_commits[-1].oid != state.head:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    if later:
        return _accept(
            PolicyContext.RC_POLICY_STATUS_DESCENDANT,
            ExecutionMode.FULL_HARNESS_ALLOWED,
            status_commit_count=len(later),
            checkpoint_oid=checkpoint.oid,
            previous_checkpoint_oid=PREVIOUS_CHECKPOINT_OID,
            checkpoint_generation=CHECKPOINT_GENERATION,
            transition_id=TRANSITION_ID,
            proof_critical_blobs_verified=True,
        )
    if state.head != checkpoint.oid:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    return _accept(
        PolicyContext.RC_POLICY_CHECKPOINT_HEAD,
        ExecutionMode.FULL_HARNESS_ALLOWED,
        checkpoint_oid=checkpoint.oid,
        previous_checkpoint_oid=PREVIOUS_CHECKPOINT_OID,
        checkpoint_generation=CHECKPOINT_GENERATION,
        transition_id=TRANSITION_ID,
        proof_critical_blobs_verified=True,
    )


def _verify_generation3_prefix(
    state: CheckoutRecord,
    records: dict[str, CommitRecord],
) -> tuple[tuple[CommitRecord, ...] | None, str | None]:
    position, error = _verify_post_p_prefix(state, records)
    if error is not None:
        return None, error
    assert position is not None
    suffix = state.first_parent_commits[position + 1 :]
    if len(suffix) < 2:
        return None, "POLICY_CONTEXT_UNRECOGNIZED"

    checkpoint, status_base = suffix[:2]
    if checkpoint.oid != GENERATION3_PREVIOUS_CHECKPOINT_OID:
        return None, "POLICY_CONTEXT_UNRECOGNIZED"
    transition = records.get(GENERATION3_TRANSITION_FEATURE_OID)
    if transition is None:
        return None, "POLICY_REQUIRED_ANCESTOR_MISSING"
    error = _verify_transition_commit(transition)
    if checkpoint.parents != (CURRENT_STATUS_OID, GENERATION3_TRANSITION_FEATURE_OID):
        error = error or "POLICY_PARENT_MISMATCH"
    if checkpoint.subject != CHECKPOINT_TWO_SUBJECT:
        error = error or "POLICY_SUBJECT_MISMATCH"
    if checkpoint.tree_oid != GENERATION3_PREVIOUS_CHECKPOINT_TREE:
        error = error or "POLICY_COMMIT_SHAPE_MISMATCH"
    error = error or _verify_exact_modified_paths(checkpoint, CHECKPOINT_TWO_PATHS)
    if transition.tree_oid != checkpoint.tree_oid:
        error = error or "POLICY_COMMIT_SHAPE_MISMATCH"
    error = error or _verify_containment_anchor(state, records)
    if error is not None:
        return None, error

    if status_base.oid != GENERATION3_STATUS_BASE_OID:
        return None, "POLICY_CONTEXT_UNRECOGNIZED"
    if status_base.parents != (GENERATION3_PREVIOUS_CHECKPOINT_OID,):
        return None, "POLICY_PARENT_MISMATCH"
    if status_base.subject != GENERATION3_STATUS_BASE_SUBJECT:
        return None, "POLICY_SUBJECT_MISMATCH"
    if status_base.tree_oid != GENERATION3_STATUS_BASE_TREE:
        return None, "POLICY_COMMIT_SHAPE_MISMATCH"
    error = _verify_exact_modified_paths(status_base, STATUS_PATHS)
    return (suffix, error)


def _verify_provider_routing_feature(
    state: CheckoutRecord,
    records: dict[str, CommitRecord],
) -> str | None:
    feature = records.get(PROVIDER_ROUTING_FEATURE_OID)
    if feature is None:
        return "POLICY_REQUIRED_ANCESTOR_MISSING"
    if feature.parents != (GENERATION3_STATUS_BASE_OID,):
        return "POLICY_PARENT_MISMATCH"
    if feature.subject != PROVIDER_ROUTING_FEATURE_SUBJECT:
        return "POLICY_SUBJECT_MISMATCH"
    if feature.tree_oid != PROVIDER_ROUTING_FEATURE_TREE:
        return "POLICY_COMMIT_SHAPE_MISMATCH"
    error = _verify_exact_path_statuses(feature, PROVIDER_ROUTING_FEATURE_PATH_STATUSES)
    if error is not None:
        return error
    blobs, error = _blob_map(state.provider_routing_blobs)
    if error is not None:
        return error
    assert blobs is not None
    if frozenset(blobs) != PROVIDER_ROUTING_FEATURE_PATHS:
        return "POLICY_PROOF_BLOB_MISMATCH"
    for path, expected_oid in PROVIDER_ROUTING_FEATURE_BLOB_OIDS:
        record = blobs.get(path)
        if record is None or record.oid != expected_oid:
            return "POLICY_PROOF_BLOB_MISMATCH"
    return None


def _verify_generation3_blobs(state: CheckoutRecord) -> str | None:
    current, error = _blob_map(state.blobs)
    if error is not None:
        return error
    checkpoint, error = _blob_map(state.checkpoint_blobs)
    if error is not None:
        return error
    assert current is not None and checkpoint is not None
    if frozenset(checkpoint) != POLICY_PATHS:
        return "POLICY_PROOF_BLOB_MISMATCH"
    for path, expected_oid in GENERATION3_PROOF_BLOB_OIDS:
        record = current.get(path)
        if record is None or record.oid != expected_oid:
            return "POLICY_PROOF_BLOB_MISMATCH"
        expected_sha256 = TRUSTED_BLOB_SHA256.get(path)
        if expected_sha256 is not None and (record.sha256 or "").upper() != expected_sha256:
            return "POLICY_PROOF_BLOB_MISMATCH"
    for path in POLICY_PATHS:
        if path not in current or current[path] != checkpoint.get(path):
            return "POLICY_PROOF_BLOB_MISMATCH"
    return None


def _verify_generation3_status_chain(
    commits: tuple[CommitRecord, ...],
    *,
    checkpoint_oid: str,
) -> str | None:
    previous = checkpoint_oid
    for index, commit in enumerate(commits):
        if len(commit.parents) != 1:
            return "POLICY_COMMIT_SHAPE_MISMATCH"
        if commit.parents != (previous,):
            return "POLICY_PARENT_MISMATCH"
        if commit.changed_paths != STATUS_PATHS:
            return "POLICY_PATH_CLOSURE_VIOLATION"
        if commit.changed_path_statuses != _modified_statuses(STATUS_PATHS):
            return "POLICY_COMMIT_SHAPE_MISMATCH"
        if not commit.subject or (index == 0 and commit.subject != GENERATION3_STATUS_SUBJECT):
            return "POLICY_SUBJECT_MISMATCH"
        previous = commit.oid
    return None


def _evaluate_v3_transition(
    state: CheckoutRecord,
    records: dict[str, CommitRecord],
) -> PolicyDecision:
    suffix, error = _verify_generation3_prefix(state, records)
    if error is not None:
        return _reject(error)
    assert suffix is not None
    if len(suffix) < 3:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    checkpoint = suffix[2]
    if len(checkpoint.parents) != 2:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    if checkpoint.parents[0] != GENERATION3_STATUS_BASE_OID:
        return _reject("POLICY_PARENT_MISMATCH")
    policy_feature = records.get(checkpoint.parents[1])
    if policy_feature is None:
        return _reject("POLICY_REQUIRED_ANCESTOR_MISSING")
    if policy_feature.parents != (PROVIDER_ROUTING_FEATURE_OID,):
        error = "POLICY_PARENT_MISMATCH"
    elif policy_feature.subject != GENERATION3_POLICY_FEATURE_SUBJECT:
        error = "POLICY_SUBJECT_MISMATCH"
    else:
        error = _verify_exact_modified_paths(policy_feature, POLICY_PATHS)
    error = error or _verify_provider_routing_feature(state, records)
    if checkpoint.subject != GENERATION3_CHECKPOINT_SUBJECT:
        error = error or "POLICY_SUBJECT_MISMATCH"
    error = error or _verify_exact_path_statuses(checkpoint, GENERATION3_CHECKPOINT_PATH_STATUSES)
    if checkpoint.tree_oid != policy_feature.tree_oid:
        error = error or "POLICY_COMMIT_SHAPE_MISMATCH"
    required_ancestors = frozenset(
        {
            GENERATION3_PREVIOUS_CHECKPOINT_OID,
            GENERATION3_STATUS_BASE_OID,
            PROVIDER_ROUTING_FEATURE_OID,
            policy_feature.oid,
        }
    )
    error = error or _verify_ancestors(state, required_ancestors)
    error = error or _verify_generation3_blobs(state)
    later = suffix[3:]
    error = error or _verify_generation3_status_chain(later, checkpoint_oid=checkpoint.oid)
    if error is not None:
        return _reject(error)
    if state.first_parent_commits[-1].oid != state.head:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    if later:
        return _accept(
            PolicyContext.RC_POLICY_STATUS_DESCENDANT,
            ExecutionMode.FULL_HARNESS_ALLOWED,
            status_commit_count=len(later),
            checkpoint_oid=checkpoint.oid,
            previous_checkpoint_oid=GENERATION3_PREVIOUS_CHECKPOINT_OID,
            checkpoint_generation=GENERATION3_CHECKPOINT_GENERATION,
            transition_id=GENERATION3_TRANSITION_ID,
            proof_critical_blobs_verified=True,
        )
    if state.head != checkpoint.oid:
        return _reject("POLICY_CONTEXT_UNRECOGNIZED")
    return _accept(
        PolicyContext.RC_POLICY_CHECKPOINT_HEAD,
        ExecutionMode.FULL_HARNESS_ALLOWED,
        checkpoint_oid=checkpoint.oid,
        previous_checkpoint_oid=GENERATION3_PREVIOUS_CHECKPOINT_OID,
        checkpoint_generation=GENERATION3_CHECKPOINT_GENERATION,
        transition_id=GENERATION3_TRANSITION_ID,
        proof_critical_blobs_verified=True,
    )


def evaluate_checkout(state: CheckoutRecord) -> PolicyDecision:
    records, error = _records(state)
    if error is not None:
        return _reject(error)
    blobs, error = _blob_map(state.blobs)
    if error is not None:
        return _reject(error)
    checkpoint_blobs, error = _blob_map(state.checkpoint_blobs)
    if error is not None:
        return _reject(error)
    containment_blobs, error = _blob_map(state.containment_blobs)
    if error is not None:
        return _reject(error)
    provider_routing_blobs, error = _blob_map(state.provider_routing_blobs)
    if error is not None:
        return _reject(error)
    assert (
        records is not None
        and blobs is not None
        and checkpoint_blobs is not None
        and containment_blobs is not None
        and provider_routing_blobs is not None
    )

    if not state.tracked_clean:
        return _reject("POLICY_TRACKED_DIRTY")
    if not state.index_empty:
        return _reject("POLICY_INDEX_NOT_EMPTY")
    if state.protected_untracked_paths:
        return _reject("POLICY_UNTRACKED_PROTECTED_OVERLAP")
    if state.branch not in {SOURCE_BRANCH, RC_BRANCH}:
        return _reject("POLICY_BRANCH_MISMATCH")

    if state.branch == SOURCE_BRANCH:
        return _evaluate_feature(state, records)
    if state.head == INTEGRATION_OID:
        return _evaluate_merge(state, records)

    chain = state.first_parent_commits
    if state.head != GENERATION3_STATUS_BASE_OID and (
        GENERATION3_STATUS_BASE_OID in state.verified_ancestors
        or any(commit.oid == GENERATION3_STATUS_BASE_OID for commit in chain)
    ):
        return _evaluate_v3_transition(state, records)
    if state.head != CURRENT_STATUS_OID and (
        CURRENT_STATUS_OID in state.verified_ancestors
        or any(commit.oid == CURRENT_STATUS_OID for commit in chain)
    ):
        return _evaluate_v2_transition(state, records)
    if len(chain) >= 2 and chain[0].oid == POLICY_BASE_OID and (
        len(chain[1].parents) == 2
        or chain[1].subject == POLICY_MERGE_SUBJECT
        or bool(chain[1].changed_paths & POLICY_PATHS)
    ):
        return _evaluate_policy_context(state, records)
    return _evaluate_historical_status(state, records)
