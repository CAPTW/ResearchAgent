"""Self-contained Journal receipt compatibility; no runtime or database."""
from copy import deepcopy

import pytest

from app.domain import generated_journal, journal_execution
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderResult
from app.services.providers.claude_journal_plan import build_plan
from tests.test_journal_review_packet import frozen_job, proposal, NOW


def archive(*, with_manifest=True):
    job = frozen_job()
    contract = journal_execution.prepare(job, journal_execution.Prepare(request_id="archive", expected_input_digest=job["input_digest"],
        provider_name="claude_code", model="synthetic-claude"), "owner", NOW)
    payload = proposal(contract["packet"])
    result = journal_execution.result(contract, ProviderResult("claude_code", "not_reported", "fixture", payload, raw_text=preserved_json(payload)))
    event = dict(state="SUCCEEDED", attempt=1, at=NOW, result=result)
    _, _, manifest = build_plan(contract, runtime_identity={name: "a" * 64 for name in
        ("launcher_sha256", "environment_sha256", "workspace_sha256", "config_dir_sha256")}, timeout_seconds=60)
    value = generated_journal.assemble(contract, event, "owner", NOW, adapter_manifest=manifest if with_manifest else None)
    return value, job


@pytest.mark.parametrize("field", ["runtime_identity", "timeout_seconds"])
@pytest.mark.parametrize("rehash_manifest", [False, True])
def test_archive_detects_rebound_variable_manifest_fields(field, rehash_manifest):
    value, job = archive()
    generated_journal.validate(value, job)
    changed = deepcopy(value)
    if field == "runtime_identity":
        changed["execution"]["adapter_manifest"][field]["launcher_sha256"] = "b" * 64
    else:
        changed["execution"]["adapter_manifest"][field] = 61.0
    if rehash_manifest:
        changed["execution"]["adapter_manifest_digest"] = digest(changed["execution"]["adapter_manifest"])
    with pytest.raises(ValueError, match="integrity_invalid"):
        generated_journal.validate(changed, job)


def test_legacy_v1_candidate_and_identity_stay_unchanged():
    old, job = archive(with_manifest=False)
    new, _ = archive()
    generated_journal.validate(old, job)
    generated_journal.validate(new, job)
    assert old["candidate_id"] == new["candidate_id"]
    assert old["execution"]["schema_version"] == "JOURNAL_EXECUTION_RECEIPT_V1"
    assert "adapter_manifest" not in old["execution"]
    assert "원본 JSON SHA-256" in old["markdown"]
    assert "구조화된 결과의 보존 방식" not in old["markdown"]
