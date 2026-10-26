from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services import profile as profile_service
from app.services import retrieval, source_config, workbench


def _crossref_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "ok", "message": {"items": items}}


def test_crossref_results_normalize_real_metadata_and_provenance() -> None:
    entries = retrieval.parse_crossref_results(
        _crossref_payload(
            [
                {
                    "DOI": "10.5555/XRML.2026.001",
                    "URL": "https://doi.org/10.5555/XRML.2026.001",
                    "title": ["Transparent XR Research Agents"],
                    "abstract": "<jats:p>Agent workbench evidence for XR research.</jats:p>",
                    "author": [
                        {"given": "Ada", "family": "Lovelace", "affiliation": [{"name": "Analytical Engines Lab"}]},
                        {"name": "Grace Hopper", "affiliation": []},
                    ],
                    "container-title": ["Journal of Research Ops"],
                    "subject": ["Computer Science", "Human-computer interaction"],
                    "published-print": {"date-parts": [[2026, 4, 20]]},
                    "deposited": {"date-time": "2026-04-21T01:02:03Z"},
                    "is-referenced-by-count": 42,
                    "link": [{"URL": "https://example.test/paper.pdf", "content-type": "application/pdf"}],
                }
            ]
        ),
        fetched_at="2026-04-30T00:00:00+00:00",
        query="xr research agent",
        profile={"max_results": 5, "sort_by": "relevance", "sort_order": "descending"},
    )

    assert len(entries) == 1
    paper = entries[0]
    assert paper["source_provider"] == "crossref"
    assert paper["paper_id"] == "crossref:10.5555/xrml.2026.001"
    assert paper["provider_record_id"] == "10.5555/XRML.2026.001"
    assert paper["doi"] == "10.5555/XRML.2026.001"
    assert paper["entry_url"] == "https://doi.org/10.5555/XRML.2026.001"
    assert paper["pdf_url"] == "https://example.test/paper.pdf"
    assert paper["title"] == "Transparent XR Research Agents"
    assert paper["abstract"] == "Agent workbench evidence for XR research."
    assert paper["authors"] == [
        {"name": "Ada Lovelace", "institutions": ["Analytical Engines Lab"]},
        {"name": "Grace Hopper", "institutions": []},
    ]
    assert paper["institutions"] == ["Analytical Engines Lab"]
    assert paper["categories"] == ["Computer Science", "Human-computer interaction", "Journal of Research Ops"]
    assert paper["published"] == "2026-04-20"
    assert paper["updated"] == "2026-04-21T01:02:03Z"
    assert paper["citations"] == 42
    assert paper["fetched_at"] == "2026-04-30T00:00:00+00:00"
    assert paper["query"] == "xr research agent"
    assert paper["query_profile"]["max_results"] == 5
    assert paper["raw_metadata_ref"] == "crossref:10.5555/XRML.2026.001"
    assert paper["crossref"]["is_referenced_by_count"] == 42


def test_crossref_results_keep_missing_metadata_safe_empty() -> None:
    entries = retrieval.parse_crossref_results(
        _crossref_payload([{"DOI": "10.5555/MISSING", "title": ["Sparse metadata paper"]}]),
        fetched_at="2026-04-30T00:00:00+00:00",
        query="sparse metadata",
        profile={},
    )

    paper = entries[0]
    assert paper["source_provider"] == "crossref"
    assert paper["paper_id"] == "crossref:10.5555/missing"
    assert paper["authors"] == []
    assert paper["institutions"] == []
    assert paper["abstract"] == ""
    assert paper["published"] == ""
    assert paper["updated"] == ""
    assert paper["citations"] == 0
    assert paper["entry_url"] == "https://doi.org/10.5555/MISSING"


def test_crossref_retrieval_records_provider_failure_without_blocking_other_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_atom(url: str, timeout: int = 30) -> str:
        return """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/2604.00001v1</id>
            <title>XR agent paper</title>
            <summary>xr agent abstract</summary>
            <published>2026-04-01T00:00:00Z</published>
            <updated>2026-04-02T00:00:00Z</updated>
            <author><name>A Researcher</name></author>
            <category term="cs.AI" />
            <link rel="alternate" href="https://arxiv.org/abs/2604.00001v1" />
          </entry>
        </feed>"""

    def fake_fetch_json(url: str, timeout: int = 30) -> dict[str, Any]:
        raise RuntimeError("Crossref rate limited")

    monkeypatch.setattr(retrieval, "fetch_atom", fake_fetch_atom)
    monkeypatch.setattr(retrieval, "fetch_json", fake_fetch_json)

    result = retrieval.retrieve_from_profile(
        profile={
            "topic": "XR",
            "sources": {
                "arxiv": {"queries": ["xr agent"], "categories": ["cs.AI"], "max_results": 1, "priority": 1},
                "crossref": {"queries": ["xr agent"], "max_results": 1, "priority": 2},
            },
            "keywords": {"primary": ["xr"], "secondary": ["agent"]},
        },
        raw_path=tmp_path / "raw.jsonl",
        inbox_path=tmp_path / "inbox.jsonl",
        sleep_seconds=0,
    )

    assert result["status"] == "partial"
    assert result["new_papers"] == 1
    assert result["providers_run"] == {"arxiv": 1}
    assert result["failed_queries"] == [
        {
            "provider": "crossref",
            "query": "<redacted>",
            "error_code": "retrieval_upstream_error",
            "error": "Retrieval upstream failed safely.",
        }
    ]


def test_crossref_source_settings_propagate_into_run_profile() -> None:
    settings = source_config.normalize_source_settings(
        {
            "defaultProvider": "crossref",
            "enabledProviders": ["crossref"],
            "providers": {
                "crossref": {
                    "enabled": True,
                    "priority": 1,
                    "queries": ["research operations"],
                    "maxResults": 7,
                    "sortBy": "published",
                    "sortOrder": "ascending",
                }
            },
        }
    )

    assert settings["enabledProviders"] == ["crossref"]
    assert settings["defaultProvider"] == "crossref"

    run_profile = profile_service.build_run_profile(
        {
            "topic": "Research Ops",
            "queries": ["fallback"],
            "categories": [],
            "source_provider": "crossref",
            "source_settings": settings,
            "max_results": 7,
        }
    )

    assert set(run_profile["sources"]) == {"crossref"}
    assert run_profile["sources"]["crossref"] == {
        "queries": ["research operations"],
        "max_results": 7,
        "sort_by": "published",
        "sort_order": "ascending",
        "priority": 1,
    }


def test_crossref_dedup_uses_doi_cluster_without_corrupting_provider_ids() -> None:
    items = [
        {"paper_id": "2604.00001v1", "title": "Same DOI", "doi": "10.5555/example", "source_provider": "arxiv"},
        {"paper_id": "crossref:10.5555/example", "title": "Same DOI", "doi": "10.5555/EXAMPLE", "source_provider": "crossref"},
    ]

    clusters = workbench._build_cluster_map(items)

    assert clusters["doi:10.5555/example"] == ["2604.00001v1", "crossref:10.5555/example"]
