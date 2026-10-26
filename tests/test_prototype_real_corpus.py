from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.domain.constants import PROJECT_ROOT
from app.repositories.files import read_jsonl
from app.repositories.runs import build_run_paths, create_run_record, load_run_record
from app.services import prototype_real_corpus as real
from app.services.research_transport import (
    ResearchHttpClient,
    ResearchTransportPolicy,
    RetrievalTransportError,
)

PUBLIC_ADDRESS = "93.184.216.34"
GENERATED_OCCURRENCE_ID = re.compile(r"^occ-[0-9a-f]{32}$")


def _candidate(index: int = 1, *, title: str | None = None, doi: str | None = None) -> dict[str, Any]:
    return {
        "uid": str(1000 + index),
        "pmcid": f"PMC{1000 + index}",
        "pmid": str(9000 + index),
        "doi": doi or f"10.5555/prototype.{index}",
        "title": title or f"Virtual reality rehabilitation intervention study {index}",
        "authors": [f"Prototype Author {index}"],
        "journal": "Synthetic Fixture Journal",
        "publication_date": f"202{index}-01-01" if index <= 5 else "2025-01-01",
        "article_type": "journal article",
        "language": "English",
        "relevance_score": 20 - index,
    }


def _rights(candidate: dict[str, Any], license_text: str = "https://creativecommons.org/licenses/by/4.0/") -> dict[str, Any]:
    classified = real.classify_license([license_text])
    return real._with_record_digest(
        {
            "schema_version": real.RIGHTS_SCHEMA,
            "rights_record_id": f"rights-{candidate['pmcid'].lower()}",
            "pmcid": candidate["pmcid"],
            "pmid": candidate["pmid"],
            "doi": candidate["doi"],
            "source": real.SOURCE,
            **classified,
            "rights_metadata_source": "PMC_OAI_V1_OAI_DC",
            "rights_retrieval_timestamp": "2026-08-01T00:00:00+00:00",
            "acceptance_policy_version": real.RIGHTS_POLICY_VERSION,
            "article_content_digest": None,
            "metadata_digest": "a" * 64,
        }
    )


def _dc_xml(candidate: dict[str, Any], license_text: str = "https://creativecommons.org/licenses/by/4.0/") -> bytes:
    return (
        "<OAI-PMH><GetRecord><record><metadata><dc>"
        f"<identifier>oai:pubmedcentral.nih.gov:{candidate['uid']}</identifier>"
        f"<identifier>{candidate['pmcid']}</identifier>"
        f"<identifier>{candidate['doi']}</identifier>"
        f"<title>{candidate['title']}</title><rights>{license_text}</rights>"
        "</dc></metadata></record></GetRecord></OAI-PMH>"
    ).encode()


def _jats_xml(
    candidate: dict[str, Any],
    *,
    license_text: str = "https://creativecommons.org/licenses/by/4.0/",
    article_type: str = "research-article",
    title: str | None = None,
    body: bool = True,
    requested_pmcid: str | None = None,
) -> bytes:
    paragraphs = ""
    if body:
        paragraphs = (
            "<sec><title>Methods and intervention</title>"
            "<p>The virtual reality rehabilitation intervention enrolled adult participants in a structured clinical training protocol. The deterministic fixture describes the intervention schedule, comparison context, and measured outcomes without using an external model.</p></sec>"
            "<sec><title>Results</title>"
            "<p>The reported results described improved task performance after the immersive training sessions, while retaining the study-specific measurement context. The finding is bounded to the evaluated participants and outcome definition in this fixture.</p></sec>"
            "<sec><title>Discussion and limitations</title>"
            "<p>However, the small sample is a limitation and the result may not generalize to other clinical settings. Further study is required, and uncertainty remains about longer-term outcomes and different rehabilitation populations.</p></sec>"
            "<funding-group><p>Funding-only text must never become body evidence even when it contains a result keyword.</p></funding-group>"
            "<sec><title>Additional observations</title>"
            "<p>The augmented reality training workflow used a bounded sequence of guided exercises and local outcome checks. This additional paragraph gives the parser enough substantive body coverage for deterministic review.</p></sec>"
        )
    pmcid = requested_pmcid or candidate["pmcid"]
    xml = (
        "<OAI-PMH><GetRecord><record><metadata>"
        f"<article article-type=\"{article_type}\"><front><journal-meta><journal-title>Fixture Journal</journal-title></journal-meta>"
        "<article-meta>"
        f"<article-id pub-id-type=\"pmc\">{pmcid}</article-id>"
        f"<article-id pub-id-type=\"pmid\">{candidate['pmid']}</article-id>"
        f"<article-id pub-id-type=\"doi\">{candidate['doi']}</article-id>"
        f"<title-group><article-title>{title or candidate['title']}</article-title></title-group>"
        "<contrib-group><contrib contrib-type=\"author\"><name><surname>Author</surname><given-names>Prototype</given-names></name></contrib></contrib-group>"
        "<pub-date><year>2024</year><month>01</month><day>02</day></pub-date>"
        "<abstract><p>Abstract-only material is stored separately and is never selected as body evidence.</p></abstract>"
        f"<permissions><license href=\"{license_text}\"><license-p>{license_text}</license-p></license></permissions>"
        "</article-meta></front>"
        f"<body>{paragraphs}</body>"
        "<back><ref-list><ref><mixed-citation>Reference-only result text must never become evidence.</mixed-citation></ref></ref-list></back>"
        "</article></metadata></record></GetRecord></OAI-PMH>"
    )
    return xml.encode()


def _summary_json(candidates: list[dict[str, Any]]) -> bytes:
    result: dict[str, Any] = {"uids": [item["uid"] for item in candidates]}
    for item in candidates:
        result[item["uid"]] = {
            "uid": item["uid"],
            "title": item["title"],
            "pubdate": item["publication_date"],
            "doctype": item["article_type"],
            "lang": ["eng"],
            "fulljournalname": item["journal"],
            "authors": [{"name": name} for name in item["authors"]],
            "articleids": [
                {"idtype": "pmcid", "value": item["pmcid"]},
                {"idtype": "pmid", "value": item["pmid"]},
                {"idtype": "doi", "value": item["doi"]},
            ],
        }
    return json.dumps({"result": result}).encode()


def _fixture_network(*, candidates: list[dict[str, Any]] | None = None, fail_first: bool = False) -> real.PmcNetworkClient:
    corpus = candidates or [_candidate(index) for index in range(1, 5)]
    by_uid = {item["uid"]: item for item in corpus}

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_first:
            raise httpx.ConnectError("fixture transport unavailable", request=request)
        query = urllib.parse.parse_qs(request.url.query.decode())
        if request.url.path.endswith("esearch.fcgi"):
            body = json.dumps(
                {"esearchresult": {"count": str(len(corpus)), "idlist": [item["uid"] for item in corpus]}}
            ).encode()
            return httpx.Response(200, content=body, headers={"Content-Type": "application/json"}, request=request)
        if request.url.path.endswith("esummary.fcgi"):
            return httpx.Response(200, content=_summary_json(corpus), headers={"Content-Type": "application/json"}, request=request)
        if request.url.path == "/api/oai/v1/mh/":
            identifier = query["identifier"][0]
            uid = identifier.rsplit(":", 1)[-1]
            candidate = by_uid[uid]
            prefix = query["metadataPrefix"][0]
            body = _dc_xml(candidate) if prefix == "oai_dc" else _jats_xml(candidate)
            return httpx.Response(200, content=body, headers={"Content-Type": "application/xml"}, request=request)
        return httpx.Response(404, request=request)

    policy = ResearchTransportPolicy.create(
        real.ALLOWED_HOSTS,
        total_timeout_seconds=5,
        max_attempts=1,
        max_redirects=0,
        initial_backoff_seconds=0,
    )
    http_client = ResearchHttpClient(
        policy,
        resolver=lambda _host, _port: (PUBLIC_ADDRESS,),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _delay: None,
    )
    return real.PmcNetworkClient(http_client, sleeper=lambda _delay: None, clock=lambda: 0.0)


@pytest.fixture
def completed_real_run(tmp_path: Path, app_factory, monkeypatch):
    network = _fixture_network()
    monkeypatch.setattr(real, "PmcNetworkClient", lambda: network)
    data_root = (tmp_path / "prototype-real-data").resolve()
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="1",
            XRML_DATA_DIR=str(data_root),
        )
    ) as client:
        response = client.post("/prototype/real-corpus/runs", json={"confirmed": True, "network_enabled": True})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        triage = read_jsonl(build_run_paths(run_id).triage_dir / "triage.jsonl")
        assert triage
        assert all(GENERATED_OCCURRENCE_ID.fullmatch(item["occurrence_id"]) for item in triage)
        assert len({item["occurrence_id"] for item in triage}) == len(triage)
        yield client, run_id, network


def _create_tagged_run() -> str:
    run = create_run_record(topic=real.TOPIC, request_payload={}, source="pytest", write_legacy=False)
    run["prototype"] = {
        "schema_version": real.REAL_CORPUS_SCHEMA,
        "label": "test",
        "corpus_kind": "real_open_access",
        "corpus_count": 0,
        "pipeline_status": "running",
        "report_status": "pending",
        "provider": real._provider_summary(),
        "real_corpus": {},
    }
    from app.repositories.runs import save_run_record

    save_run_record(run)
    return run["run_id"]


def test_control_01_external_network_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("XRML_PROTOTYPE_REAL_CORPUS_NETWORK", raising=False)
    assert real.network_enabled() is False


def test_control_02_non_allowlisted_host_is_rejected() -> None:
    policy = ResearchTransportPolicy.create(real.ALLOWED_HOSTS, max_attempts=1, max_redirects=0)
    with pytest.raises(RetrievalTransportError):
        real._validate_endpoint("https://example.com/article", policy)


def test_control_03_redirect_to_non_allowlisted_host_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com/private"}, request=request)

    policy = ResearchTransportPolicy.create(real.ALLOWED_HOSTS, max_attempts=1, max_redirects=0)
    client = ResearchHttpClient(
        policy,
        resolver=lambda _host, _port: (PUBLIC_ADDRESS,),
        transport=httpx.MockTransport(handler),
    )
    network = real.PmcNetworkClient(client, sleeper=lambda _delay: None, clock=lambda: 0.0)
    with pytest.raises(RetrievalTransportError):
        network.fetch(
            f"{real.ESEARCH_ENDPOINT}?db=pmc",
            purpose="redirect_control",
            max_bytes=1024,
            accept="application/json",
        )


def test_control_04_legacy_pmc_oa_web_service_is_rejected() -> None:
    policy = ResearchTransportPolicy.create(real.ALLOWED_HOSTS, max_attempts=1, max_redirects=0)
    with pytest.raises(RetrievalTransportError):
        real._validate_endpoint("https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=PMC1", policy)


def test_control_05_ftp_endpoint_is_rejected() -> None:
    policy = ResearchTransportPolicy.create(real.ALLOWED_HOSTS, max_attempts=1, max_redirects=0)
    with pytest.raises(RetrievalTransportError):
        real._validate_endpoint("ftp://pmc.ncbi.nlm.nih.gov/pub/pmc/", policy)


def test_control_06_article_without_license_is_rejected() -> None:
    result = real.classify_license([])
    assert result["reuse_allowed"] is False and result["rejection_reason"] == "license_missing"


def test_control_07_ambiguous_custom_license_is_rejected() -> None:
    result = real.classify_license(["Free to read under journal terms"])
    assert result["reuse_allowed"] is False and result["rejection_reason"] == "license_ambiguous_or_custom"


def test_control_08_cc_by_nc_is_rejected() -> None:
    assert real.classify_license(["https://creativecommons.org/licenses/by-nc/4.0/"])["reuse_allowed"] is False


def test_control_09_cc_by_nd_is_rejected() -> None:
    assert real.classify_license(["CC BY-ND 4.0"])["reuse_allowed"] is False


def test_control_10_cc_by_sa_is_rejected() -> None:
    assert real.classify_license(["https://creativecommons.org/licenses/by-sa/4.0/"])["reuse_allowed"] is False


def test_control_11_cc0_is_accepted() -> None:
    result = real.classify_license(["https://creativecommons.org/publicdomain/zero/1.0/"])
    assert result["reuse_allowed"] is True and result["license_type"] == "CC0"


def test_control_12_cc_by_is_accepted() -> None:
    result = real.classify_license(["https://creativecommons.org/licenses/by/4.0/"])
    assert result["reuse_allowed"] is True and result["license_type"] == "CC BY"


def test_control_13_license_url_and_statement_digest_are_preserved() -> None:
    value = "https://creativecommons.org/licenses/by/4.0/"
    result = real.classify_license([value])
    assert result["canonical_license_url"] == value
    assert result["license_statement_digest"] == real._sha256_text(value)


def test_control_14_retraction_record_is_rejected() -> None:
    candidate = _candidate()
    with pytest.raises(real.RealCorpusError, match="real_corpus_article_type_rejected"):
        real.parse_jats_record(_jats_xml(candidate, article_type="retraction"), candidate, _rights(candidate))


def test_control_15_correction_only_record_is_rejected() -> None:
    candidate = _candidate()
    with pytest.raises(real.RealCorpusError, match="real_corpus_article_type_rejected"):
        real.parse_jats_record(_jats_xml(candidate, title="Correction to a virtual reality study"), candidate, _rights(candidate))


def test_control_16_duplicate_pmcid_is_rejected() -> None:
    seen_pmcids: set[str] = set()
    seen_works: set[str] = set()
    assert real.candidate_identity_rejection(_candidate(), seen_pmcids, seen_works) is None
    assert real.candidate_identity_rejection(_candidate(), seen_pmcids, seen_works) == "duplicate_pmcid"


def test_control_17_duplicate_doi_work_version_is_deduplicated() -> None:
    seen_pmcids: set[str] = set()
    seen_works: set[str] = set()
    assert real.candidate_identity_rejection(_candidate(1, doi="10.1/shared"), seen_pmcids, seen_works) is None
    assert (
        real.candidate_identity_rejection(_candidate(2, doi="10.1/shared"), seen_pmcids, seen_works)
        == "duplicate_work_version"
    )


def test_control_18_oversized_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "99999"}, content=b"x", request=request)

    policy = ResearchTransportPolicy.create(real.ALLOWED_HOSTS, max_attempts=1, max_redirects=0)
    client = ResearchHttpClient(
        policy,
        resolver=lambda _host, _port: (PUBLIC_ADDRESS,),
        transport=httpx.MockTransport(handler),
    )
    network = real.PmcNetworkClient(client, sleeper=lambda _delay: None, clock=lambda: 0.0)
    with pytest.raises(RetrievalTransportError, match="retrieval_response_too_large"):
        network.fetch(
            f"{real.ESEARCH_ENDPOINT}?db=pmc",
            purpose="oversize_control",
            max_bytes=10,
            accept="application/json",
        )


def test_control_19_malformed_xml_is_rejected() -> None:
    with pytest.raises(real.RealCorpusError, match="fixture_invalid"):
        real._parse_xml(b"<broken>", "fixture_invalid")


def test_control_20_truncated_xml_is_rejected() -> None:
    with pytest.raises(real.RealCorpusError, match="fixture_truncated"):
        real._parse_xml(b"<article", "fixture_truncated")


def test_control_21_pmcid_mismatch_is_rejected() -> None:
    candidate = _candidate()
    with pytest.raises(real.RealCorpusError, match="real_corpus_full_text_identity_mismatch"):
        real.parse_jats_record(
            _jats_xml(candidate, requested_pmcid="PMC999999"),
            candidate,
            _rights(candidate),
        )


def test_control_22_full_text_artifact_is_created_only_after_validation(app_factory) -> None:
    app_factory(AUTH_MODE="disabled", XRML_PROTOTYPE_MODE="1", XRML_PROTOTYPE_REAL_CORPUS_NETWORK="1")
    run_id = _create_tagged_run()
    candidate = _candidate()
    response = real.MetadataFetchResult(body=b"<truncated", content_type="application/xml", status_code=200)
    with pytest.raises(real.RealCorpusError):
        real._persist_article(run_id, candidate, _rights(candidate), response)
    assert not (build_run_paths(run_id).raw_dir / "real-corpus" / candidate["pmcid"] / "source.xml").exists()


def test_control_23_repository_path_write_is_rejected(app_factory) -> None:
    app_factory(AUTH_MODE="disabled", XRML_PROTOTYPE_MODE="1", XRML_PROTOTYPE_REAL_CORPUS_NETWORK="1")
    run_id = _create_tagged_run()
    with pytest.raises(real.RealCorpusError, match="real_corpus_artifact_path_rejected"):
        real._require_disposable_target(PROJECT_ROOT / "forbidden.xml", run_id)


def test_control_24_protected_runtime_path_write_is_rejected(tmp_path: Path, app_factory) -> None:
    app_factory(AUTH_MODE="disabled", XRML_PROTOTYPE_MODE="1", XRML_PROTOTYPE_REAL_CORPUS_NETWORK="1")
    run_id = _create_tagged_run()
    with pytest.raises(real.RealCorpusError, match="real_corpus_artifact_path_rejected"):
        real._require_disposable_target(tmp_path / "foreign-runtime" / "source.xml", run_id)


def test_control_25_parser_excludes_references_from_body_evidence() -> None:
    candidate = _candidate()
    parsed = real.parse_jats_record(_jats_xml(candidate), candidate, _rights(candidate))
    assert all("Reference-only" not in item["text"] for item in parsed["paragraphs"])


def test_control_26_parser_excludes_license_and_funding_text_from_body_evidence() -> None:
    candidate = _candidate()
    parsed = real.parse_jats_record(_jats_xml(candidate), candidate, _rights(candidate))
    body = " ".join(item["text"] for item in parsed["paragraphs"])
    assert "Funding-only" not in body
    assert "creativecommons.org" not in body


def test_control_27_article_without_usable_body_is_not_reviewed() -> None:
    candidate = _candidate()
    with pytest.raises(real.RealCorpusError, match="real_corpus_body_text_insufficient"):
        real.parse_jats_record(_jats_xml(candidate, body=False), candidate, _rights(candidate))


def test_control_28_evidence_links_to_exact_section_and_paragraph() -> None:
    candidate = _candidate()
    parsed = real.parse_jats_record(_jats_xml(candidate), candidate, _rights(candidate))
    evidence = real.extract_evidence("fixture-run", parsed, "artifact-1")
    paragraphs = {item["paragraph_id"]: item for item in parsed["paragraphs"]}
    assert len(evidence) == 3
    assert all(item["section_id"] == paragraphs[item["paragraph_id"]]["section_id"] for item in evidence)


def test_control_29_source_digest_change_makes_evidence_stale() -> None:
    assert real.evidence_is_stale({"source_content_digest": "a" * 64, "stale": False}, "b" * 64) is True


def test_control_30_cross_run_evidence_reference_is_rejected() -> None:
    evidence = [{"evidence_id": "ev-1", "run_id": "other", "stale": False}]
    claims = [{"claim_type": "grounded", "evidence_ids": ["ev-1"]}]
    assert real.validate_report_claims("current", claims, evidence)["cross_run_evidence_ids"] == 1


def test_control_31_unknown_evidence_reference_is_rejected() -> None:
    claims = [{"claim_type": "grounded", "evidence_ids": ["missing"]}]
    assert real.validate_report_claims("current", claims, [])["unknown_evidence_ids"] == 1


def test_control_32_grounded_claim_without_evidence_is_rejected() -> None:
    claims = [{"claim_type": "grounded", "evidence_ids": []}]
    assert real.validate_report_claims("current", claims, [])["unsupported_grounded_claims"] == 1


def test_control_33_license_rejected_paper_cannot_enter_report() -> None:
    article = {"rights": {"license_type": "CC BY-NC", "reuse_allowed": False}, "evidence": [], "paper": {}}
    with pytest.raises(real.RealCorpusError, match="real_corpus_report_license_boundary"):
        real.build_report("run-1", [article], _fixture_network())


def test_control_34_preview_export_excludes_raw_xml(completed_real_run) -> None:
    client, run_id, _network = completed_real_run
    preview = client.get(f"/runs/{run_id}/preview")
    export = client.post(f"/runs/{run_id}/export", json={"format": "report_markdown"})
    assert preview.status_code == 200 and export.status_code == 200
    public = json.dumps([preview.json(), export.json()])
    assert "<article" not in public and "<OAI-PMH" not in public


def test_control_35_preview_export_contains_no_absolute_path(completed_real_run) -> None:
    client, run_id, _network = completed_real_run
    preview = client.get(f"/runs/{run_id}/preview").json()
    export = client.post(f"/runs/{run_id}/export", json={"format": "report_markdown"}).json()
    public = json.dumps([preview, export]).lower()
    assert "pytest-of-" not in public and "\\users\\" not in public and ":\\" not in public


def test_control_36_cached_replay_rejects_corrupt_cache_item(completed_real_run) -> None:
    _client, run_id, _network = completed_real_run
    cache_path = next((build_run_paths(run_id).raw_dir / "real-corpus" / "cache").glob("*.json"))
    cache_path.write_text("{}", encoding="utf-8")
    with pytest.raises(real.RealCorpusError, match="real_corpus_cache_corrupt"):
        real.replay_real_corpus_run(run_id)


def test_control_37_cached_replay_performs_zero_network_requests(completed_real_run) -> None:
    client, run_id, network = completed_real_run
    before = network.request_count
    replay = client.post(f"/prototype/runs/{run_id}/real-corpus/replay")
    assert replay.status_code == 200, replay.text
    assert replay.json()["network_request_count"] == 0
    assert network.request_count == before
    replay_triage = read_jsonl(build_run_paths(replay.json()["run_id"]).triage_dir / "triage.jsonl")
    assert replay_triage
    assert all(GENERATED_OCCURRENCE_ID.fullmatch(item["occurrence_id"]) for item in replay_triage)
    assert len({item["occurrence_id"] for item in replay_triage}) == len(replay_triage)


def test_control_37b_curated_snapshot_writer_assigns_unique_occurrence_authority(
    completed_real_run,
) -> None:
    _client, source_run_id, _network = completed_real_run
    source_record = real.load_public_record(source_run_id)
    selected = [str(article["pmcid"]) for article in source_record["articles"]]

    curated = real.create_curated_real_corpus_run(
        source_run_id=source_run_id,
        selected_pmcids=selected,
        owner_user_id="local-internal-user",
        workspace_context={
            "topic_id": "curated-writer-contract",
            "snapshot_id": "snapshot-curated-writer-contract",
            "parent_run_id": source_run_id,
        },
    )
    triage = read_jsonl(build_run_paths(curated["run_id"]).triage_dir / "triage.jsonl")

    assert triage
    assert all(GENERATED_OCCURRENCE_ID.fullmatch(item["occurrence_id"]) for item in triage)
    assert len({item["occurrence_id"] for item in triage}) == len(triage)
    assert curated["real_corpus"]["provider_call_count"] == 0


def test_control_38_synthetic_and_real_corpus_labels_cannot_be_confused(completed_real_run) -> None:
    client, run_id, _network = completed_real_run
    run = client.get(f"/runs/{run_id}").json()
    status = client.get("/prototype/status").json()
    assert run["prototype"]["corpus_kind"] == "real_open_access"
    assert "synthetic" in status["label"].lower()
    assert run["prototype"]["label"] != status["label"]


def test_control_39_provider_invocation_count_remains_zero(completed_real_run) -> None:
    client, run_id, _network = completed_real_run
    record = client.get(f"/prototype/runs/{run_id}/real-corpus").json()
    run = client.get(f"/runs/{run_id}").json()
    assert record["provider_calls"] == 0
    assert run["prototype"]["provider"]["live_provider_invoked"] is False


def test_control_40_network_error_does_not_corrupt_existing_prototype_runs(
    tmp_path: Path, app_factory, monkeypatch
) -> None:
    data_root = (tmp_path / "prototype-failure-data").resolve()
    with TestClient(
        app_factory(
            AUTH_MODE="disabled",
            XRML_PROTOTYPE_MODE="1",
            XRML_PROTOTYPE_REAL_CORPUS_NETWORK="1",
            XRML_RUN_EXECUTOR_POLL_SECONDS="0.05",
            XRML_DATA_DIR=str(data_root),
        )
    ) as client:
        synthetic = client.post("/prototype/runs")
        assert synthetic.status_code == 200
        synthetic_id = synthetic.json()["run_id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current = client.get(f"/runs/{synthetic_id}").json()
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert current["status"] == "completed"
        before = build_run_paths(synthetic_id).run_json_path.read_bytes()
        failing_network = _fixture_network(fail_first=True)
        monkeypatch.setattr(real, "PmcNetworkClient", lambda: failing_network)
        failed = client.post("/prototype/real-corpus/runs", json={"confirmed": True, "network_enabled": True})
        assert failed.status_code == 200
        assert failed.json()["status"] == "failed"
        assert build_run_paths(synthetic_id).run_json_path.read_bytes() == before
        assert load_run_record(synthetic_id)["prototype"]["corpus_kind"] == "synthetic"
