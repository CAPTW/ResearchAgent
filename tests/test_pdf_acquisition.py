from __future__ import annotations

import json

from app.repositories import runs as run_repository
from app.services import pdf_acquisition
from tests.research_transport_fakes import install_pdf_urlopen_mock


class _FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "application/pdf") -> None:
        self._body = body
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._body


def _patch_run_storage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", tmp_path / "storage" / "runs")


def test_pdf_acquisition_dry_run_plans_without_writing(tmp_path, monkeypatch) -> None:
    _patch_run_storage(tmp_path, monkeypatch)

    result = pdf_acquisition.acquire_pdf(
        paper_id="2605.12345v1",
        pdf_url="https://arxiv.org/pdf/2605.12345v1",
        topic_id="large-language-models",
        run_id="run-1",
        allowed_domains=["arxiv.org"],
        dry_run=True,
    )

    assert result["status"] == "planned"
    assert result["reason"] == "dry_run"
    assert result["source_url"] == "https://arxiv.org/<redacted>"
    assert result["local_pdf_path"] == "run:raw/2605.12345v1.pdf"
    assert not (run_repository.build_run_paths("run-1").raw_dir / "2605.12345v1.pdf").exists()


def test_pdf_acquisition_rejects_non_https_and_disallowed_domain(tmp_path, monkeypatch) -> None:
    _patch_run_storage(tmp_path, monkeypatch)

    non_https = pdf_acquisition.acquire_pdf(
        paper_id="2605.12345v1",
        pdf_url="http://arxiv.org/pdf/2605.12345v1",
        topic_id="large-language-models",
        run_id="run-1",
        allowed_domains=["arxiv.org"],
    )
    off_domain = pdf_acquisition.acquire_pdf(
        paper_id="2605.12345v1",
        pdf_url="https://example.com/2605.12345v1.pdf",
        topic_id="large-language-models",
        run_id="run-1",
        allowed_domains=["arxiv.org"],
    )

    assert non_https["status"] == "rejected"
    assert non_https["reason"] == "non_https_url"
    assert off_domain["status"] == "rejected"
    assert off_domain["reason"] == "domain_not_allowed"


def test_pdf_acquisition_downloads_pdf_with_content_validation(tmp_path, monkeypatch) -> None:
    _patch_run_storage(tmp_path, monkeypatch)

    def fake_urlopen(_request, *, timeout):
        assert timeout == 7
        return _FakeResponse(b"%PDF-1.7\nmock pdf bytes\n%%EOF")

    install_pdf_urlopen_mock(monkeypatch, fake_urlopen)

    result = pdf_acquisition.acquire_pdf(
        paper_id="2605.12345v1",
        pdf_url="https://arxiv.org/pdf/2605.12345v1",
        topic_id="large-language-models",
        run_id="run-1",
        allowed_domains=["arxiv.org"],
        max_bytes=1024,
        timeout_seconds=7,
    )

    pdf_path = run_repository.build_run_paths("run-1").raw_dir / "2605.12345v1.pdf"
    assert result["status"] == "downloaded"
    assert result["bytes_written"] == len(b"%PDF-1.7\nmock pdf bytes\n%%EOF")
    assert result["sha256"]
    assert result["content_type"] == "application/pdf"
    assert pdf_path.exists()
    assert pdf_path.read_bytes().startswith(b"%PDF-1.7")


def test_pdf_acquisition_rejects_oversized_or_non_pdf_response(tmp_path, monkeypatch) -> None:
    _patch_run_storage(tmp_path, monkeypatch)

    def oversized_urlopen(_request, *, timeout):
        return _FakeResponse(b"%PDF-1.7\n" + (b"x" * 32) + b"\n%%EOF")

    install_pdf_urlopen_mock(monkeypatch, oversized_urlopen)
    oversized = pdf_acquisition.acquire_pdf(
        paper_id="2605.12345v1",
        pdf_url="https://arxiv.org/pdf/2605.12345v1",
        topic_id="large-language-models",
        run_id="run-1",
        allowed_domains=["arxiv.org"],
        max_bytes=10,
    )

    def html_urlopen(_request, *, timeout):
        return _FakeResponse(b"<html>not a pdf</html>", content_type="text/html")

    install_pdf_urlopen_mock(monkeypatch, html_urlopen)
    non_pdf = pdf_acquisition.acquire_pdf(
        paper_id="2605.12346v1",
        pdf_url="https://arxiv.org/pdf/2605.12346v1",
        topic_id="large-language-models",
        run_id="run-1",
        allowed_domains=["arxiv.org"],
        max_bytes=1024,
    )

    assert oversized["status"] == "rejected"
    assert oversized["reason"] == "file_too_large"
    assert non_pdf["status"] == "rejected"
    assert non_pdf["reason"] == "not_pdf"


def test_pdf_extraction_writes_text_and_pages_artifacts(tmp_path, monkeypatch) -> None:
    _patch_run_storage(tmp_path, monkeypatch)
    paths = run_repository.ensure_run_layout("run-1")
    pdf_path = paths.raw_dir / "2605.12345v1.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nmock pdf")

    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [
            {
                "page_number": 1,
                "text": "Large language model agents use retrieval augmented generation. "
                "This local extraction has enough deterministic text to support evidence quality checks.",
            },
            {
                "page_number": 2,
                "text": "Evidence traceability improves when citations are grounded in local text. "
                "The extracted page is intentionally long enough for a usable evidence span.",
            },
        ],
    )

    result = pdf_acquisition.extract_pdf_to_text_artifacts(
        pdf_path=pdf_path,
        paper_id="2605.12345v1",
        max_text_chars=200,
        run_id="run-1",
    )

    text_path = paths.raw_dir / "2605.12345v1.txt"
    pages_path = paths.raw_dir / "2605.12345v1.pages.json"
    pages_payload = json.loads(pages_path.read_text(encoding="utf-8"))

    assert result["status"] == "extracted"
    assert result["text_char_count"] > 0
    assert result["page_count"] == 2
    assert text_path.exists()
    assert pages_path.exists()
    assert "retrieval augmented generation" in text_path.read_text(encoding="utf-8")
    assert pages_payload["pages"][0]["page"] == 1
    assert result["quality"]["status"] == "ok"
    assert result["quality"]["character_count"] == result["text_char_count"]
    assert result["quality"]["non_empty_page_count"] == 2
    assert result["quality"]["text_sha256"]
    assert result["quality"]["evidence_span_count"] == 2
    assert result["quality"]["average_evidence_span_length"] >= 80
    assert result["quality"]["extraction_method"] == "mock"
    assert result["pdf_path"] == "run:raw/2605.12345v1.pdf"
    assert result["text_path"] == "run:raw/2605.12345v1.txt"
    assert pages_payload["source_pdf_path"] == "run:raw/2605.12345v1.pdf"


def test_pdf_extraction_can_use_local_pdftotext_fallback(tmp_path, monkeypatch) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nmock pdf")

    class _Completed:
        returncode = 0
        stdout = "Page one retrieval augmented generation.\fPage two local evidence."
        stderr = ""

    monkeypatch.setattr(pdf_acquisition.shutil, "which", lambda name: "pdftotext.exe" if name == "pdftotext" else None)
    monkeypatch.setattr(
        pdf_acquisition.subprocess,
        "run",
        lambda args, **kwargs: _Completed(),
    )

    pages = pdf_acquisition._extract_pdf_pages_with_pdftotext(pdf_path, max_text_chars=200)

    assert [page["page_number"] for page in pages] == [1, 2]
    assert "retrieval augmented generation" in pages[0]["text"]


def test_pdf_extraction_failure_keeps_pdf_available_but_not_reviewed(tmp_path, monkeypatch) -> None:
    _patch_run_storage(tmp_path, monkeypatch)
    paths = run_repository.ensure_run_layout("run-1")
    pdf_path = paths.raw_dir / "2605.12345v1.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nmock pdf")

    monkeypatch.setattr(pdf_acquisition, "_extract_pdf_pages", lambda _path, max_text_chars=24000: [])

    result = pdf_acquisition.extract_pdf_to_text_artifacts(
        pdf_path=pdf_path,
        paper_id="2605.12345v1",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "no_extractable_text"
    assert pdf_path.exists()
    assert not (paths.raw_dir / "2605.12345v1.txt").exists()


def test_pdf_extraction_rejects_too_short_text_with_quality_reason(tmp_path, monkeypatch) -> None:
    _patch_run_storage(tmp_path, monkeypatch)
    paths = run_repository.ensure_run_layout("run-1")
    pdf_path = paths.raw_dir / "2605.12345v1.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\nmock pdf")

    monkeypatch.setattr(
        pdf_acquisition,
        "_extract_pdf_pages",
        lambda _path, max_text_chars=24000: [{"page_number": 1, "text": "Tiny text."}],
    )

    result = pdf_acquisition.extract_pdf_to_text_artifacts(
        pdf_path=pdf_path,
        paper_id="2605.12345v1",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "weak_extracted_text"
    assert result["quality"]["status"] == "failed"
    assert result["quality"]["reason"] == "weak_extracted_text"
    assert result["quality"]["character_count"] < pdf_acquisition.MIN_EXTRACTED_TEXT_CHARS
    assert not (paths.raw_dir / "2605.12345v1.txt").exists()
