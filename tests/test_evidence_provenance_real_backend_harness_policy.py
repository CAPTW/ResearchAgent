from __future__ import annotations

import importlib
from dataclasses import replace

import pytest


BASE = "b11ce3316cf89060d98a3261b92a839ddefb9701"
SAFE = "224c8b48652944b6b444d1a1114ae9e2813c2546"
FEATURE = "d0c9707b5b66207c73f1aa313a30d679b536a314"
INTEGRATION = "fc360525fb113f050447636ce4f27d870a7a84f6"
STATUS = "029307e4c704d55766b1664684700c89310890cf"
POLICY_FEATURE = "f" * 40
POLICY_MERGE = "e" * 40
STATUS_TWO = "a" * 40
POLICY_STATUS = "c" * 40

PREVIOUS_POLICY_FEATURE = "dd6f9f6e90c4bf4de168d1345aab6bbcc1e2b836"
PREVIOUS_CHECKPOINT = "df52d5b9c15e8dc8d166b092ef95189a0a7f4775"
PS1 = "d1ad512bf5e757c69cfec3e58237f467fe87fddb"
PS1_TREE = "44cbc5a6ef18e7c104177b07797baa11ac98e917"
CONTAINMENT = "80bd691d1acad3382445ffb017d05d81f71efd16"
CONTAINMENT_TREE = "e732bc041fcc533c8b657ae180af7cc0370906b7"
TRANSITION = "9" * 40
CHECKPOINT_TWO = "8" * 40
CHECKPOINT_STATUS = "7" * 40
CHECKPOINT_STATUS_TWO = "6" * 40
TRANSITION_TREE = "a" * 40
UNRELATED = "d" * 40

PS1_SUBJECT = "Record reusable real-backend harness policy validation"
CONTAINMENT_SUBJECT = "Block unscoped provider output activation"
TRANSITION_SUBJECT = "Extend harness policy for provider containment checkpoint"
CHECKPOINT_TWO_SUBJECT = "Merge provider output gate containment checkpoint"
CHECKPOINT_STATUS_SUBJECT = "Record provider output gate containment validation"
TRANSITION_ID = "provider_output_gate_containment"

M2 = "ed1cd924c825e446976307ee69a18a049ee47207"
M2_TREE = "b5e8b5a267fb100dc54ec0868647638415258373"
TRANSITION_TWO = "3934330ef2c26ea7be124cc28330c816a2c86f08"
S2 = "6084abe9d61a5c1698f0f1242529423b6b1dd15a"
S2_TREE = "bc20b5d2e62fcc25f6ce7880d87850ade13b8ead"
ROUTING_FEATURE = "a345c5ca04fe35837597cdf76d4ee6bad9d82cac"
ROUTING_FEATURE_TREE = "15a6e299e4bad826e2a3ed87172bcb05f3a06057"
Q3 = "5" * 40
G3 = "4" * 40
S3 = "3" * 40
S3_TWO = "2" * 40
Q3_TREE = "1f" * 20

S2_SUBJECT = "Record provider output gate containment validation"
ROUTING_FEATURE_SUBJECT = "Implement fail-closed provider routing contract"
Q3_SUBJECT = "Prepare generation-3 provider routing checkpoint policy"
G3_SUBJECT = "Merge provider multi-backend routing generation-3 checkpoint"
S3_SUBJECT = "Record provider multi-backend routing checkpoint validation"
V3_TRANSITION_ID = "provider_multi_backend_routing_contract"

SOURCE_BRANCH = "revive/evidence-provenance-real-backend-browser"
RC_BRANCH = "rc/h008d115-l10-current-chain"

SAFE_PATHS = frozenset(
    {
        "web/src/components/ProvenanceDetailPanel.tsx",
        "web/src/screens/__tests__/EvidenceProvenanceScreen.test.tsx",
    }
)
IMPLEMENTATION_PATHS = frozenset(
    {
        "scripts/test_evidence_provenance_real_backend.py",
        "tests/helpers/evidence_provenance_fixture.py",
        "web/playwright.real-backend.config.ts",
        "web/tests/real-backend/evidence-provenance.real-backend.spec.ts",
    }
)
MERGE_PATHS = SAFE_PATHS | IMPLEMENTATION_PATHS
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

ROUTING_FEATURE_BLOBS = {
    "app/services/provider_orchestration.py": "eba30c5b2fcf371463261f3a86d5db80a98ffa77",
    "app/services/provider_output_class_proof.py": "f5fa43e264736fe942170c8127be54b828a17479",
    "app/services/provider_output_acceptance.py": "b8130b5232655908d48cdd52fb3bde2bee29750d",
    "tests/test_provider_multi_backend_routing_contract.py": "a35d7292cca38da2a58217d745fce8248e065985",
    "tests/test_provider_deterministic_weighted_routing.py": "e28cbf145f74e9dbb54d72ff88d4a1baeac18109",
    "tests/test_provider_output_acceptance_contract.py": "76738fc5f7b9c9390e78aefec864dd547b27bf2e",
}
ROUTING_FEATURE_STATUSES = {
    "app/services/provider_orchestration.py": "M",
    "app/services/provider_output_class_proof.py": "M",
    "app/services/provider_output_acceptance.py": "A",
    "tests/test_provider_multi_backend_routing_contract.py": "A",
    "tests/test_provider_deterministic_weighted_routing.py": "A",
    "tests/test_provider_output_acceptance_contract.py": "A",
}
ROUTING_FEATURE_PATHS = frozenset(ROUTING_FEATURE_BLOBS)
G3_PATHS = ROUTING_FEATURE_PATHS | POLICY_PATHS
V3_PROOF_BLOBS = {
    "web/src/components/ProvenanceDetailPanel.tsx": "46163cb7d5623a6aee4b40de1f1f038ae11cc9fe",
    "web/src/screens/__tests__/EvidenceProvenanceScreen.test.tsx": (
        "636d2bd38862bddc72270add3a9a5bbfd4bdc962"
    ),
    "tests/helpers/evidence_provenance_fixture.py": "d147f55506ea290e85a825f547e595b6ce45e665",
    "web/playwright.real-backend.config.ts": "8c012ce65f1c148a45a5520624384fa0104024f3",
    "web/tests/real-backend/evidence-provenance.real-backend.spec.ts": (
        "73b9b91e6b4dda361f5884fab9cffbcf731f4124"
    ),
}
Q3_POLICY_BLOBS = {
    "scripts/evidence_provenance_harness_git_policy.py": "a" * 40,
    "scripts/test_evidence_provenance_real_backend.py": "b" * 40,
    "tests/test_evidence_provenance_real_backend_harness_policy.py": "c" * 40,
}

V3_MUTATION_CASES = (
    "f-parent-not-s2",
    "f-subject-mismatch",
    "f-tree-mismatch",
    "f-path-status-mismatch",
    "f-missing-path",
    "f-seventh-path",
    "f-integrated-blob-mismatch",
    "q-parent-not-f",
    "q-subject-mismatch",
    "q-fourth-path",
    "g3-first-parent-not-s2",
    "g3-second-parent-not-q",
    "g3-parent-order-reversed",
    "g3-subject-mismatch",
    "g3-tree-not-q",
    "g3-missing-feature-path",
    "g3-tenth-path",
    "g3-merge-time-edit",
    "post-g3-source-commit",
    "post-g3-merge",
    "s3-status-plus-source",
    "stale-ambiguous-or-malformed-generation-record",
)

CONTAINMENT_BLOBS = {
    "app/services/providers/__init__.py": "f2af2c1e4916502fe536558a36fee00469d7f688",
    "tests/test_provider_output_class_proof.py": "0778c666ac70d71583b51abc23836525855df251",
}
V2_MUTATION_CASES = (
    ("t-parent-not-pc", "POLICY_PARENT_MISMATCH"),
    ("t-subject-mismatch", "POLICY_SUBJECT_MISMATCH"),
    ("t-fourth-path", "POLICY_PATH_CLOSURE_VIOLATION"),
    ("pc-subject-mismatch", "POLICY_SUBJECT_MISMATCH"),
    ("pc-tree-mismatch", "POLICY_COMMIT_SHAPE_MISMATCH"),
    ("pc-provider-blob-mismatch", "POLICY_PROOF_BLOB_MISMATCH"),
    ("pc-test-blob-mismatch", "POLICY_PROOF_BLOB_MISMATCH"),
    ("m2-first-parent-not-ps1", "POLICY_PARENT_MISMATCH"),
    ("m2-second-parent-not-t", "POLICY_REQUIRED_ANCESTOR_MISSING"),
    ("m2-parent-order-reversed", "POLICY_PARENT_MISMATCH"),
    ("m2-subject-mismatch", "POLICY_SUBJECT_MISMATCH"),
    ("m2-tree-not-t", "POLICY_COMMIT_SHAPE_MISMATCH"),
    ("m2-missing-containment-path", "POLICY_PATH_CLOSURE_VIOLATION"),
    ("m2-sixth-path", "POLICY_PATH_CLOSURE_VIOLATION"),
    ("m2-merge-time-edit", "POLICY_COMMIT_SHAPE_MISMATCH"),
    ("post-m2-source-commit", "POLICY_PATH_CLOSURE_VIOLATION"),
    ("post-m2-merge", "POLICY_COMMIT_SHAPE_MISMATCH"),
    ("s2-status-plus-source", "POLICY_PATH_CLOSURE_VIOLATION"),
    ("stale-transition-record", "POLICY_CONTEXT_UNRECOGNIZED"),
    ("ambiguous-old-new-context", "POLICY_CONTEXT_AMBIGUOUS"),
    ("malformed-git-facts", "POLICY_GIT_COMMAND_FAILED"),
    ("owner-runtime-path-regression", "POLICY_UNTRACKED_PROTECTED_OVERLAP"),
)

TRUSTED_BLOBS = {
    "web/src/components/ProvenanceDetailPanel.tsx": "46163cb7d5623a6aee4b40de1f1f038ae11cc9fe",
    "web/src/screens/__tests__/EvidenceProvenanceScreen.test.tsx": "636d2bd38862bddc72270add3a9a5bbfd4bdc962",
    "scripts/test_evidence_provenance_real_backend.py": "5afa8dd8f659ef263995345b5ae1969bf6b1e85f",
    "tests/helpers/evidence_provenance_fixture.py": "d147f55506ea290e85a825f547e595b6ce45e665",
    "web/playwright.real-backend.config.ts": "8c012ce65f1c148a45a5520624384fa0104024f3",
    "web/tests/real-backend/evidence-provenance.real-backend.spec.ts": "73b9b91e6b4dda361f5884fab9cffbcf731f4124",
}
TRUSTED_SHA256 = {
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


def _policy():
    try:
        return importlib.import_module("scripts.evidence_provenance_harness_git_policy")
    except ModuleNotFoundError as exc:
        pytest.fail(f"policy module is not implemented: {exc}")


def _commit(oid: str, parents: tuple[str, ...], subject: str, paths: frozenset[str], tree: str):
    return _policy().CommitRecord(oid, parents, subject, paths, tree)


def _base_commit():
    return _commit(BASE, (), "Record evidence provenance browser integration", frozenset(), "0" * 40)


def _safe_commit():
    return _commit(SAFE, (BASE,), "Allow topic-scoped run links in provenance detail", SAFE_PATHS, "1" * 40)


def _feature_commit():
    return _commit(
        FEATURE,
        (SAFE,),
        "Add real-backend evidence provenance browser test",
        IMPLEMENTATION_PATHS,
        "2" * 40,
    )


def _integration_commit():
    return _commit(
        INTEGRATION,
        (BASE, FEATURE),
        "Merge real-backend evidence provenance browser test",
        MERGE_PATHS,
        "3" * 40,
    )


def _status_commit(oid: str = STATUS, parent: str = INTEGRATION, paths: frozenset[str] | None = None):
    return _commit(
        oid,
        (parent,),
        "Record real-backend evidence provenance browser integration",
        frozenset({"STATUS.md"}) if paths is None else paths,
        "4" * 40,
    )


def _policy_feature_commit(parent: str = STATUS, paths: frozenset[str] = POLICY_PATHS):
    return _commit(
        POLICY_FEATURE,
        (parent,),
        "Generalize real-backend harness Git policy",
        paths,
        "5" * 40,
    )


def _policy_merge_commit(
    parents: tuple[str, ...] = (STATUS, POLICY_FEATURE),
    paths: frozenset[str] = POLICY_PATHS,
    tree: str = "5" * 40,
):
    return _commit(
        POLICY_MERGE,
        parents,
        "Merge reusable real-backend harness policy",
        paths,
        tree,
    )


def _historical_blobs():
    module = _policy()
    return tuple(
        module.BlobRecord(path, oid, TRUSTED_SHA256.get(path)) for path, oid in sorted(TRUSTED_BLOBS.items())
    )


def _checkpoint_blobs():
    module = _policy()
    records = [
        module.BlobRecord(path, oid, TRUSTED_SHA256.get(path))
        for path, oid in sorted(TRUSTED_BLOBS.items())
        if path != "scripts/test_evidence_provenance_real_backend.py"
    ]
    records.extend(
        (
            module.BlobRecord("scripts/test_evidence_provenance_real_backend.py", "6" * 40, None),
            module.BlobRecord("scripts/evidence_provenance_harness_git_policy.py", "7" * 40, None),
            module.BlobRecord("tests/test_evidence_provenance_real_backend_harness_policy.py", "8" * 40, None),
        )
    )
    return tuple(sorted(records, key=lambda item: item.path))


def _checkout(context: str):
    module = _policy()
    base = _base_commit()
    safe = _safe_commit()
    feature = _feature_commit()
    integration = _integration_commit()
    common_related = (base, safe, feature, integration)
    if context == "feature":
        return module.CheckoutRecord(
            branch=SOURCE_BRANCH,
            head=FEATURE,
            head_commit=feature,
            first_parent_commits=(),
            related_commits=(base, safe),
            blobs=_historical_blobs(),
            checkpoint_blobs=(),
            verified_ancestors=frozenset({BASE, SAFE}),
            tracked_clean=True,
            index_empty=True,
            protected_untracked_paths=(),
        )
    if context == "merge":
        return module.CheckoutRecord(
            branch=RC_BRANCH,
            head=INTEGRATION,
            head_commit=integration,
            first_parent_commits=(),
            related_commits=(base, safe, feature),
            blobs=_historical_blobs(),
            checkpoint_blobs=(),
            verified_ancestors=frozenset({BASE, SAFE, FEATURE}),
            tracked_clean=True,
            index_empty=True,
            protected_untracked_paths=(),
        )
    status = _status_commit()
    if context == "status":
        return module.CheckoutRecord(
            branch=RC_BRANCH,
            head=STATUS,
            head_commit=status,
            first_parent_commits=(status,),
            related_commits=common_related,
            blobs=_historical_blobs(),
            checkpoint_blobs=(),
            verified_ancestors=frozenset({BASE, SAFE, FEATURE, INTEGRATION}),
            tracked_clean=True,
            index_empty=True,
            protected_untracked_paths=(),
        )
    policy_feature = _policy_feature_commit()
    policy_merge = _policy_merge_commit()
    if context == "checkpoint":
        return module.CheckoutRecord(
            branch=RC_BRANCH,
            head=POLICY_MERGE,
            head_commit=policy_merge,
            first_parent_commits=(status, policy_merge),
            related_commits=common_related + (policy_feature,),
            blobs=_checkpoint_blobs(),
            checkpoint_blobs=_checkpoint_blobs(),
            verified_ancestors=frozenset({BASE, SAFE, FEATURE, INTEGRATION, STATUS}),
            tracked_clean=True,
            index_empty=True,
            protected_untracked_paths=(),
        )
    raise AssertionError(f"unknown fixture context: {context}")


def _with_blob(state, path: str, oid: str):
    module = _policy()
    blobs = tuple(module.BlobRecord(item.path, oid if item.path == path else item.oid, item.sha256) for item in state.blobs)
    return replace(state, blobs=blobs)


def _decision(state):
    return _policy().evaluate_checkout(state)


def _v2_commit(
    oid: str,
    parents: tuple[str, ...],
    subject: str,
    paths: frozenset[str],
    tree: str,
    *,
    statuses: tuple[tuple[str, str], ...] | None = None,
):
    if statuses is None:
        statuses = tuple((path, "M") for path in sorted(paths))
    return _policy().CommitRecord(
        oid=oid,
        parents=parents,
        subject=subject,
        changed_paths=paths,
        tree_oid=tree,
        changed_path_statuses=statuses,
    )


def _previous_policy_feature_commit():
    return _v2_commit(
        PREVIOUS_POLICY_FEATURE,
        (STATUS,),
        "Generalize real-backend harness Git policy",
        POLICY_PATHS,
        "5" * 40,
    )


def _previous_checkpoint_commit():
    return _v2_commit(
        PREVIOUS_CHECKPOINT,
        (STATUS, PREVIOUS_POLICY_FEATURE),
        "Merge reusable real-backend harness policy",
        POLICY_PATHS,
        "5" * 40,
    )


def _ps1_commit():
    return _v2_commit(PS1, (PREVIOUS_CHECKPOINT,), PS1_SUBJECT, frozenset({"STATUS.md"}), PS1_TREE)


def _containment_commit():
    return _v2_commit(
        CONTAINMENT,
        (PS1,),
        CONTAINMENT_SUBJECT,
        CONTAINMENT_PATHS,
        CONTAINMENT_TREE,
    )


def _transition_commit():
    return _v2_commit(
        TRANSITION,
        (CONTAINMENT,),
        TRANSITION_SUBJECT,
        POLICY_PATHS,
        TRANSITION_TREE,
    )


def _checkpoint_two_commit():
    return _v2_commit(
        CHECKPOINT_TWO,
        (PS1, TRANSITION),
        CHECKPOINT_TWO_SUBJECT,
        CHECKPOINT_TWO_PATHS,
        TRANSITION_TREE,
    )


def _checkpoint_status_commit(
    oid: str = CHECKPOINT_STATUS,
    parent: str = CHECKPOINT_TWO,
    *,
    subject: str = CHECKPOINT_STATUS_SUBJECT,
    paths: frozenset[str] = frozenset({"STATUS.md"}),
    parents: tuple[str, ...] | None = None,
):
    return _v2_commit(
        oid,
        (parent,) if parents is None else parents,
        subject,
        paths,
        "b" * 40,
    )


def _containment_blobs(
    provider_oid: str = CONTAINMENT_BLOBS["app/services/providers/__init__.py"],
    test_oid: str = CONTAINMENT_BLOBS["tests/test_provider_output_class_proof.py"],
):
    module = _policy()
    return (
        module.BlobRecord("app/services/providers/__init__.py", provider_oid),
        module.BlobRecord("tests/test_provider_output_class_proof.py", test_oid),
    )


def _v2_checkout(context: str):
    module = _policy()
    base = _base_commit()
    safe = _safe_commit()
    feature = _feature_commit()
    integration = _integration_commit()
    status = _status_commit()
    previous_feature = _previous_policy_feature_commit()
    previous_checkpoint = _previous_checkpoint_commit()
    ps1 = _ps1_commit()
    containment = _containment_commit()
    transition = _transition_commit()
    checkpoint = _checkpoint_two_commit()
    common_related = (base, safe, feature, integration, previous_feature, containment, transition)
    common_ancestors = frozenset(
        {
            BASE,
            SAFE,
            FEATURE,
            INTEGRATION,
            STATUS,
            PREVIOUS_CHECKPOINT,
            PS1,
            CONTAINMENT,
        }
    )
    if context == "previous-checkpoint":
        return module.CheckoutRecord(
            branch=RC_BRANCH,
            head=PREVIOUS_CHECKPOINT,
            head_commit=previous_checkpoint,
            first_parent_commits=(status, previous_checkpoint),
            related_commits=(base, safe, feature, integration, previous_feature),
            blobs=_checkpoint_blobs(),
            checkpoint_blobs=_checkpoint_blobs(),
            containment_blobs=(),
            verified_ancestors=common_ancestors - {PS1, CONTAINMENT},
            tracked_clean=True,
            index_empty=True,
            protected_untracked_paths=(),
        )
    if context == "ps1":
        return module.CheckoutRecord(
            branch=RC_BRANCH,
            head=PS1,
            head_commit=ps1,
            first_parent_commits=(status, previous_checkpoint, ps1),
            related_commits=(base, safe, feature, integration, previous_feature),
            blobs=_checkpoint_blobs(),
            checkpoint_blobs=_checkpoint_blobs(),
            containment_blobs=(),
            verified_ancestors=common_ancestors - {CONTAINMENT},
            tracked_clean=True,
            index_empty=True,
            protected_untracked_paths=(),
        )

    first_parent = (status, previous_checkpoint, ps1, checkpoint)
    head = checkpoint
    if context == "s2":
        first_status = _checkpoint_status_commit()
        first_parent = (*first_parent, first_status)
        head = first_status
    elif context == "multiple-status":
        first_status = _checkpoint_status_commit()
        second_status = _checkpoint_status_commit(
            CHECKPOINT_STATUS_TWO,
            CHECKPOINT_STATUS,
            subject="Record later provider containment validation",
        )
        first_parent = (*first_parent, first_status, second_status)
        head = second_status
    elif context != "m2":
        raise AssertionError(f"unknown v2 fixture context: {context}")
    return module.CheckoutRecord(
        branch=RC_BRANCH,
        head=head.oid,
        head_commit=head,
        first_parent_commits=first_parent,
        related_commits=common_related,
        blobs=_checkpoint_blobs(),
        checkpoint_blobs=_checkpoint_blobs(),
        containment_blobs=_containment_blobs(),
        verified_ancestors=common_ancestors,
        tracked_clean=True,
        index_empty=True,
        protected_untracked_paths=(),
    )


def _replace_commit(state, oid: str, replacement):
    return replace(
        state,
        head_commit=replacement if state.head_commit.oid == oid else state.head_commit,
        first_parent_commits=tuple(
            replacement if commit.oid == oid else commit for commit in state.first_parent_commits
        ),
        related_commits=tuple(replacement if commit.oid == oid else commit for commit in state.related_commits),
    )


def _non_executable_v2_state(case: str):
    state = _v2_checkout("m2")
    prefix = state.first_parent_commits[:3]
    containment = _containment_commit()
    if case == "pc-full-execution":
        return replace(
            state,
            head=CONTAINMENT,
            head_commit=containment,
            first_parent_commits=(*prefix, containment),
        )
    transition = _transition_commit()
    return replace(
        state,
        head=TRANSITION,
        head_commit=transition,
        first_parent_commits=(*prefix, containment, transition),
    )


def _v2_mutation(case: str):
    state = _v2_checkout("m2")
    containment = _containment_commit()
    transition = _transition_commit()
    checkpoint = _checkpoint_two_commit()
    if case == "t-parent-not-pc":
        return _replace_commit(state, TRANSITION, replace(transition, parents=(UNRELATED,)))
    if case == "t-subject-mismatch":
        return _replace_commit(state, TRANSITION, replace(transition, subject="Unexpected transition"))
    if case == "t-fourth-path":
        paths = POLICY_PATHS | {"README.md"}
        return _replace_commit(
            state,
            TRANSITION,
            replace(
                transition,
                changed_paths=paths,
                changed_path_statuses=tuple((path, "M") for path in sorted(paths)),
            ),
        )
    if case == "pc-subject-mismatch":
        return _replace_commit(state, CONTAINMENT, replace(containment, subject="Unexpected containment"))
    if case == "pc-tree-mismatch":
        return _replace_commit(state, CONTAINMENT, replace(containment, tree_oid="c" * 40))
    if case == "pc-provider-blob-mismatch":
        return replace(state, containment_blobs=_containment_blobs(provider_oid="c" * 40))
    if case == "pc-test-blob-mismatch":
        return replace(state, containment_blobs=_containment_blobs(test_oid="c" * 40))
    if case == "m2-first-parent-not-ps1":
        return _replace_commit(state, CHECKPOINT_TWO, replace(checkpoint, parents=(UNRELATED, TRANSITION)))
    if case == "m2-second-parent-not-t":
        return _replace_commit(state, CHECKPOINT_TWO, replace(checkpoint, parents=(PS1, UNRELATED)))
    if case == "m2-parent-order-reversed":
        return _replace_commit(state, CHECKPOINT_TWO, replace(checkpoint, parents=(TRANSITION, PS1)))
    if case == "m2-subject-mismatch":
        return _replace_commit(state, CHECKPOINT_TWO, replace(checkpoint, subject="Unexpected checkpoint"))
    if case == "m2-tree-not-t":
        return _replace_commit(state, CHECKPOINT_TWO, replace(checkpoint, tree_oid="c" * 40))
    if case == "m2-missing-containment-path":
        paths = CHECKPOINT_TWO_PATHS - {"app/services/providers/__init__.py"}
        return _replace_commit(
            state,
            CHECKPOINT_TWO,
            replace(
                checkpoint,
                changed_paths=paths,
                changed_path_statuses=tuple((path, "M") for path in sorted(paths)),
            ),
        )
    if case == "m2-sixth-path":
        paths = CHECKPOINT_TWO_PATHS | {"README.md"}
        return _replace_commit(
            state,
            CHECKPOINT_TWO,
            replace(
                checkpoint,
                changed_paths=paths,
                changed_path_statuses=tuple((path, "M") for path in sorted(paths)),
            ),
        )
    if case == "m2-merge-time-edit":
        statuses = tuple(
            (path, "A" if path == "app/services/providers/__init__.py" else "M")
            for path in sorted(CHECKPOINT_TWO_PATHS)
        )
        return _replace_commit(state, CHECKPOINT_TWO, replace(checkpoint, changed_path_statuses=statuses))
    if case == "post-m2-source-commit":
        later = _v2_commit(
            UNRELATED,
            (CHECKPOINT_TWO,),
            "Change source after checkpoint",
            frozenset({"app/main.py"}),
            "c" * 40,
        )
        return replace(
            state,
            head=UNRELATED,
            head_commit=later,
            first_parent_commits=(*state.first_parent_commits, later),
        )
    if case == "post-m2-merge":
        later = _v2_commit(
            UNRELATED,
            (CHECKPOINT_TWO, TRANSITION),
            "Merge after checkpoint",
            frozenset({"STATUS.md"}),
            "c" * 40,
        )
        return replace(
            state,
            head=UNRELATED,
            head_commit=later,
            first_parent_commits=(*state.first_parent_commits, later),
        )
    if case == "s2-status-plus-source":
        state = _v2_checkout("s2")
        status = _checkpoint_status_commit(paths=frozenset({"STATUS.md", "app/main.py"}))
        return _replace_commit(state, CHECKPOINT_STATUS, status)
    if case == "stale-transition-record":
        return replace(state, first_parent_commits=state.first_parent_commits[:-1])
    if case == "ambiguous-old-new-context":
        conflicting = replace(transition, subject="Conflicting transition record")
        return replace(state, related_commits=(*state.related_commits, conflicting))
    if case == "malformed-git-facts":
        return _replace_commit(state, CHECKPOINT_TWO, replace(checkpoint, tree_oid="not-a-tree"))
    if case == "owner-runtime-path-regression":
        return replace(state, protected_untracked_paths=("scripts/runtime-guard-drift.py",))
    raise AssertionError(f"unknown v2 mutation: {case}")


def _exact_m2_commit():
    return _v2_commit(
        M2,
        (PS1, TRANSITION_TWO),
        CHECKPOINT_TWO_SUBJECT,
        CHECKPOINT_TWO_PATHS,
        M2_TREE,
    )


def _exact_transition_two_commit():
    return _v2_commit(
        TRANSITION_TWO,
        (CONTAINMENT,),
        TRANSITION_SUBJECT,
        POLICY_PATHS,
        M2_TREE,
    )


def _exact_s2_commit():
    return _v2_commit(S2, (M2,), S2_SUBJECT, frozenset({"STATUS.md"}), S2_TREE)


def _routing_feature_commit():
    statuses = tuple((path, ROUTING_FEATURE_STATUSES[path]) for path in sorted(ROUTING_FEATURE_PATHS))
    return _v2_commit(
        ROUTING_FEATURE,
        (S2,),
        ROUTING_FEATURE_SUBJECT,
        ROUTING_FEATURE_PATHS,
        ROUTING_FEATURE_TREE,
        statuses=statuses,
    )


def _q3_commit():
    return _v2_commit(Q3, (ROUTING_FEATURE,), Q3_SUBJECT, POLICY_PATHS, Q3_TREE)


def _g3_commit():
    statuses = tuple(
        (path, ROUTING_FEATURE_STATUSES.get(path, "M")) for path in sorted(G3_PATHS)
    )
    return _v2_commit(G3, (S2, Q3), G3_SUBJECT, G3_PATHS, Q3_TREE, statuses=statuses)


def _s3_commit(
    oid: str = S3,
    parent: str = G3,
    *,
    subject: str = S3_SUBJECT,
    paths: frozenset[str] = frozenset({"STATUS.md"}),
    parents: tuple[str, ...] | None = None,
):
    return _v2_commit(
        oid,
        (parent,) if parents is None else parents,
        subject,
        paths,
        "2f" * 20,
    )


def _provider_routing_blobs(changed_path: str | None = None):
    module = _policy()
    return tuple(
        module.BlobRecord(path, "0" * 40 if path == changed_path else oid)
        for path, oid in sorted(ROUTING_FEATURE_BLOBS.items())
    )


def _v3_policy_blobs():
    module = _policy()
    return tuple(module.BlobRecord(path, oid) for path, oid in sorted(Q3_POLICY_BLOBS.items()))


def _v3_current_blobs():
    module = _policy()
    records = [
        module.BlobRecord(path, oid, TRUSTED_SHA256.get(path))
        for path, oid in sorted(V3_PROOF_BLOBS.items())
    ]
    records.extend(module.BlobRecord(path, oid) for path, oid in sorted(Q3_POLICY_BLOBS.items()))
    return tuple(sorted(records, key=lambda item: item.path))


def _v3_checkout(context: str = "g3"):
    module = _policy()
    base = _base_commit()
    safe = _safe_commit()
    historical_feature = _feature_commit()
    integration = _integration_commit()
    status = _status_commit()
    previous_feature = _previous_policy_feature_commit()
    previous_checkpoint = _previous_checkpoint_commit()
    ps1 = _ps1_commit()
    containment = _containment_commit()
    transition_two = _exact_transition_two_commit()
    m2 = _exact_m2_commit()
    s2 = _exact_s2_commit()
    routing_feature = _routing_feature_commit()
    q3 = _q3_commit()
    g3 = _g3_commit()
    first_parent = (status, previous_checkpoint, ps1, m2, s2, g3)
    head = g3
    if context == "s3":
        first_status = _s3_commit()
        first_parent = (*first_parent, first_status)
        head = first_status
    elif context == "multiple-status":
        first_status = _s3_commit()
        second_status = _s3_commit(S3_TWO, S3, subject="Record later generation-3 validation")
        first_parent = (*first_parent, first_status, second_status)
        head = second_status
    elif context != "g3":
        raise AssertionError(f"unknown v3 fixture context: {context}")
    related = (
        base,
        safe,
        historical_feature,
        integration,
        previous_feature,
        containment,
        transition_two,
        routing_feature,
        q3,
    )
    return module.CheckoutRecord(
        branch=RC_BRANCH,
        head=head.oid,
        head_commit=head,
        first_parent_commits=first_parent,
        related_commits=related,
        blobs=_v3_current_blobs(),
        checkpoint_blobs=_v3_policy_blobs(),
        containment_blobs=_containment_blobs(),
        provider_routing_blobs=_provider_routing_blobs(),
        verified_ancestors=frozenset(
            {
                BASE,
                SAFE,
                FEATURE,
                INTEGRATION,
                STATUS,
                PREVIOUS_CHECKPOINT,
                PS1,
                CONTAINMENT,
                M2,
                S2,
                ROUTING_FEATURE,
                Q3,
            }
        ),
        tracked_clean=True,
        index_empty=True,
        protected_untracked_paths=(),
    )


def _v3_feature_state(case: str):
    state = _v3_checkout("g3")
    prefix = state.first_parent_commits[:-1]
    routing_feature = _routing_feature_commit()
    if case == "f":
        return replace(
            state,
            head=ROUTING_FEATURE,
            head_commit=routing_feature,
            first_parent_commits=(*prefix, routing_feature),
        )
    q3 = _q3_commit()
    return replace(
        state,
        head=Q3,
        head_commit=q3,
        first_parent_commits=(*prefix, routing_feature, q3),
    )


def _v3_mutation(case: str):
    state = _v3_checkout("g3")
    routing_feature = _routing_feature_commit()
    q3 = _q3_commit()
    g3 = _g3_commit()
    if case == "f-parent-not-s2":
        return _replace_commit(state, ROUTING_FEATURE, replace(routing_feature, parents=(UNRELATED,)))
    if case == "f-subject-mismatch":
        return _replace_commit(state, ROUTING_FEATURE, replace(routing_feature, subject="Unexpected feature"))
    if case == "f-tree-mismatch":
        return _replace_commit(state, ROUTING_FEATURE, replace(routing_feature, tree_oid="0" * 40))
    if case == "f-path-status-mismatch":
        statuses = tuple(
            (path, "A" if path == "app/services/provider_orchestration.py" else status)
            for path, status in routing_feature.changed_path_statuses
        )
        return _replace_commit(state, ROUTING_FEATURE, replace(routing_feature, changed_path_statuses=statuses))
    if case in {"f-missing-path", "f-seventh-path"}:
        paths = (
            ROUTING_FEATURE_PATHS - {"app/services/provider_output_acceptance.py"}
            if case == "f-missing-path"
            else ROUTING_FEATURE_PATHS | {"README.md"}
        )
        statuses = tuple((path, ROUTING_FEATURE_STATUSES.get(path, "M")) for path in sorted(paths))
        return _replace_commit(
            state,
            ROUTING_FEATURE,
            replace(routing_feature, changed_paths=paths, changed_path_statuses=statuses),
        )
    if case == "q-parent-not-f":
        return _replace_commit(state, Q3, replace(q3, parents=(UNRELATED,)))
    if case == "q-subject-mismatch":
        return _replace_commit(state, Q3, replace(q3, subject="Unexpected policy transition"))
    if case == "q-fourth-path":
        paths = POLICY_PATHS | {"README.md"}
        return _replace_commit(
            state,
            Q3,
            replace(q3, changed_paths=paths, changed_path_statuses=tuple((path, "M") for path in sorted(paths))),
        )
    if case == "g3-first-parent-not-s2":
        return _replace_commit(state, G3, replace(g3, parents=(UNRELATED, Q3)))
    if case == "g3-second-parent-not-q":
        return _replace_commit(state, G3, replace(g3, parents=(S2, UNRELATED)))
    if case == "g3-parent-order-reversed":
        return _replace_commit(state, G3, replace(g3, parents=(Q3, S2)))
    if case == "g3-subject-mismatch":
        return _replace_commit(state, G3, replace(g3, subject="Unexpected generation-3 checkpoint"))
    if case == "g3-tree-not-q":
        return _replace_commit(state, G3, replace(g3, tree_oid="0" * 40))
    if case in {"g3-missing-feature-path", "g3-tenth-path"}:
        paths = (
            G3_PATHS - {"app/services/provider_output_acceptance.py"}
            if case == "g3-missing-feature-path"
            else G3_PATHS | {"README.md"}
        )
        statuses = tuple((path, ROUTING_FEATURE_STATUSES.get(path, "M")) for path in sorted(paths))
        return _replace_commit(state, G3, replace(g3, changed_paths=paths, changed_path_statuses=statuses))
    if case == "g3-merge-time-edit":
        statuses = tuple(
            (path, "A" if path == "scripts/evidence_provenance_harness_git_policy.py" else status)
            for path, status in g3.changed_path_statuses
        )
        return _replace_commit(state, G3, replace(g3, changed_path_statuses=statuses))
    if case == "post-g3-source-commit":
        later = _v2_commit(
            UNRELATED,
            (G3,),
            "Change source after generation-3 checkpoint",
            frozenset({"app/main.py"}),
            "0" * 40,
        )
        return replace(
            state,
            head=UNRELATED,
            head_commit=later,
            first_parent_commits=(*state.first_parent_commits, later),
        )
    if case == "post-g3-merge":
        later = _v2_commit(
            UNRELATED,
            (G3, Q3),
            "Merge after generation-3 checkpoint",
            frozenset({"STATUS.md"}),
            "0" * 40,
        )
        return replace(
            state,
            head=UNRELATED,
            head_commit=later,
            first_parent_commits=(*state.first_parent_commits, later),
        )
    if case == "s3-status-plus-source":
        state = _v3_checkout("s3")
        status = _s3_commit(paths=frozenset({"STATUS.md", "app/main.py"}))
        return _replace_commit(state, S3, status)
    raise AssertionError(f"v3 mutation needs bounded handling: {case}")


def test_01_accepts_exact_committed_feature_head():
    decision = _decision(_checkout("feature"))
    assert decision.accepted
    assert decision.context is _policy().PolicyContext.COMMITTED_FEATURE_HEAD
    assert decision.execution_mode is _policy().ExecutionMode.HISTORICAL_PROVENANCE_ONLY


def test_02_accepts_exact_original_rc_merge_head():
    decision = _decision(_checkout("merge"))
    assert decision.accepted
    assert decision.context is _policy().PolicyContext.RC_INTEGRATION_MERGE_HEAD


def test_03_accepts_current_status_descendant():
    decision = _decision(_checkout("status"))
    assert decision.accepted
    assert decision.context is _policy().PolicyContext.RC_STATUS_DESCENDANT
    assert decision.status_commit_count == 1


def test_04_accepts_multiple_historical_status_descendants():
    state = _checkout("status")
    second = _status_commit(STATUS_TWO, STATUS)
    decision = _decision(replace(state, head=STATUS_TWO, head_commit=second, first_parent_commits=(*state.first_parent_commits, second)))
    assert decision.accepted
    assert decision.status_commit_count == 2


def test_05_accepts_structural_policy_checkpoint():
    decision = _decision(_checkout("checkpoint"))
    assert decision.accepted
    assert decision.context is _policy().PolicyContext.RC_POLICY_CHECKPOINT_HEAD
    assert decision.execution_mode is _policy().ExecutionMode.FULL_HARNESS_ALLOWED
    assert decision.checkpoint_oid == POLICY_MERGE


def test_06_accepts_multiple_status_descendants_after_checkpoint():
    state = _checkout("checkpoint")
    first = _status_commit(POLICY_STATUS, POLICY_MERGE)
    second = _status_commit("b" * 40, POLICY_STATUS)
    decision = _decision(
        replace(
            state,
            head=second.oid,
            head_commit=second,
            first_parent_commits=(*state.first_parent_commits, first, second),
        )
    )
    assert decision.accepted
    assert decision.context is _policy().PolicyContext.RC_POLICY_STATUS_DESCENDANT
    assert decision.status_commit_count == 2


def test_07_rejects_wrong_branch():
    decision = _decision(replace(_checkout("feature"), branch="revive/unrelated"))
    assert not decision.accepted
    assert decision.reason == "POLICY_BRANCH_MISMATCH"


def test_08_rejects_wrong_feature_tip():
    state = _checkout("feature")
    wrong = replace(state.head_commit, oid="9" * 40)
    assert not _decision(replace(state, head=wrong.oid, head_commit=wrong)).accepted


def test_09_rejects_wrong_original_merge_first_parent():
    state = _checkout("merge")
    wrong = replace(state.head_commit, parents=("9" * 40, FEATURE))
    assert _decision(replace(state, head_commit=wrong)).reason == "POLICY_PARENT_MISMATCH"


def test_10_rejects_wrong_original_merge_second_parent():
    state = _checkout("merge")
    wrong = replace(state.head_commit, parents=(BASE, "9" * 40))
    assert _decision(replace(state, head_commit=wrong)).reason == "POLICY_PARENT_MISMATCH"


def test_11_rejects_seventh_merge_path():
    state = _checkout("merge")
    wrong = replace(state.head_commit, changed_paths=MERGE_PATHS | {"README.md"})
    assert _decision(replace(state, head_commit=wrong)).reason == "POLICY_PATH_CLOSURE_VIOLATION"


def test_12_rejects_changed_safe_link_component_blob():
    assert _decision(_with_blob(_checkout("status"), "web/src/components/ProvenanceDetailPanel.tsx", "9" * 40)).reason == "POLICY_PROOF_BLOB_MISMATCH"


def test_13_rejects_changed_safe_link_regression_blob():
    path = "web/src/screens/__tests__/EvidenceProvenanceScreen.test.tsx"
    assert _decision(_with_blob(_checkout("status"), path, "9" * 40)).reason == "POLICY_PROOF_BLOB_MISMATCH"


def test_14_rejects_changed_historical_harness_blob_outside_checkpoint():
    path = "scripts/test_evidence_provenance_real_backend.py"
    assert _decision(_with_blob(_checkout("status"), path, "9" * 40)).reason == "POLICY_PROOF_BLOB_MISMATCH"


def test_15_rejects_changed_fixture_blob():
    path = "tests/helpers/evidence_provenance_fixture.py"
    assert _decision(_with_blob(_checkout("status"), path, "9" * 40)).reason == "POLICY_PROOF_BLOB_MISMATCH"


def test_16_rejects_changed_playwright_config_blob():
    path = "web/playwright.real-backend.config.ts"
    assert _decision(_with_blob(_checkout("status"), path, "9" * 40)).reason == "POLICY_PROOF_BLOB_MISMATCH"


def test_17_rejects_changed_real_backend_spec_blob():
    path = "web/tests/real-backend/evidence-provenance.real-backend.spec.ts"
    assert _decision(_with_blob(_checkout("status"), path, "9" * 40)).reason == "POLICY_PROOF_BLOB_MISMATCH"


def test_18_rejects_status_commit_that_changes_source():
    state = _checkout("status")
    wrong = replace(state.head_commit, changed_paths=frozenset({"STATUS.md", "app/main.py"}))
    assert _decision(replace(state, head_commit=wrong, first_parent_commits=(wrong,))).reason == "POLICY_PATH_CLOSURE_VIOLATION"


def test_19_rejects_merge_after_status_descendant():
    state = _checkout("status")
    merge = _status_commit(STATUS_TWO, STATUS)
    merge = replace(merge, parents=(STATUS, FEATURE))
    assert not _decision(replace(state, head=merge.oid, head_commit=merge, first_parent_commits=(*state.first_parent_commits, merge))).accepted


def test_20_rejects_multi_parent_current_status_descendant():
    state = _checkout("status")
    wrong = replace(state.head_commit, parents=(INTEGRATION, FEATURE))
    assert _decision(replace(state, head_commit=wrong, first_parent_commits=(wrong,))).reason == "POLICY_COMMIT_SHAPE_MISMATCH"


def test_21_rejects_later_package_or_lockfile_change():
    state = _checkout("status")
    later = _status_commit(STATUS_TWO, STATUS, frozenset({"web/package-lock.json"}))
    assert not _decision(replace(state, head=later.oid, head_commit=later, first_parent_commits=(*state.first_parent_commits, later))).accepted


def test_22_rejects_later_vite_config_change():
    state = _checkout("status")
    later = _status_commit(STATUS_TWO, STATUS, frozenset({"web/vite.config.ts"}))
    assert not _decision(replace(state, head=later.oid, head_commit=later, first_parent_commits=(*state.first_parent_commits, later))).accepted


def test_23_rejects_later_mocked_smoke_change():
    state = _checkout("status")
    later = _status_commit(STATUS_TWO, STATUS, frozenset({"web/tests/smoke/evidence-provenance.smoke.spec.ts"}))
    assert not _decision(replace(state, head=later.oid, head_commit=later, first_parent_commits=(*state.first_parent_commits, later))).accepted


def test_24_rejects_new_untracked_protected_source_file():
    state = replace(_checkout("status"), protected_untracked_paths=("scripts/new_policy.py",))
    assert _decision(state).reason == "POLICY_UNTRACKED_PROTECTED_OVERLAP"


def test_25_rejects_staged_path():
    assert _decision(replace(_checkout("status"), index_empty=False)).reason == "POLICY_INDEX_NOT_EMPTY"


def test_26_rejects_dirty_tracked_worktree():
    assert _decision(replace(_checkout("status"), tracked_clean=False)).reason == "POLICY_TRACKED_DIRTY"


def test_27_detects_changed_owner_artifact():
    module = _policy()
    before = (module.OwnerArtifactRecord("AUDIT.md", "file", 1, 2, "a" * 64),)
    after = (replace(before[0], sha256="b" * 64),)
    assert module.compare_owner_inventories(before, after) == (False, ("AUDIT.md",))


def test_28_detects_added_owner_artifact():
    module = _policy()
    item = module.OwnerArtifactRecord("AUDIT.md", "file", 1, 2, "a" * 64)
    added = module.OwnerArtifactRecord("OTHER.md", "file", 1, 2, "b" * 64)
    assert module.compare_owner_inventories((item,), (item, added)) == (False, ("OTHER.md",))


def test_29_detects_removed_owner_artifact():
    module = _policy()
    item = module.OwnerArtifactRecord("AUDIT.md", "file", 1, 2, "a" * 64)
    assert module.compare_owner_inventories((item,), ()) == (False, ("AUDIT.md",))


def test_30_rejects_owner_artifact_overlapping_protected_path():
    assert _policy().is_protected_untracked_path("web/tests/new.spec.ts")
    assert not _policy().is_protected_untracked_path("AUDIT_JOURNAL.md")


def test_31_rejects_missing_required_ancestor():
    state = _checkout("status")
    assert _decision(replace(state, verified_ancestors=state.verified_ancestors - {SAFE})).reason == "POLICY_REQUIRED_ANCESTOR_MISSING"


def test_32_rejects_ambiguous_conflicting_commit_facts():
    state = _checkout("status")
    conflicting = replace(_integration_commit(), subject="Conflicting integration record")
    assert _decision(replace(state, related_commits=(*state.related_commits, conflicting))).reason == "POLICY_CONTEXT_AMBIGUOUS"


def test_33_rejects_unknown_expected_head_or_bypass_cli_option():
    harness = importlib.import_module("scripts.test_evidence_provenance_real_backend")
    parser = harness._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--expected-head", "9" * 40])
    with pytest.raises(SystemExit):
        parser.parse_args(["--allow-current-head"])


def test_34_rejects_malformed_git_fact_and_preserves_failure_precedence():
    state = _checkout("feature")
    malformed = replace(state.head_commit, oid="not-an-oid")
    assert _decision(replace(state, head="not-an-oid", head_commit=malformed)).reason == "POLICY_GIT_COMMAND_FAILED"

    harness = importlib.import_module("scripts.test_evidence_provenance_real_backend")
    resolution = harness._resolve_failure_precedence(
        "POLICY_PROOF_BLOB_MISMATCH",
        ("RUNTIME_ARTIFACT_GUARD_DRIFT", "PORT_NOT_RELEASED"),
    )
    assert resolution.primary_failure == "POLICY_PROOF_BLOB_MISMATCH"
    assert resolution.secondary_failure_codes == (
        "SECONDARY_RUNTIME_GUARD_DRIFT",
        "SECONDARY_PORT_RELEASE_FAILED",
    )


@pytest.mark.parametrize(
    "case",
    [
        "exact-m2",
        "exact-s2",
        "multiple-status-descendants-after-m2",
        "pm-ps1-historical-preservation",
    ],
)
def test_35_v2_accepts_required_contexts(case: str) -> None:
    module = _policy()
    assert module.POLICY_VERSION == "evidence_provenance_multi_context_v3"
    if case == "pm-ps1-historical-preservation":
        previous = _decision(_v2_checkout("previous-checkpoint"))
        ps1 = _decision(_v2_checkout("ps1"))
        assert previous.accepted
        assert previous.context is module.PolicyContext.RC_POLICY_CHECKPOINT_HEAD
        assert previous.checkpoint_oid == PREVIOUS_CHECKPOINT
        assert previous.status_commit_count == 0
        assert previous.previous_checkpoint_oid is None
        assert previous.checkpoint_generation is None
        assert previous.transition_id is None
        assert previous.proof_critical_blobs_verified is True
        assert ps1.accepted
        assert ps1.context is module.PolicyContext.RC_POLICY_STATUS_DESCENDANT
        assert ps1.checkpoint_oid == PREVIOUS_CHECKPOINT
        assert ps1.status_commit_count == 1
        assert ps1.previous_checkpoint_oid is None
        assert ps1.checkpoint_generation is None
        assert ps1.transition_id is None
        assert ps1.proof_critical_blobs_verified is True
        return

    context = {
        "exact-m2": "m2",
        "exact-s2": "s2",
        "multiple-status-descendants-after-m2": "multiple-status",
    }[case]
    decision = _decision(_v2_checkout(context))
    expected_count = {
        "exact-m2": 0,
        "exact-s2": 1,
        "multiple-status-descendants-after-m2": 2,
    }[case]
    expected_context = (
        module.PolicyContext.RC_POLICY_CHECKPOINT_HEAD
        if case == "exact-m2"
        else module.PolicyContext.RC_POLICY_STATUS_DESCENDANT
    )
    assert decision.accepted
    assert decision.context is expected_context
    assert decision.execution_mode is module.ExecutionMode.FULL_HARNESS_ALLOWED
    assert decision.checkpoint_oid == CHECKPOINT_TWO
    assert decision.status_commit_count == expected_count
    assert decision.previous_checkpoint_oid == PREVIOUS_CHECKPOINT
    assert decision.checkpoint_generation == 2
    assert decision.transition_id == TRANSITION_ID
    assert decision.proof_critical_blobs_verified is True


@pytest.mark.parametrize(
    "case",
    ["pc-full-execution", "t-full-execution"],
)
def test_36_v2_rejects_non_executable_feature_heads(case: str) -> None:
    decision = _decision(_non_executable_v2_state(case))
    assert not decision.accepted
    assert decision.execution_mode is not _policy().ExecutionMode.FULL_HARNESS_ALLOWED
    assert decision.reason == "POLICY_CONTEXT_UNRECOGNIZED"
    assert decision.proof_critical_blobs_verified is False


@pytest.mark.parametrize(
    "case, expected_reason",
    V2_MUTATION_CASES,
    ids=[case for case, _reason in V2_MUTATION_CASES],
)
def test_37_v2_rejects_transition_mutations(case: str, expected_reason: str) -> None:
    decision = _decision(_v2_mutation(case))
    assert not decision.accepted
    assert decision.reason == expected_reason
    assert decision.proof_critical_blobs_verified is False


@pytest.mark.parametrize(
    "case",
    ["environment-expected-head", "cli-expected-head"],
)
def test_38_v2_rejects_mutable_checkpoint_bypasses(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if case == "environment-expected-head":
        monkeypatch.setenv("XRML_EVIDENCE_PROVENANCE_EXPECTED_HEAD", CHECKPOINT_TWO)
        decision = _decision(_non_executable_v2_state("pc-full-execution"))
        assert not decision.accepted
        assert decision.reason == "POLICY_CONTEXT_UNRECOGNIZED"
        return

    harness = importlib.import_module("scripts.test_evidence_provenance_real_backend")
    parser = harness._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--expected-head", CHECKPOINT_TWO])


@pytest.mark.parametrize(
    "case",
    [
        "exact-g3",
        "exact-s3",
        "multiple-status-descendants-after-g3",
        "generation1-generation2-history-preserved",
    ],
)
def test_39_v3_accepts_required_contexts(case: str) -> None:
    module = _policy()
    assert module.POLICY_VERSION == "evidence_provenance_multi_context_v3"
    if case == "generation1-generation2-history-preserved":
        for state in (
            _v2_checkout("previous-checkpoint"),
            _v2_checkout("ps1"),
            _v2_checkout("m2"),
            _v2_checkout("s2"),
        ):
            assert _decision(state).accepted
        return

    context = {
        "exact-g3": "g3",
        "exact-s3": "s3",
        "multiple-status-descendants-after-g3": "multiple-status",
    }[case]
    decision = _decision(_v3_checkout(context))
    expected_count = {"exact-g3": 0, "exact-s3": 1, "multiple-status-descendants-after-g3": 2}[case]
    expected_context = (
        module.PolicyContext.RC_POLICY_CHECKPOINT_HEAD
        if case == "exact-g3"
        else module.PolicyContext.RC_POLICY_STATUS_DESCENDANT
    )
    assert decision.accepted
    assert decision.context is expected_context
    assert decision.execution_mode is module.ExecutionMode.FULL_HARNESS_ALLOWED
    assert decision.checkpoint_oid == G3
    assert decision.status_commit_count == expected_count
    assert decision.previous_checkpoint_oid == M2
    assert decision.checkpoint_generation == 3
    assert decision.transition_id == V3_TRANSITION_ID
    assert decision.proof_critical_blobs_verified is True


@pytest.mark.parametrize("case", ["f-full-execution", "q-full-execution"])
def test_40_v3_rejects_non_executable_feature_heads(case: str) -> None:
    decision = _decision(_v3_feature_state("f" if case.startswith("f-") else "q"))
    assert not decision.accepted
    assert decision.execution_mode is not _policy().ExecutionMode.FULL_HARNESS_ALLOWED
    assert decision.proof_critical_blobs_verified is False


@pytest.mark.parametrize("case", V3_MUTATION_CASES)
def test_41_v3_rejects_transition_mutations(case: str) -> None:
    if case == "f-integrated-blob-mismatch":
        for path in ROUTING_FEATURE_BLOBS:
            decision = _decision(replace(_v3_checkout("g3"), provider_routing_blobs=_provider_routing_blobs(path)))
            assert not decision.accepted
            assert decision.reason == "POLICY_PROOF_BLOB_MISMATCH"
            assert decision.proof_critical_blobs_verified is False
        return
    if case == "stale-ambiguous-or-malformed-generation-record":
        state = _v3_checkout("g3")
        stale = replace(state, first_parent_commits=state.first_parent_commits[:-1])
        conflicting_q = replace(_q3_commit(), subject="Conflicting generation-3 transition")
        ambiguous = replace(state, related_commits=(*state.related_commits, conflicting_q))
        malformed = _replace_commit(state, G3, replace(_g3_commit(), tree_oid="not-a-tree"))
        for mutation in (stale, ambiguous, malformed):
            decision = _decision(mutation)
            assert not decision.accepted
            assert decision.proof_critical_blobs_verified is False
        return
    decision = _decision(_v3_mutation(case))
    assert not decision.accepted
    assert decision.proof_critical_blobs_verified is False


@pytest.mark.parametrize("case", ["environment-expected-checkpoint", "cli-expected-checkpoint"])
def test_42_v3_rejects_mutable_checkpoint_bypasses(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if case == "environment-expected-checkpoint":
        monkeypatch.setenv("XRML_EVIDENCE_PROVENANCE_EXPECTED_CHECKPOINT", G3)
        decision = _decision(_v3_feature_state("q"))
        assert not decision.accepted
        fields = _policy().CheckoutRecord.__dataclass_fields__
        assert not any("expected" in name or "override" in name for name in fields)
        return

    harness = importlib.import_module("scripts.test_evidence_provenance_real_backend")
    parser = harness._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--expected-checkpoint", G3])
