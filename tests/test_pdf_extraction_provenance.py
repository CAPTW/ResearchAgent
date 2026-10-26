"""Local synthetic PDF provenance; extraction is never model review."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.services import pdf_acquisition as pdf


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def files(root: Path):
    return root / "paper.txt", root / "paper.pages.json", root / "paper.extraction.json"


def fake_document(monkeypatch, root, texts):
    source = root / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic bytes, fake engine only")

    class Document:
        page_count = len(texts)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def load_page(self, index):
            return SimpleNamespace(get_text=lambda _: texts[index])

    monkeypatch.setitem(
        sys.modules, "fitz", SimpleNamespace(open=lambda *_, **__: Document(), VersionBind="test-engine")
    )
    return source


def extract(source, cap=24000):
    return pdf.extract_pdf_to_text_artifacts(pdf_path=source, paper_id="paper", max_text_chars=cap)


def receipt(root, source):
    text, pages, _ = files(root)
    return pdf.read_extraction_provenance(text, pages, source)


def test_bounded_extraction_records_observed_pages_and_preserves_existing_text_contract(tmp_path, monkeypatch):
    source = fake_document(monkeypatch, tmp_path, ["", "A " * 200, "B " * 200])
    result = extract(source, 150)
    assert result["status"] == "extracted"
    saved = receipt(tmp_path, source)
    text, pages, marker = files(tmp_path)
    assert saved == json.loads(marker.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "PDF_EXTRACTION_PROVENANCE_V1"
    assert saved["source_pdf"] == {"sha256": sha(source.read_bytes()), "bytes": source.stat().st_size}
    assert saved["extractor"] == {"name": "pymupdf", "version": "test-engine", "normalization": "XRML_WHITESPACE_V1"}
    assert saved["document_page_count"] == 3
    assert saved["visited_page_numbers"] == [1, 2]
    assert saved["retained_page_numbers"] == [2]
    assert saved["truncation"] == "TRUNCATED" and saved["document_text_chars"] is None
    assert saved["max_chars"] == 150
    assert saved["outputs"] == {
        "text_sha256": sha(text.read_bytes()),
        "text_bytes": text.stat().st_size,
        "pages_sha256": sha(pages.read_bytes()),
        "pages_bytes": pages.stat().st_size,
    }
    assert result["quality"]["text_sha256"] == sha(text.read_bytes())
    assert result["page_count"] == 1  # Existing retained-page count is unchanged.
    assert text.read_text(encoding="utf-8") == pdf._normalize_text(("A " * 200)[:150])


def test_exact_last_page_boundary_is_not_reported_as_truncated(tmp_path, monkeypatch):
    text = "X" * 150
    source = fake_document(monkeypatch, tmp_path, ["", text])
    assert extract(source, len(text))["status"] == "extracted"
    saved = receipt(tmp_path, source)
    assert saved["visited_page_numbers"] == [1, 2] and saved["retained_page_numbers"] == [2]
    assert saved["truncation"] == "NOT_OBSERVED"
    assert saved["document_text_chars"] == 150


def test_last_page_can_be_text_truncated_with_a_known_engine_text_length(tmp_path, monkeypatch):
    source = fake_document(monkeypatch, tmp_path, ["X" * 200])
    assert extract(source, 150)["status"] == "extracted"
    saved = receipt(tmp_path, source)
    assert saved["truncation"] == "TRUNCATED" and saved["document_text_chars"] == 200


def test_legacy_list_extractor_does_not_invent_observation(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fixture")
    monkeypatch.setattr(pdf, "_extract_pdf_pages", lambda *_, **__: [{"page_number": 4, "text": "X" * 180}])
    assert extract(source)["status"] == "extracted"
    saved = receipt(tmp_path, source)
    assert saved["document_page_count"] is None and saved["document_text_chars"] is None
    assert saved["visited_page_numbers"] == [] and saved["retained_page_numbers"] == [4]
    assert saved["truncation"] == "UNKNOWN"


@pytest.mark.parametrize(
    "mutation",
    [
        "text",
        "pages",
        "pdf",
        "missing_text",
        "missing_pages",
        "missing_pdf",
        "invalid_manifest",
        "extra_field",
        "invalid_range",
        "bool_count",
    ],
)
def test_readback_rejects_changed_missing_or_invalid_bound_artifacts(tmp_path, monkeypatch, mutation):
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    assert extract(source)["status"] == "extracted"
    text, pages, marker = files(tmp_path)
    targets = {"text": text, "pages": pages, "pdf": source}
    if mutation in targets:
        targets[mutation].write_bytes(b"changed")
    elif mutation.startswith("missing_"):
        targets[mutation.removeprefix("missing_")].unlink()
    elif mutation == "invalid_manifest":
        marker.write_text("{broken", encoding="utf-8")
    else:
        content = json.loads(marker.read_text(encoding="utf-8"))
        if mutation == "extra_field":
            content["external_path"] = "C:/private.pdf"
        elif mutation == "invalid_range":
            content["visited_page_numbers"] = [2]
        else:
            content["max_chars"] = True
        marker.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="^extraction_provenance_invalid$"):
        receipt(tmp_path, source)


def test_absent_manifest_means_legacy_unknown(tmp_path):
    assert receipt(tmp_path, tmp_path / "missing.pdf") is None


def test_replay_reuses_identical_files_and_conflicting_reextraction_changes_nothing(tmp_path, monkeypatch):
    source = fake_document(monkeypatch, tmp_path, ["X" * 200])
    assert extract(source, 150)["status"] == "extracted"
    paths = files(tmp_path)
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert extract(source, 150)["status"] == "extracted"
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == before
    conflict = extract(source, 180)
    assert conflict["status"] == "conflict" and conflict["reason"] == "extraction_artifact_conflict"
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == before


def test_identical_legacy_outputs_can_gain_new_observation_without_rewriting_bytes(tmp_path, monkeypatch):
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    assert extract(source)["status"] == "extracted"
    text, pages, marker = files(tmp_path)
    marker.unlink()
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (text, pages)]
    assert extract(source)["status"] == "extracted"
    assert receipt(tmp_path, source)["document_page_count"] == 1
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in (text, pages)] == before


def test_failure_preserves_existing_files_and_marker_is_not_published_after_partial_write(tmp_path, monkeypatch):
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    text, pages, marker = files(tmp_path)
    text.write_bytes(b"owner-existing")
    before = text.read_bytes()
    assert extract(source)["status"] == "conflict"
    assert text.read_bytes() == before and not pages.exists() and not marker.exists()
    text.unlink()  # This test owns the disposable fixture.
    original = pdf.write_json

    def fail_pages(path, payload):
        if path == pages:
            raise OSError("fixture publication failure")
        return original(path, payload)

    monkeypatch.setattr(pdf, "write_json", fail_pages)
    result = extract(source)
    assert result["status"] == "failed" and result["reason"] == "extraction_artifact_write_failed"
    assert not marker.exists()
    with pytest.raises(ValueError, match="^extraction_provenance_invalid$"):
        receipt(tmp_path, source)
    monkeypatch.setattr(pdf, "write_json", original)
    assert extract(source)["status"] == "extracted"
    assert receipt(tmp_path, source) is not None


def test_concurrent_identical_extracts_replay_with_a_valid_complete_marker(tmp_path, monkeypatch):
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: extract(source), range(3)))
    assert [item["status"] for item in results] == ["extracted"] * 3
    assert receipt(tmp_path, source)["outputs"]["text_bytes"] == 180


def test_real_synthetic_pdf_retains_bounded_text_and_known_page_count(tmp_path):
    fitz = pytest.importorskip("fitz", reason="Locked optional fulltext dependency not installed")
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page()
        page = document.new_page()
        page.insert_textbox(fitz.Rect(50, 50, 540, 780), "Synthetic evidence line. " * 140, fontsize=10)
        document.new_page().insert_text((50, 50), "Final synthetic page.")
        document.save(source)
    assert extract(source, 200)["status"] == "extracted"
    saved = receipt(tmp_path, source)
    assert saved["document_page_count"] == 3
    assert saved["visited_page_numbers"] == [1, 2]
    assert saved["truncation"] == "TRUNCATED"


def test_pdf_snapshot_is_not_visible_as_another_raw_paper_and_source_change_blocks_publication(tmp_path, monkeypatch):
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    original_bytes = source.read_bytes()
    original_extract = pdf._extract_pdf_pages

    def inspect_snapshot(path, max_text_chars=24000):
        assert path != source and path.read_bytes() == original_bytes
        assert list(tmp_path.glob("*.pdf")) == [source]
        source.write_bytes(b"%PDF changed while extraction was in flight")
        return original_extract(path, max_text_chars=max_text_chars)

    monkeypatch.setattr(pdf, "_extract_pdf_pages", inspect_snapshot)
    result = extract(source)
    assert result["status"] == "failed" and result["reason"] == "source_pdf_changed"
    assert all(not path.exists() for path in files(tmp_path))
    assert list(tmp_path.glob(".xrml-extract-*")) == []


def test_pdftotext_segments_do_not_invent_physical_document_totals(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF fixture")
    monkeypatch.setattr(pdf.shutil, "which", lambda _: "fixture-pdftotext")
    monkeypatch.setattr(
        pdf.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout="\f" + "X" * 200 + "\f" + "Z" * 150 + "\f", stderr=""),
    )
    monkeypatch.setattr(pdf, "_extract_pdf_pages", pdf._extract_pdf_pages_with_pdftotext)
    assert extract(source, 150)["status"] == "extracted"
    saved = receipt(tmp_path, source)
    assert saved["extractor"]["name"] == "pdftotext"
    assert saved["visited_page_numbers"] == [1, 2] and saved["retained_page_numbers"] == [2]
    assert saved["document_page_count"] is None and saved["document_text_chars"] is None
    assert saved["truncation"] == "TRUNCATED"


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_source",
        "invalid_hash",
        "bool_page",
        "duplicate_page",
        "unknown_size",
        "wrong_version",
        "invalid_normalization",
        "missing_visited",
        "unordered_pages",
    ],
)
def test_pure_validator_rejects_incoherent_metadata_without_accessing_files(tmp_path, monkeypatch, mutation):
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    assert extract(source)["status"] == "extracted"
    saved = receipt(tmp_path, source)
    if mutation == "extra_source":
        saved["source_pdf"]["path"] = "elsewhere"
    elif mutation == "invalid_hash":
        saved["outputs"]["text_sha256"] = "wrong"
    elif mutation == "bool_page":
        saved["document_page_count"] = True
    elif mutation == "duplicate_page":
        saved["retained_page_numbers"] = [1, 1]
    elif mutation == "unknown_size":
        saved["source_pdf"]["bytes"] = None
    elif mutation == "wrong_version":
        saved["schema_version"] = "future-v2"
    elif mutation == "invalid_normalization":
        saved["extractor"]["normalization"] = "different"
    elif mutation == "missing_visited":
        saved["visited_page_numbers"] = []
    else:
        saved["visited_page_numbers"] = [2, 1]
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: pytest.fail("Pure validation must not read files"))
    with pytest.raises(ValueError, match="^extraction_provenance_invalid$"):
        pdf.validate_extraction_provenance(saved)


@pytest.mark.parametrize(
    "suffix", [".txt", ".pages.json", ".extraction.json", ".extraction.pending.json", ".extraction.json.lock"]
)
def test_publication_refuses_simulated_reparse_outputs_before_lock_or_write(tmp_path, monkeypatch, suffix):
    """Exercise the filesystem guard seam, without claiming an OS symlink test."""
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    unsafe = tmp_path / ("paper" + suffix)
    monkeypatch.setattr(pdf, "is_reparse_point", lambda path: path == unsafe, raising=False)
    result = extract(source)
    assert result["status"] == "conflict" and result["reason"] == "extraction_artifact_conflict"
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("suffix", [".txt", ".pages.json", ".extraction.json", ".extraction.pending.json"])
def test_readback_refuses_simulated_reparse_outputs_without_following_them(tmp_path, monkeypatch, suffix):
    source = fake_document(monkeypatch, tmp_path, ["X" * 180])
    assert extract(source)["status"] == "extracted"
    unsafe = tmp_path / ("paper" + suffix)
    monkeypatch.setattr(pdf, "is_reparse_point", lambda path: path == unsafe, raising=False)
    monkeypatch.setattr(
        pdf, "_file_provenance", lambda _: pytest.fail("Readback must reject the reparse guard before hashing files")
    )
    with pytest.raises(ValueError, match="^extraction_provenance_invalid$"):
        receipt(tmp_path, source)
