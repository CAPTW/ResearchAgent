"""Local full-document extraction is separate from stored reviews and model work."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import sys
from types import SimpleNamespace
from threading import Barrier

import pytest

from app.auth.resource_access import ResourceActor
from app.db import bootstrap
from app.repositories import paper_reviews
from app.repositories.files import safe_filename
from tests import test_review_preparation
from tests import test_browser_collection

linked = test_review_preparation.linked
seeded = test_review_preparation.seeded
browser_collection = test_browser_collection.collection


def modules():
    return (importlib.import_module("app.domain.review_pdf_extraction"),
            importlib.import_module("app.repositories.review_pdf_extractions"),
            importlib.import_module("app.services.review_pdf_extraction"))


def _confirmed_browser_pdf(fixture):
    test_browser_collection.start(fixture)
    test_browser_collection.upload(fixture)
    saved = test_browser_collection.confirm(fixture)["items"][0]["saved"]
    return saved, dict(topic=fixture["topic"], version=saved["paper_version_id"],
        actor=fixture["actor"], run=saved["run_id"], paper="2610.99999v1")


def test_browser_attachment_inventory_omits_unbound_alias_missing_but_keeps_damaged_attachment(browser_collection):
    from app.domain import review_pdf_attachment
    domain, _, service = modules()
    saved, scope = _confirmed_browser_pdf(browser_collection)
    before = bootstrap.metadata_index_path().read_bytes()
    inventory = service.inventory(scope["topic"], scope["version"], scope["actor"])
    assert len(inventory["pdfs"]) == 1 and inventory["pdfs"][0]["attachment_id"] == saved["attachment_id"]
    assert inventory["unavailable"] == []
    assert bootstrap.metadata_index_path().read_bytes() == before
    digest = hashlib.sha256(browser_collection["raw"]).hexdigest()
    path = bootstrap.STORAGE_ROOT / scope["run"] / review_pdf_attachment.reference(digest)[4:]
    path.write_bytes(b"changed fixture PDF")
    damaged = service.inventory(scope["topic"], scope["version"], scope["actor"])
    assert damaged["pdfs"] == []
    assert damaged["unavailable"] == [dict(diagnostic_id=saved["attachment_id"], run_id=scope["run"],
                                           reason="review_pdf_source_changed")]


def test_browser_attachment_does_not_hide_missing_preserved_primary_extraction(browser_collection):
    domain, _, service = modules()
    _, scope = _confirmed_browser_pdf(browser_collection)
    path = bootstrap.STORAGE_ROOT / scope["run"] / "raw" / (safe_filename(scope["paper"]) + ".pdf")
    path.write_bytes(browser_collection["raw"])
    record = service.extract(scope["topic"], scope["version"], domain.Extract(
        run_id=scope["run"], paper_id=scope["paper"],
        expected_pdf_sha256=hashlib.sha256(browser_collection["raw"]).hexdigest()), scope["actor"])["extraction"]
    path.unlink()
    inventory = service.inventory(scope["topic"], scope["version"], scope["actor"])
    assert inventory["extractions"] == [record] and len(inventory["pdfs"]) == 1
    assert inventory["unavailable"] == [dict(diagnostic_id=domain.sha(domain.encoded([scope["run"], scope["paper"]])),
        run_id=scope["run"], reason="review_pdf_source_missing")]
    assert service.read_text(record).startswith("Synthetic collection")


def test_browser_attachment_does_not_hide_missing_explicit_legacy_alias_pdf_link(browser_collection):
    from app.services import review_input_history
    from tests.test_ai_review_provider import _review
    domain, _, service = modules()
    _, scope = _confirmed_browser_pdf(browser_collection)
    aliases = review_input_history.visible_occurrences(scope["topic"], scope["version"], scope["actor"])
    alias = next(paper for run, paper in aliases if run == scope["run"] and paper != scope["paper"])
    paper_reviews.upsert_review(_review(1, topic_id=scope["topic"], run_id=scope["run"], paper_id=alias,
        full_text_artifact_path="run:raw/" + safe_filename(alias) + ".pdf"))
    inventory = service.inventory(scope["topic"], scope["version"], scope["actor"])
    assert len(inventory["pdfs"]) == 1
    assert inventory["unavailable"] == [dict(diagnostic_id=domain.sha(domain.encoded([scope["run"], alias])),
        run_id=scope["run"], reason="review_pdf_source_missing")]


@pytest.mark.parametrize("reason", ["review_pdf_source_invalid", "review_pdf_source_too_large", "review_pdf_source_changed"])
def test_browser_attachment_never_hides_primary_integrity_errors(browser_collection, monkeypatch, reason):
    domain, _, service = modules()
    _, scope = _confirmed_browser_pdf(browser_collection)
    real = service._pdf
    def invalid(run, paper):
        if (run, paper) == (scope["run"], scope["paper"]):
            raise ValueError(reason)
        return real(run, paper)
    monkeypatch.setattr(service, "_pdf", invalid)
    inventory = service.inventory(scope["topic"], scope["version"], scope["actor"])
    assert len(inventory["pdfs"]) == 1
    assert inventory["unavailable"] == [dict(diagnostic_id=domain.sha(domain.encoded([scope["run"], scope["paper"]])),
        run_id=scope["run"], reason=reason)]


@pytest.fixture
def pdf_only(linked, monkeypatch):
    path = bootstrap.STORAGE_ROOT / linked["run"] / "raw" / (safe_filename(linked["paper"]) + ".pdf")
    path.write_bytes(b"%PDF-1.7\nsynthetic local fixture")
    pages = ["A" * 30000, "", "e\u0301 한글🙂 final page. " * 20]

    class Document:
        page_count = len(pages)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def load_page(self, index):
            return SimpleNamespace(get_text=lambda _: pages[index])

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda *_: Document(), VersionBind="fixture"))
    review = paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True)
    paper_reviews.upsert_review({**review, "full_text_artifact_path": "run:raw/" + path.name})
    return {**linked, "pdf": path, "pages": pages, "pdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def body(fixture):
    domain, _, _ = modules()
    return domain.Extract(run_id=fixture["run"], paper_id=fixture["paper"], expected_pdf_sha256=fixture["pdf_sha256"])


def extract(fixture):
    _, _, service = modules()
    return service.extract(fixture["topic"], fixture["version"], body(fixture), fixture["actor"])


def test_pdf_only_full_extraction_connects_unicode_tail_without_replacing_old_review(pdf_only):
    from app.services import review_preparation as preparation
    from app.domain.review_preparation import Prepare, Preview

    _, _, service = modules()
    before = paper_reviews.get_review(pdf_only["topic"], pdf_only["run"], pdf_only["paper"], read_only=True)
    old_bytes = pdf_only["path"].read_bytes()
    inventory = service.inventory(pdf_only["topic"], pdf_only["version"], pdf_only["actor"])
    assert dict(run_id=pdf_only["run"], paper_id=pdf_only["paper"], pdf_sha256=pdf_only["pdf_sha256"],
                pdf_bytes=pdf_only["pdf"].stat().st_size) in inventory["pdfs"]
    result = extract(pdf_only)
    record = result["extraction"]
    assert result["created"] is True
    text = " ".join(part.strip() for part in pdf_only["pages"] if part.strip())
    assert service.read_text(record) == text
    assert record["source"]["source_chars"] == len(text) > 24000
    assert record["provenance"]["visited_page_numbers"] == [1, 2, 3]
    assert record["no_text_page_numbers"] == [2] and record["unvisited_page_numbers"] == []
    assert record["page_ranges"] == [dict(page_number=1, start=0, end=30000, chars=30000),
        dict(page_number=3, start=30001, end=len(text), chars=len(text) - 30001)]
    assert record["execution"] == {"started": False, "full_document_review": "NOT_ESTABLISHED"}
    source = next(row for row in preparation.sources(pdf_only["topic"], pdf_only["version"], pdf_only["actor"])["sources"]
                  if row.get("extraction_id") == record["extraction_id"])
    selected = dict(run_id=pdf_only["run"], paper_id=pdf_only["paper"], expected_source_digest=source["source_digest"], start=30001)
    preview = preparation.preview(pdf_only["topic"], pdf_only["version"], Preview(**selected, length=1000), pdf_only["actor"])
    assert preview["window"]["text"] == text[30001:]
    saved = preparation.prepare(pdf_only["topic"], pdf_only["version"], Prepare(**selected, end=len(text), request_id="pdf-tail"), pdf_only["actor"])
    assert saved["selection"]["text"] == text[30001:]
    assert saved["source"] == record["source"]
    assert paper_reviews.get_review(pdf_only["topic"], pdf_only["run"], pdf_only["paper"], read_only=True) == before
    assert pdf_only["path"].read_bytes() == old_bytes


def test_saved_detail_and_exact_replay_survive_original_pdf_deletion(pdf_only, monkeypatch):
    _, _, service = modules()
    result = extract(pdf_only)
    record = result["extraction"]
    pdf_only["pdf"].unlink()
    monkeypatch.setattr(service.pdf_acquisition, "extract_pdf_to_text_artifacts", lambda **_: pytest.fail("replay must not extract"))
    assert extract(pdf_only) == {"extraction": record, "created": False}
    assert service.detail(pdf_only["topic"], pdf_only["version"], record["extraction_id"], pdf_only["actor"]) == record
    assert service.read_text(record).endswith("final page.")
    importlib.reload(service)
    assert service.detail(pdf_only["topic"], pdf_only["version"], record["extraction_id"], pdf_only["actor"]) == record


def test_original_pdf_change_before_extraction_refuses_without_record(pdf_only):
    _, repo, _ = modules()
    pdf_only["pdf"].write_bytes(b"%PDF-1.7 changed")
    with pytest.raises(ValueError, match="review_pdf_source_changed"):
        extract(pdf_only)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []


def test_failed_link_transaction_keeps_artifacts_recoverable_and_preserves_first_record(pdf_only, monkeypatch):
    _, repo, service = modules()
    monkeypatch.setattr(repo, "checkpoint", lambda _: (_ for _ in ()).throw(RuntimeError("fixture rollback")))
    with pytest.raises(RuntimeError, match="fixture rollback"):
        extract(pdf_only)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []
    monkeypatch.setattr(repo, "checkpoint", lambda _: None)
    result = extract(pdf_only)
    assert result["created"] is True
    assert extract(pdf_only) == {**result, "created": False}
    assert service.inventory(pdf_only["topic"], pdf_only["version"], pdf_only["actor"])["extractions"] == [result["extraction"]]


def test_new_source_is_usable_when_legacy_link_is_broken_but_new_bytes_are_checked(pdf_only):
    from app.services import review_preparation as preparation
    from app.domain.review_preparation import Preview
    _, _, service = modules()
    record = extract(pdf_only)["extraction"]
    pdf_only["pdf"].unlink()
    request = Preview(run_id=pdf_only["run"], paper_id=pdf_only["paper"], expected_source_digest=record["source"]["source_digest"], start=0, length=20)
    assert preparation.preview(pdf_only["topic"], pdf_only["version"], request, pdf_only["actor"])["window"]["text"] == "A" * 20
    path = bootstrap.STORAGE_ROOT / pdf_only["run"] / record["source"]["artifact_ref"][4:]
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="review_preparation_source_changed"):
        service.read_text(record)
    with pytest.raises(ValueError, match="review_preparation_source_changed"):
        preparation.preview(pdf_only["topic"], pdf_only["version"], request, pdf_only["actor"])


def test_scope_before_decoding_and_public_read_is_not_extraction_permission(pdf_only, monkeypatch):
    from app.services import review_input_history
    _, repo, service = modules()
    record = extract(pdf_only)["extraction"]
    monkeypatch.setattr(repo, "_read", lambda _: pytest.fail("hidden record must not be decoded"))
    with pytest.raises((KeyError, FileNotFoundError)):
        service.detail(pdf_only["other_topic"], pdf_only["version"], record["extraction_id"], pdf_only["actor"])
    with pytest.raises((KeyError, FileNotFoundError)):
        service.inventory(pdf_only["topic"], pdf_only["version"], ResourceActor.oidc_user("outsider"))
    profiles = review_input_history.topic_profiles
    monkeypatch.setattr(profiles, "SYSTEM_TEMPLATE_PROFILE_IDS", profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {pdf_only["topic"]})
    with pytest.raises((KeyError, FileNotFoundError)):
        service.extract(pdf_only["topic"], pdf_only["version"], body(pdf_only), ResourceActor.oidc_user("public-reader"))


def test_immutable_record_refuses_rehashed_page_range_corruption(pdf_only):
    domain, repo, service = modules()
    record = extract(pdf_only)["extraction"]
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER review_pdf_extractions_no_update")
        corrupt = json.loads(domain.encoded(record))
        corrupt["page_ranges"][0]["end"] -= 1
        payload = domain.encoded(corrupt)
        c.execute("UPDATE paper_pdf_extractions SET payload_json=?,payload_sha256=?", (payload, domain.sha(payload)))
    with pytest.raises(ValueError, match="review_pdf_extraction_integrity_invalid"):
        service.detail(pdf_only["topic"], pdf_only["version"], record["extraction_id"], pdf_only["actor"])
    assert repo.listing(pdf_only["topic"], pdf_only["version"], []) == []


def test_concurrent_duplicate_extractions_publish_one_link_and_exact_first_record(pdf_only):
    _, repo, _ = modules()
    barrier = Barrier(3)

    def create(_):
        barrier.wait()
        return extract(pdf_only)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(create, range(3)))
    assert sum(result["created"] for result in results) == 1
    assert all(result["extraction"] == results[0]["extraction"] for result in results)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == [results[0]["extraction"]]


def test_partial_file_publication_is_not_linked_and_identical_retry_recovers(pdf_only, monkeypatch):
    _, repo, service = modules()
    original = service.pdf_acquisition.write_json

    def fail(path, payload):
        if path.name == "document.pages.json":
            raise OSError("fixture partial publication")
        return original(path, payload)

    monkeypatch.setattr(service.pdf_acquisition, "write_json", fail)
    with pytest.raises(ValueError, match="review_pdf_extraction_failed"):
        extract(pdf_only)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []
    monkeypatch.setattr(service.pdf_acquisition, "write_json", original)
    result = extract(pdf_only)
    assert result["created"] is True
    assert service.read_text(result["extraction"]).endswith("final page.")
    directory = bootstrap.STORAGE_ROOT / pdf_only["run"] / "extractions" / result["extraction"]["extraction_id"]
    assert not (directory / "document.extraction.pending.json").exists()


def test_changed_pdf_during_engine_read_cannot_publish_a_link(pdf_only, monkeypatch):
    _, repo, service = modules()
    original = service.pdf_acquisition._extract_pdf_pages

    def change(path, max_text_chars):
        value = original(path, max_text_chars)
        pdf_only["pdf"].write_bytes(b"%PDF-1.7 changed during extraction")
        return value

    monkeypatch.setattr(service.pdf_acquisition, "_extract_pdf_pages", change)
    with pytest.raises(ValueError, match="review_pdf_source_changed"):
        extract(pdf_only)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []


def test_image_only_engine_text_has_no_success_link(pdf_only):
    _, repo, _ = modules()
    pdf_only["pages"][:] = ["", "  ", ""]
    with pytest.raises(ValueError, match="review_pdf_extraction_failed"):
        extract(pdf_only)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []


def test_character_cap_is_explicitly_truncated_not_full_document_review(pdf_only):
    from app.domain.review_preparation import MAX_SOURCE_CHARS
    _, _, service = modules()
    pdf_only["pages"][0] = "X" * (MAX_SOURCE_CHARS + 10)
    record = extract(pdf_only)["extraction"]
    assert len(service.read_text(record)) == MAX_SOURCE_CHARS
    assert record["source"]["extraction_truncation"] == "TRUNCATED"
    assert record["provenance"]["visited_page_numbers"] == [1]
    assert record["unvisited_page_numbers"] == [2, 3]
    assert record["execution"]["full_document_review"] == "NOT_ESTABLISHED"


def test_saved_model_input_receipt_remains_exactly_unchanged(pdf_only):
    from app.repositories import ai_review_drafts
    from tests.test_review_input_integration import _seed_receipt

    receipt_id, _ = _seed_receipt(pdf_only["topic"], pdf_only["run"], pdf_only["paper"])
    before = ai_review_drafts.get_input_receipt(pdf_only["topic"], receipt_id)
    record = extract(pdf_only)["extraction"]
    assert record["execution"]["started"] is False
    assert ai_review_drafts.get_input_receipt(pdf_only["topic"], receipt_id) == before


@pytest.mark.parametrize("kind", ["hardlink", "reparse", "oversized", "nonpdf"])
def test_local_pdf_guards_reject_before_engine_or_write(pdf_only, monkeypatch, kind):
    domain, repo, service = modules()
    if kind == "hardlink":
        pdf_only["pdf"].with_name("fixture-hardlink.pdf").hardlink_to(pdf_only["pdf"])
    elif kind == "reparse":
        original = service.is_reparse_point
        monkeypatch.setattr(service, "is_reparse_point", lambda path: path == pdf_only["pdf"].parent or original(path))
    elif kind == "oversized":
        monkeypatch.setattr(domain, "MAX_PDF_BYTES", 5)
    else:
        pdf_only["pdf"].write_bytes(b"not a PDF document")
    monkeypatch.setattr(service.pdf_acquisition, "extract_pdf_to_text_artifacts", lambda **_: pytest.fail("guard must precede engine"))
    with pytest.raises(ValueError, match="review_pdf_source_(invalid|too_large)"):
        extract(pdf_only)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []


def test_actual_app_routes_replay_scope_and_reject_client_paths_or_provider_flags(pdf_only, monkeypatch):
    from app.api import paper_bank as api
    from tests.test_review_preparation_api import client
    from app.services import ai_provider
    _, _, service = modules()
    monkeypatch.setattr(ai_provider, "run_ai_review_provider", lambda *_, **__: pytest.fail("no model calls"))
    monkeypatch.setattr(service.pdf_acquisition, "acquire_pdf", lambda **_: pytest.fail("no acquisition"))
    root = f'/api/topics/{pdf_only["topic"]}/paper-bank/{pdf_only["version"]}/review-preparations/pdf-extractions'
    with client() as http:
        assert http.get(root).status_code == 200
        request = body(pdf_only).model_dump()
        for extra in ({"pdf_path": "C:/private.pdf"}, {"url": "https://example.invalid"},
                      {"approval": True}, {"provider": "codex"}, {"max_chars": 5}):
            assert http.post(root, json={**request, **extra}).status_code == 422
        assert http.post(root, json={**request, "run_id": "other-run"}).status_code == 404
        response = http.post(root, json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["created"] is True
        assert http.get(root + "/" + result["extraction"]["extraction_id"]).json() == result["extraction"]
        pdf_only["pdf"].unlink()
        assert http.post(root, json=request).json() == {**result, "created": False}
        monkeypatch.setattr(api, "resource_actor_for_user", lambda _: ResourceActor.oidc_user("outsider"))
        assert http.get(root).status_code == 404
        assert http.get(root + "/" + result["extraction"]["extraction_id"]).status_code == 404
        assert http.post(root, json=request).status_code == 404


def test_real_synthetic_pdf_records_all_engine_visited_pages_without_a_model(linked):
    fitz = pytest.importorskip("fitz", reason="Locked optional fulltext dependency not installed")
    path = bootstrap.STORAGE_ROOT / linked["run"] / "raw" / (safe_filename(linked["paper"]) + ".pdf")
    with fitz.open() as document:
        document.new_page().insert_text((60, 70), "First synthetic page text. " * 6)
        document.new_page()
        document.new_page().insert_text((60, 70), "Last synthetic page text. " * 6)
        document.save(path)
    fixture = {**linked, "pdf": path, "pdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    _, _, service = modules()
    record = extract(fixture)["extraction"]
    assert record["provenance"]["document_page_count"] == 3
    assert record["provenance"]["visited_page_numbers"] == [1, 2, 3]
    assert record["no_text_page_numbers"] == [2]
    assert record["unvisited_page_numbers"] == []
    assert "Last synthetic page" in service.read_text(record)


@pytest.mark.parametrize("condition", ["missing_engine", "encrypted", "page_limit"])
def test_preflight_rejects_unsupported_document_before_text_engine(pdf_only, monkeypatch, condition):
    domain, repo, service = modules()
    expected = {"missing_engine": "review_pdf_engine_unavailable", "encrypted": "review_pdf_encrypted",
                "page_limit": "review_pdf_page_limit_exceeded"}[condition]
    if condition == "missing_engine":
        monkeypatch.setitem(sys.modules, "fitz", None)
    elif condition == "page_limit":
        monkeypatch.setattr(domain, "MAX_DOCUMENT_PAGES", 2)
    else:
        original = sys.modules["fitz"].open

        def encrypted(*args):
            document = original(*args)
            document.needs_pass = True
            return document

        monkeypatch.setattr(sys.modules["fitz"], "open", encrypted)
    monkeypatch.setattr(service.pdf_acquisition, "extract_pdf_to_text_artifacts", lambda **_: pytest.fail("preflight must precede extraction"))
    with pytest.raises(ValueError, match=expected):
        extract(pdf_only)
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []


def test_conflicting_output_cannot_replace_owner_bytes_or_create_link(pdf_only):
    domain, repo, service = modules()
    extraction_id = domain.identity(pdf_only["topic"], pdf_only["version"], body(pdf_only))
    directory = bootstrap.STORAGE_ROOT / pdf_only["run"] / "extractions" / extraction_id
    directory.mkdir(parents=True)
    original = b"existing output must remain intact"
    (directory / "document.txt").write_bytes(original)
    with pytest.raises(ValueError, match="review_pdf_extraction_conflict"):
        extract(pdf_only)
    assert (directory / "document.txt").read_bytes() == original
    assert not (directory / "document.extraction.json").exists()
    assert repo.listing(pdf_only["topic"], pdf_only["version"], [(pdf_only["run"], pdf_only["paper"])]) == []


def test_new_inventory_reads_do_not_create_storage_table_or_mutate_database(pdf_only):
    _, _, service = modules()
    before = bootstrap.metadata_index_path().read_bytes()
    inventory = service.inventory(pdf_only["topic"], pdf_only["version"], pdf_only["actor"])
    assert inventory["extractions"] == []
    assert bootstrap.metadata_index_path().read_bytes() == before
    with closing(bootstrap.connect_metadata_index(read_only=True)) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='paper_pdf_extractions'").fetchone() is None


def test_missing_extractions_have_distinct_diagnostics_and_preserve_legacy_identity(pdf_only):
    from app.services import review_preparation

    legacy = review_preparation.sources(pdf_only["topic"], pdf_only["version"], pdf_only["actor"])["unavailable_sources"]
    first = extract(pdf_only)["extraction"]
    pdf_only["pdf"].write_bytes(b"%PDF-1.7\nsecond synthetic local fixture")
    pdf_only["pdf_sha256"] = hashlib.sha256(pdf_only["pdf"].read_bytes()).hexdigest()
    second = extract(pdf_only)["extraction"]
    for record in (first, second):
        (bootstrap.STORAGE_ROOT / pdf_only["run"] / record["source"]["artifact_ref"][4:]).unlink()
    unavailable = review_preparation.sources(pdf_only["topic"], pdf_only["version"], pdf_only["actor"])["unavailable_sources"]
    assert len({row["diagnostic_id"] for row in unavailable}) == len(unavailable)
    assert [row for row in unavailable if "extraction_id" not in row] == legacy
    extracted = [row for row in unavailable if "extraction_id" in row]
    assert {row["extraction_id"] for row in extracted} == {first["extraction_id"], second["extraction_id"]}
    assert all(row["reason"] == "review_preparation_not_found" for row in extracted)


def replace_record(record):
    domain, _, _ = modules()
    with closing(bootstrap.connect_metadata_index()) as connection, connection:
        connection.execute("DROP TRIGGER review_pdf_extractions_no_update")
        payload = domain.encoded(record)
        connection.execute("UPDATE paper_pdf_extractions SET payload_json=?,payload_sha256=? WHERE extraction_id=?",
                           (payload, domain.sha(payload), record["extraction_id"]))


def test_rehashed_consistent_page_boundary_corruption_cannot_supply_source_or_coverage(pdf_only):
    from app.domain.review_preparation import Preview
    from app.services import review_preparation, review_document_coverage

    domain, _, service = modules()
    record = extract(pdf_only)["extraction"]
    record["page_ranges"][0]["end"] += 1
    record["page_ranges"][0]["chars"] += 1
    record["page_ranges"][1]["start"] += 1
    record["page_ranges"][1]["chars"] -= 1
    assert domain.validate(record) == record  # Internally consistent, but not the frozen pages.
    replace_record(record)
    saved = service.detail(pdf_only["topic"], pdf_only["version"], record["extraction_id"], pdf_only["actor"])
    assert saved == record  # History remains readable without touching artifacts.
    with pytest.raises(ValueError, match="review_pdf_extraction_integrity_invalid"):
        service.read_text(saved)
    request = Preview(run_id=pdf_only["run"], paper_id=pdf_only["paper"],
                      expected_source_digest=record["source"]["source_digest"], start=0, length=20)
    with pytest.raises(ValueError, match="review_pdf_extraction_integrity_invalid"):
        review_preparation.preview(pdf_only["topic"], pdf_only["version"], request, pdf_only["actor"])
    with pytest.raises(ValueError, match="review_pdf_extraction_integrity_invalid"):
        review_document_coverage.overview(pdf_only["topic"], pdf_only["version"], record["extraction_id"], pdf_only["actor"])


@pytest.mark.parametrize("corruption", ["missing", "changed_bytes", "rehashed_number", "rehashed_shape", "rehashed_duplicate"])
def test_preserved_page_artifact_is_required_and_exact_before_text_is_reused(pdf_only, corruption):
    _, _, service = modules()
    record = extract(pdf_only)["extraction"]
    path = bootstrap.STORAGE_ROOT / pdf_only["run"] / "extractions" / record["extraction_id"] / "document.pages.json"
    if corruption == "missing":
        path.unlink()
    elif corruption == "changed_bytes":
        path.write_bytes(b"changed page bytes")
    else:
        pages = json.loads(path.read_bytes())
        if corruption == "rehashed_number":
            pages["pages"][1]["page"] = 2
        elif corruption == "rehashed_shape":
            pages["pages"][0]["unsupported"] = "must not be ignored"
        raw = json.dumps(pages, ensure_ascii=False).encode("utf-8")
        if corruption == "rehashed_duplicate":
            raw = raw.replace(b'"paper_id": "document"', b'"paper_id": "document", "paper_id": "document"', 1)
        path.write_bytes(raw)
        record["provenance"]["outputs"]["pages_sha256"] = hashlib.sha256(raw).hexdigest()
        record["provenance"]["outputs"]["pages_bytes"] = len(raw)
        replace_record(record)
    # Frozen history must not require the current PDF or any surviving file.
    pdf_only["pdf"].unlink()
    saved = service.detail(pdf_only["topic"], pdf_only["version"], record["extraction_id"], pdf_only["actor"])
    assert saved == record
    if corruption == "missing":
        with pytest.raises(KeyError, match="review_preparation_not_found"):
            service.read_text(saved)
    else:
        with pytest.raises(ValueError, match="review_pdf_extraction_integrity_invalid"):
            service.read_text(saved)
