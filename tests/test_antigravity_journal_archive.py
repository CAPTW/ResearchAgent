"""Self-contained Antigravity provenance; no runtime or database required."""
from copy import deepcopy
import json

import pytest

from app.domain import generated_journal, journal_execution
from app.domain.journal_exchange import preserved_json
from app.domain.temporal_memory import digest
from app.services.providers.base import ProviderResult
from tests.test_journal_review_packet import frozen_job, proposal, NOW


def archive(*, with_manifest=True):
    from app.services.providers.antigravity_journal_plan import build_plan
    job = frozen_job()
    contract = journal_execution.prepare(job, journal_execution.Prepare(request_id="archive-agy", expected_input_digest=job["input_digest"],
        provider_name="antigravity_cli", model="synthetic-agy"), "owner", NOW)
    payload = proposal(contract["packet"])
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    result = journal_execution.result(contract, ProviderResult("antigravity_cli", "not_reported", "fixture", payload, raw_text=raw))
    event = dict(state="SUCCEEDED", attempt=1, at=NOW, result=result)
    _, _, manifest = build_plan(contract, runtime_identity={name: "a" * 64 for name in
        ("launcher_sha256", "environment_sha256", "workspace_sha256")}, timeout_seconds=60)
    value = generated_journal.assemble(contract, event, "owner", NOW, adapter_manifest=manifest if with_manifest else None)
    return value, job, raw


def test_candidate_keeps_response_bytes_and_separate_stream_provenance():
    value, job, raw = archive()
    generated_journal.validate(value, job)
    assert value["raw_json"] == raw and raw != preserved_json(value["proposal"])
    assert value["execution"]["schema_version"] == "JOURNAL_EXECUTION_RECEIPT_V2"
    assert value["execution"]["adapter_manifest"]["raw_envelope_retained"] is False
    assert "Antigravity 최종 결과 응답의 JSON" in value["markdown"]
    assert "전체 NDJSON 출력 로그" in value["markdown"]
    assert "Claude" not in value["markdown"]


@pytest.mark.parametrize("field", ["runtime_identity", "timeout_seconds", "encoding"])
@pytest.mark.parametrize("rehash_manifest", [False, True])
def test_archive_detects_manifest_mutation(field, rehash_manifest):
    value, job, _ = archive()
    changed = deepcopy(value)
    manifest = changed["execution"]["adapter_manifest"]
    if field == "runtime_identity":
        manifest[field]["launcher_sha256"] = "b" * 64
    elif field == "timeout_seconds":
        manifest[field] = 61.0
    else:
        manifest["raw_envelope_retained"] = True
    if rehash_manifest:
        changed["execution"]["adapter_manifest_digest"] = digest(manifest)
    with pytest.raises(ValueError, match="integrity_invalid"):
        generated_journal.validate(changed, job)


def test_legacy_v1_candidate_and_identity_stay_unchanged():
    old, job, _ = archive(with_manifest=False)
    new, _, _ = archive()
    generated_journal.validate(old, job)
    generated_journal.validate(new, job)
    assert old["candidate_id"] == new["candidate_id"]
    assert old["execution"]["schema_version"] == "JOURNAL_EXECUTION_RECEIPT_V1"
    assert "adapter_manifest" not in old["execution"]
    assert "원본 JSON SHA-256" in old["markdown"]
    assert "구조화된 결과의 보존 방식" not in old["markdown"]
