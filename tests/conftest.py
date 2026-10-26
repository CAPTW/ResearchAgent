from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.config import clear_auth_settings_cache
from app.auth.security import clear_auth_runtime_caches
from app.db import bootstrap
from app.domain import constants
from app.main import create_app
from app.repositories import metadata_index, paper_reviews, runs as run_repository
from app.services import topic_analytics, topic_notifications, topic_profiles, topic_schedules

PRIMARY_RUN_ID = "20260403-100702-m3-validation-run"
COMPARE_RUN_ID = "20260403-141139-m3-validation-run"
SESSION_ID = "20260404-030020-executive-summary"
STAGED_ACTION_ID = "f7fa5260fd04"
HISTORY_ACTION_ID = "f7635e4a1a95"
AUTH_ENV_KEYS = (
    "AUTH_MODE",
    "SESSION_SECRET",
    "APP_BASE_URL",
    "WEB_APP_BASE_PATH",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_AUDIENCE",
    "OIDC_SCOPES",
    "COOKIE_SECURE",
    "COOKIE_SAMESITE",
    "TRUST_PROXY_HEADERS",
    "ALLOWED_ORIGINS",
    "ADMIN_USER_IDS",
    "ADMIN_USER_EMAILS",
    "XRML_ENVIRONMENT",
    "XRML_DEPLOYMENT_PROFILE",
    "XRML_SHARED_DEPLOYMENT",
    "XRML_STRICT_ENV_VALIDATION",
    "XRML_DATA_DIR",
    "XRML_PROVIDER",
    "LOG_FORMAT",
    "LOG_LEVEL",
    "NOTIFICATION_EMAIL_SMTP_HOST",
    "NOTIFICATION_EMAIL_SMTP_PORT",
    "NOTIFICATION_EMAIL_SMTP_TLS",
    "NOTIFICATION_EMAIL_SMTP_SSL",
    "NOTIFICATION_EMAIL_SMTP_USERNAME",
    "NOTIFICATION_EMAIL_SMTP_PASSWORD",
    "NOTIFICATION_EMAIL_FROM",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_FROM",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_TLS",
    "SMTP_SSL",
    "NOTIFICATION_WEBHOOK_ENABLED",
    "NOTIFICATION_WEBHOOK_ALLOWED_HOSTS",
    "NOTIFICATION_WEBHOOK_ALLOWED_PORTS",
    "TOPIC_SCHEDULER_MODE",
    "SCHEDULER_MODE",
    "TOPIC_SCHEDULER_RUNTIME_MODE",
    "TOPIC_SCHEDULER_BACKEND",
    "SCHEDULER_DISTRIBUTED",
    "SCHEDULER_DISTRIBUTED_MODE",
    "TOPIC_SCHEDULER_ENABLED",
    "TOPIC_SCHEDULER_TICK_SECONDS",
    "OPENAI_API_KEY",
    "XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT",
    "XRML_PROTOTYPE_REAL_CORPUS_NETWORK",
    "XRML_GEMINI_ENGINE_PATH",
)
REAL_RUN_FIXTURE_OPT_IN_ENV = "XRML_TEST_USE_REAL_RUN_FIXTURES"


def _restore_env(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _clear_scheduler_backend_test_leakage() -> None:
    leaked_attrs = (
        "configured_runtime_mode",
        "configured_runtime_mode_raw",
        "configured_runtime_mode_valid",
        "configured_runtime_mode_source",
    )
    for backend in (
        topic_schedules.FILE_JSON_SCHEDULER_BACKEND,
        topic_schedules.DB_SCHEDULER_BACKEND,
        topic_schedules.QUEUE_SCHEDULER_BACKEND,
    ):
        attrs = getattr(backend, "__dict__", {})
        for attr in leaked_attrs:
            if attr in attrs:
                delattr(backend, attr)


def reset_runtime_state_for_tests() -> None:
    clear_auth_settings_cache()
    clear_auth_runtime_caches()
    _clear_scheduler_backend_test_leakage()
    topic_schedules.schedule_store_lock_preflight(recover_stale=True)


def _patch_storage_roots(
    monkeypatch: pytest.MonkeyPatch,
    data_root: Path,
    *,
    set_env: bool,
) -> Path:
    storage_root = data_root / "runs"
    if set_env:
        monkeypatch.setenv("XRML_DATA_DIR", str(data_root))
    monkeypatch.setattr(constants, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(run_repository, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_DIR", data_root / "topic_profiles")
    monkeypatch.setattr(topic_profiles, "PROFILE_LIBRARY_PATH", data_root / "topic_profiles" / "profiles.json")
    monkeypatch.setattr(topic_analytics, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_analytics, "ANALYTICS_LIBRARY_DIR", data_root / "topic_analytics")
    monkeypatch.setattr(topic_notifications, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_notifications, "NOTIFICATION_LIBRARY_DIR", data_root / "topic_notifications")
    monkeypatch.setattr(
        topic_notifications,
        "NOTIFICATION_SETTINGS_PATH",
        data_root / "topic_notifications" / "settings.json",
    )
    monkeypatch.setattr(
        topic_notifications,
        "NOTIFICATION_EVENTS_PATH",
        data_root / "topic_notifications" / "notifications.json",
    )
    monkeypatch.setattr(
        topic_notifications,
        "NOTIFICATION_DELIVERIES_PATH",
        data_root / "topic_notifications" / "deliveries.json",
    )
    monkeypatch.setattr(topic_schedules, "STORAGE_ROOT", storage_root)
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_DIR", data_root / "topic_schedules")
    monkeypatch.setattr(topic_schedules, "SCHEDULE_LIBRARY_PATH", data_root / "topic_schedules" / "schedules.json")
    monkeypatch.setattr(topic_schedules, "SCHEDULER_STATUS_PATH", data_root / "topic_schedules" / "worker_status.json")
    return storage_root


KNOWN_EVIDENCE_MANIFESTS = {
    "data/evidence_manifests/m19.3-large-language-models-2604.26498v1.json",
    "data/evidence_manifests/m21-large-language-models-2605.01143v1.json",
    "data/evidence_manifests/m23-large-language-models-2604.14475v1.json",
}

# Timestamps for generated fixtures. Fixed so a fixture built twice is the same
# fixture; see `_write_portable_topic_profiles_fixture`.
FIXTURE_EPOCH = "2026-01-01T00:00:00+00:00"


def _is_git_checkout(root: Path | None = None) -> bool:
    """Whether `root` is inside a working Git checkout.

    A published source archive is a plain directory tree: no `.git`, no index.
    Repository-policy checks that shell out to Git cannot be *evaluated* there,
    which is different from failing them.
    """
    target = Path(root or constants.PROJECT_ROOT)
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _normalized_repo_path(path: str | Path) -> str:
    try:
        resolved = Path(path).resolve()
        return resolved.relative_to(constants.PROJECT_ROOT).as_posix()
    except (OSError, ValueError):
        return Path(path).as_posix().replace("\\", "/")


def _verified_known_manifest(
    path: str | Path,
    *,
    topic_id: str | None = None,
    paper_id: str | None = None,
    strict: bool = False,
    original_verify=None,
) -> dict[str, Any]:
    manifest_path = Path(path)
    normalized = _normalized_repo_path(manifest_path)
    if normalized not in KNOWN_EVIDENCE_MANIFESTS:
        if original_verify is None:
            from app.services import evidence_manifest_verifier

            original_verify = evidence_manifest_verifier.verify_manifest
        return original_verify(path, topic_id=topic_id, paper_id=paper_id, strict=strict)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paper = str(manifest["paper_id"])
    return {
        "status": "verified",
        "paper_id": paper,
        "topic_id": manifest["topic_id"],
        "checks": {
            "passed": [
                "review_row",
                "review_status",
                "review_source_text_sha256",
                "evidence_spans",
                "artifact_ref",
                "artifact_source_text_sha256",
                "pdf_sha256",
                "known_exclusions",
            ],
            "failed": [],
        },
        "local_status": "reviewed",
        "expected_status": (manifest.get("review") or {}).get("expected_status"),
        "local_source_text_sha256": manifest.get("source_text_sha256"),
        "expected_source_text_sha256": manifest.get("source_text_sha256"),
        "local_evidence_span_count": int((manifest.get("review") or {}).get("evidence_span_count") or 1),
        "expected_evidence_span_count": int((manifest.get("review") or {}).get("evidence_span_count") or 1),
        "artifact_ref": manifest.get("artifact_ref"),
        "artifact_resolved": True,
        "pdf_artifact_ref": manifest.get("pdf_artifact_ref"),
        "pdf_artifact_resolved": bool(manifest.get("pdf_artifact_ref")),
        "local_artifact_sha256": manifest.get("source_text_sha256"),
        "local_pdf_sha256": manifest.get("pdf_sha256"),
        "replay_required": False,
        "safe_to_claim_local_verified": True,
        "safe_to_claim_clean_clone_reproducible": False,
        "reasons": [],
        "strict": bool(strict),
    }


def _use_real_repository_fixtures() -> bool:
    """Opt-in to reading the developer's own runtime state instead of fixtures."""
    return os.getenv(REAL_RUN_FIXTURE_OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _write_portable_topic_profiles_fixture(data_root: Path) -> None:
    """Write a deterministic, product-shaped default profile library.

    Built by calling the product's own seeder so the fixture cannot drift away
    from the schema, then pinning the fields that are not stable between calls.

    Those fields are not guessed: running the seeder repeatedly and diffing the
    output shows exactly four varying leaves out of ~300, all `created_at` /
    `updated_at`, because `_seed_default_profiles` reads the clock on each call.
    Left unpinned, two reads of a seeded-but-unstored library differ and any
    equality assertion becomes a coin flip -- which is precisely what happened
    once the tracked `storage/topic_profiles/profiles.json` stopped being
    copied in. `tests/test_source_distribution_self_test.py` re-derives the
    varying set so a newly nondeterministic field fails loudly instead of
    silently reintroducing the flake.
    """
    profiles = [topic_profiles._normalize_profile(p) for p in topic_profiles._seed_default_profiles()]
    for profile in profiles:
        profile["created_at"] = FIXTURE_EPOCH
        profile["updated_at"] = FIXTURE_EPOCH
    _write_fixture_json(data_root / "topic_profiles" / "profiles.json", profiles)


def _write_fixture_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_fixture_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_portable_topic_analytics_fixture(data_root: Path) -> None:
    payload = _portable_topic_analytics_payload()
    # `derivations` and `metricDefinitions` come from the product's own committed
    # constants rather than being hardcoded here. They used to be empty lists,
    # which no real artifact can be: generation always fills them from
    # DERIVATION_DEFINITIONS / METRIC_DEFINITIONS. The suite only tolerated it
    # because the dashboard test happened to select a topic with no persisted
    # artifact, so it exercised the compute path and never read this fixture.
    # Sourcing them from the product also means a newly added surface shows up
    # here automatically instead of silently drifting.
    payload["derivations"] = topic_analytics._derivations(
        str(payload["computedAt"]), list(payload["sourceRuns"])
    )
    payload["metricDefinitions"] = [dict(row) for row in topic_analytics.METRIC_DEFINITIONS]
    # Same story for coverage: a generated snapshot always carries these four
    # rows, so an empty list is a shape no real artifact has. Built through the
    # product's own `_coverage_item` so the key set is whatever the product says
    # it is rather than a copy that can drift.
    total = int(payload["metrics"]["collectedPapers"]["value"])
    reviewed = int(payload["reviewCoverage"]["reviewedCount"])
    payload["coverage"] = [
        topic_analytics._coverage_item("published_metadata", "발행일 메타데이터", covered=total, total=total),
        topic_analytics._coverage_item("citation_metadata", "인용 메타데이터", covered=0, total=total,
                                       empty_reason="Synthetic fixture carries no citation metadata."),
        topic_analytics._coverage_item("keyword_hits", "키워드 히트", covered=reviewed, total=total),
        topic_analytics._coverage_item("institution_metadata", "기관/소속 메타데이터", covered=0, total=total,
                                       empty_reason="Synthetic fixture carries no institution metadata."),
    ]
    _write_fixture_json(data_root / "topic_analytics" / "large-language-models.json", payload)


def _portable_topic_analytics_payload() -> dict[str, Any]:
    return (
        {
            "profileId": "large-language-models",
            "artifactVersion": "topic-analytics-v2",
            "computedAt": "2026-04-04T03:00:20+00:00",
            "generatedAt": "2026-04-04T03:00:20+00:00",
            "lastAnalysisAt": "2026-04-04T03:00:20+00:00",
            "freshness": {
                "status": "fresh",
                "label": "Synthetic clean-worktree analytics fixture.",
                "computedAt": "2026-04-04T03:00:20+00:00",
                "lastAnalysisAt": "2026-04-04T03:00:20+00:00",
                "newestSourceRunAt": "2026-04-04T03:00:20+00:00",
                "sourceRunCount": 2,
                "sourcePaperCount": 524,
                "staleReason": None,
            },
            "sourceRuns": [
                {
                    "runId": PRIMARY_RUN_ID,
                    "status": "completed",
                    "createdAt": "2026-04-04T03:00:20+00:00",
                    "finishedAt": "2026-04-04T03:00:20+00:00",
                },
                {
                    "runId": COMPARE_RUN_ID,
                    "status": "completed",
                    "createdAt": "2026-04-04T03:00:20+00:00",
                    "finishedAt": "2026-04-04T03:00:20+00:00",
                },
            ],
            "metrics": {
                "collectedPapers": {"value": 524, "delta": 0, "sparkline": [524]},
                "corePapers": {"value": 164, "delta": 0, "sparkline": [164]},
                "averageImpact": {"value": 72.0, "delta": 0, "sparkline": [72.0]},
                "totalCitations": {"value": 0, "delta": 0, "sparkline": [0]},
                "trendIndex": {"label": "stable", "deltaPercent": 0, "sparkline": [524]},
            },
            "trends": {"labels": ["2026-04"], "totalPapers": [524], "corePapers": [164], "citations": [0]},
            "hotKeywords": [],
            "latestCorePapers": [],
            "fieldDistribution": [],
            "institutionDistribution": [],
            "coverage": [],
            "metricDefinitions": [],
            "derivations": [],
            "trendDetail": {"bucketMode": "published_month", "summary": "Synthetic clean-worktree analytics fixture.", "points": []},
            "keywordDetails": [],
            "fieldDetails": [],
            "institutionDetails": [],
            "corePaperDetails": [],
            "reviewCoverage": {
                "totalPapers": 524,
                "reviewedCount": 165,
                "abstractOnlyCount": 1,
                "fullTextReviewedCount": 164,
                "coveragePercentage": 31.5,
                "rawIndexedPaperCount": 524,
                "uniqueCandidateCount": 524,
                "topicRelevantUniqueCount": 524,
                "reviewedUniqueCount": 165,
                "abstractOnlyUniqueCount": 1,
                "fullTextAvailableUniqueCount": 164,
                "fullTextReviewedUniqueCount": 164,
            },
            "warnings": [],
        }
    )


def _seed_portable_metadata_index(run_ids: tuple[str, ...] = (PRIMARY_RUN_ID, COMPARE_RUN_ID)) -> None:
    for run_id in run_ids:
        metadata_index.index_run(run_id)
    primary_text = "Synthetic source text for 2601.18372v1. No real runtime artifact data is embedded.\n"
    primary_hash = hashlib.sha256(primary_text.encode("utf-8")).hexdigest()
    paper_reviews.upsert_review(
        {
            "topic_id": "large-language-models",
            "run_id": PRIMARY_RUN_ID,
            "paper_id": "2601.18372v1",
            "title": "Synthetic XR retrieval benchmark for large language models",
            "source_provider": "arxiv",
            "abstract_text": "Synthetic fixture evidence for clean worktree validation.",
            "text_excerpt": primary_text,
            "full_text_status": "reviewed",
            "relevance_score": 88,
            "novelty_score": 65,
            "evidence_strength": 80,
            "method_summary": "Synthetic full-text fixture for provider packet planning.",
            "key_findings": ["Clean worktrees use generated test fixtures."],
            "limitations": ["Synthetic fixture only."],
            "why_it_matters": "Provider safety tests can reject real command profiles without live execution.",
            "topic_fit_rationale": "Large language model retrieval fixture.",
            "review_model": "deterministic-local-full-text-v1",
            "reviewer": "pytest-synthetic-fixture",
            "reviewed_at": "2026-04-04T03:00:20+00:00",
            "source_text_sha256": primary_hash,
            "full_text_artifact_path": "run:raw/2601.18372v1.txt",
            "text_char_count": len(primary_text),
            "review_source_type": "full_text",
            "full_text_reviewed_at": "2026-04-04T03:00:20+00:00",
            "evidence_spans": [
                {
                    "span_id": "synthetic-provider-span-1",
                    "text": primary_text.strip(),
                    "source_path": "run:raw/2601.18372v1.txt",
                    "start_char": 0,
                    "end_char": len(primary_text),
                }
            ],
        }
    )
    paper_reviews.upsert_review(
        {
            "topic_id": "large-language-models",
            "run_id": PRIMARY_RUN_ID,
            "paper_id": "2602.01674v1",
            "title": "Synthetic provider-free evaluation for report workbenches",
            "source_provider": "openalex",
            "abstract_text": "Synthetic fixture evidence for deterministic report checks.",
            "full_text_status": "abstract_only",
            "relevance_score": 62,
            "novelty_score": 50,
            "evidence_strength": 45,
            "method_summary": "Synthetic abstract fixture.",
            "key_findings": ["Provider-free report checks remain deterministic."],
            "limitations": ["No real research provenance."],
            "why_it_matters": "Keeps integration tests portable.",
            "topic_fit_rationale": "Large language model report quality fixture.",
            "review_model": "deterministic-baseline-v1",
            "reviewer": "pytest-synthetic-fixture",
            "reviewed_at": "2026-04-04T03:00:20+00:00",
            "review_source_type": "abstract",
        }
    )


def _synthetic_triage_items(*, compare: bool = False) -> list[dict[str, Any]]:
    suffix = "comparison " if compare else ""
    score_delta = -8 if compare else 0
    return [
        {
            "paper_id": "2601.18372v1",
            "title": f"Synthetic {suffix}XR retrieval benchmark for large language models",
            "abstract": "Synthetic fixture evidence for clean worktree validation. No runtime provenance is embedded.",
            "published": "2026-01-18T00:00:00Z",
            "entry_url": "https://arxiv.org/abs/2601.18372v1",
            "pdf_url": "https://arxiv.org/pdf/2601.18372v1.pdf",
            "score": 72 + score_delta,
            "theme": "Grounded retrieval",
            "source_provider": "arxiv",
            "triage": {
                "hits_primary": ["large language models", "retrieval augmented generation"],
                "hits_secondary": ["xr", "real-time"],
                "hits_negative": [],
            },
            "keyword_hits": {
                "primary": ["large language models", "retrieval augmented generation"],
                "secondary": ["xr", "real-time"],
            },
        },
        {
            "paper_id": "2602.01674v1",
            "title": f"Synthetic {suffix}provider-free evaluation for report workbenches",
            "abstract": "Synthetic fixture evidence for deterministic report checks and local-only validation surfaces.",
            "published": "2026-02-02T00:00:00Z",
            "entry_url": "https://openalex.org/W260201674",
            "pdf_url": "",
            "score": 58 + score_delta,
            "theme": "Report quality",
            "source_provider": "openalex",
            "triage": {
                "hits_primary": ["report", "deterministic"],
                "hits_secondary": ["provider-free"],
                "hits_negative": [],
            },
            "keyword_hits": {
                "primary": ["report", "deterministic"],
                "secondary": ["provider-free"],
            },
        },
    ]


def _synthetic_review_payload(item: dict[str, Any]) -> dict[str, Any]:
    paper_id = str(item["paper_id"])
    return {
        "paper_id": paper_id,
        "title": item["title"],
        "theme": item["theme"],
        "review_ko": {
            "one_liner": f"Synthetic review for {item['title']} validates local test contracts.",
            "why_it_matters": [
                "Clean worktrees can exercise APIs without ignored runtime data.",
                "Provider and OAuth tests stay independent of real storage artifacts.",
            ],
            "key_contributions": [
                "Portable pytest fixture content generated under tmp_path.",
                "Deterministic evidence shape for report and review endpoints.",
            ],
            "method": {
                "problem": "Clean worktrees cannot rely on ignored runtime run directories.",
                "model": "Synthetic fixture generator",
            },
            "experiments": {"key_results": ["Fixture contracts validate without live provider execution."]},
            "xr_takeaways": ["Use generated local fixtures for clean swarm worktrees."],
            "limitations": ["This fixture is synthetic and must not be treated as research evidence."],
            "lab_actions": ["Keep runtime storage out of git.", "Use disposable data roots for validation."],
            "confidence": 0.7,
            "evidence": [
                {
                    "page": 1,
                    "section": "Synthetic Fixture",
                    "note": "Synthetic evidence generated inside pytest tmp_path.",
                    "source_type": "synthetic_fixture",
                    "quality_hint": "test_fixture",
                    "artifact_path": f"run:raw/{paper_id}.txt",
                },
                {
                    "page": 1,
                    "section": "Portability",
                    "note": "The fixture avoids copying ignored storage/runs data.",
                    "source_type": "synthetic_fixture",
                    "quality_hint": "test_fixture",
                    "artifact_path": f"run:raw/{paper_id}.pages.json",
                },
            ],
        },
    }


def _synthetic_outline(run_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    paper_ids = [str(item["paper_id"]) for item in items]
    return {
        "run_id": run_id,
        "path": "run:reports/outline.json",
        "sections": [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "kind": "markdown",
                "body_markdown": "Synthetic local fixture summary for clean worktree validation.",
                "paper_ids": [paper_ids[0]],
                "claims": [
                    {
                        "claim_id": "synthetic-claim-1",
                        "claim_text": "Clean worktrees use generated test fixtures instead of ignored runtime data.",
                        "citations": [
                            {
                                "evidence_id": "synthetic-evidence-1",
                                "source_id": paper_ids[0],
                                "paper_id": paper_ids[0],
                                "artifact_path": f"run:raw/{paper_ids[0]}.txt",
                                "citation_text": f"Synthetic fixture citation [{paper_ids[0]}]",
                                "snippet": "Synthetic evidence generated inside pytest tmp_path.",
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "trend-signals",
                "title": "Trend Signals",
                "kind": "markdown",
                "body_markdown": "",
                "paper_ids": [],
                "claims": [],
            },
            {
                "section_id": "top-papers",
                "title": "Top Papers",
                "kind": "papers",
                "body_markdown": "Synthetic ranked papers for workbench contract checks.",
                "paper_ids": paper_ids,
                "claims": [],
            },
            {
                "section_id": "research-gaps",
                "title": "Research Gaps / Open Questions",
                "kind": "markdown",
                "body_markdown": "Fixture portability should stay narrow and deterministic.",
                "paper_ids": [paper_ids[-1]],
                "claims": [],
            },
            {
                "section_id": "next-experiments",
                "title": "Next Experiments",
                "kind": "markdown",
                "body_markdown": "Rerun focused validation after integration merges.",
                "paper_ids": [],
                "claims": [],
            },
        ],
        "applied_actions": [],
    }


def _synthetic_research_plan(run_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    paper_id = str(items[0]["paper_id"])
    return {
        "run_id": run_id,
        "topic": "large-language-models",
        "generated_at": "2026-04-04T03:00:20+00:00",
        "evidence_count": 1,
        "source_count": len(items),
        "generator": {"mode": "heuristic", "warnings": ["Synthetic pytest fixture; not real research output."]},
        "sections": [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "summary": "Synthetic local fixture summary for clean worktree validation.",
                "paper_ids": [paper_id],
                "claims": [
                    {
                        "claim_id": "synthetic-claim-1",
                        "claim_text": "Clean worktrees use generated test fixtures instead of ignored runtime data.",
                        "importance": "medium",
                        "source_ids": [paper_id],
                        "citations": [
                            {
                                "evidence_id": "synthetic-evidence-1",
                                "source_id": paper_id,
                                "paper_id": paper_id,
                                "artifact_path": f"run:raw/{paper_id}.txt",
                                "citation_text": f"Synthetic fixture citation [{paper_id}]",
                                "snippet": "Synthetic evidence generated inside pytest tmp_path.",
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "top-papers",
                "title": "Top Papers",
                "summary": "Synthetic ranked papers for workbench contract checks.",
                "paper_ids": [str(item["paper_id"]) for item in items],
                "claims": [],
            },
        ],
    }


def _create_synthetic_run_fixture(run_dir: Path, run_id: str, *, compare_run_id: str, compare: bool = False) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for dirname in ("inputs", "raw", "triage", "notes", "reviews", "reports"):
        (run_dir / dirname).mkdir(parents=True, exist_ok=True)

    items = _synthetic_triage_items(compare=compare)
    outline = _synthetic_outline(run_id, items)
    research_plan = _synthetic_research_plan(run_id, items)
    preview = f"# Research Report Preview - {run_id}\n\nSynthetic local-only fixture preview.\n"
    report = f"# Synthetic Report - {run_id}\n\nThis file is generated under pytest tmp_path only.\n"

    _write_fixture_jsonl(run_dir / "triage" / "triage.jsonl", items)
    for item in items:
        paper_id = str(item["paper_id"])
        (run_dir / "raw" / f"{paper_id}.txt").write_text(
            f"Synthetic source text for {paper_id}. No real runtime artifact data is embedded.\n",
            encoding="utf-8",
        )
        _write_fixture_json(run_dir / "raw" / f"{paper_id}.pages.json", {"pages": [{"page": 1, "text": f"Synthetic page text for {paper_id}."}]})
        _write_fixture_json(run_dir / "reviews" / f"{paper_id}.json", _synthetic_review_payload(item))

    _write_fixture_json(run_dir / "reports" / "outline.json", outline)
    (run_dir / "reports" / "preview.md").write_text(preview, encoding="utf-8")
    (run_dir / "reports" / "report.md").write_text(report, encoding="utf-8")
    (run_dir / "reports" / "research_plan.md").write_text(report, encoding="utf-8")
    _write_fixture_json(run_dir / "reports" / "research_plan.json", research_plan)
    (run_dir / "reports" / "exported_preview.md").write_text(preview, encoding="utf-8")
    _write_fixture_json(
        run_dir / "reports" / "exports.json",
        [
            {
                "export_id": "synthetic-report-markdown",
                "format": "report_markdown",
                "status": "ready",
                "output": "run:reports/exported_preview.md",
                "storage_path": "run:reports/exported_preview.md",
                "created_at": "2026-04-04T03:00:20+00:00",
            }
        ],
    )
    _write_fixture_json(run_dir / "reports" / "report_versions.json", [])
    _write_fixture_json(
        run_dir / "inputs" / "profile.json",
        {
            "topic": "Large Language Models",
            "source_provider": "arxiv",
            "source_settings": {"enabledProviders": ["arxiv", "openalex"], "defaultProvider": "arxiv"},
            "run_options": {"top_papers": 5, "weekly_top": 20},
            "sources": {"arxiv": {"enabled": True}, "openalex": {"enabled": True}},
            "keywords": {"primary": ["large language model", "retrieval augmented generation"]},
            "themes": {"Grounded retrieval": ["retrieval augmented generation"]},
        },
    )

    if not compare:
        _write_fixture_json(
            run_dir / "inputs" / "editing_sessions.json",
            [
                {
                    "session_id": SESSION_ID,
                    "run_id": run_id,
                    "title": "Synthetic executive summary session",
                    "workspace_view_id": "report-authoring",
                    "compare_partner_run_id": compare_run_id,
                    "active_compare_view": "report",
                    "compare_mode": "side_by_side",
                    "focus_section_id": "executive-summary",
                    "focus_section_title": "Executive Summary",
                    "preferred_report_side": "both",
                    "staged_action_ids": [STAGED_ACTION_ID],
                    "pending_merge_order": [STAGED_ACTION_ID],
                    "created_at": "2026-04-04T03:00:20+00:00",
                    "updated_at": "2026-04-04T03:00:20+00:00",
                }
            ],
        )
        _write_fixture_json(
            run_dir / "inputs" / "staged_workspace_actions.json",
            [
                {
                    "candidate_id": STAGED_ACTION_ID,
                    "editing_session_id": SESSION_ID,
                    "source_run_id": run_id,
                    "compare_partner_run_id": compare_run_id,
                    "workspace_view_id": "report-authoring",
                    "source_section_id": "executive-summary",
                    "source_section_title": "Executive Summary",
                    "target_section_id": "research-gaps",
                    "target_section_title": "Research Gaps / Open Questions",
                    "target_index": 3,
                    "source_paper_ids": [item["paper_id"] for item in items],
                    "merge_intent": "append_to_target_section",
                    "preview_blocks": [{"block_id": "synthetic-preview-block", "label": "Synthetic fixture patch", "kind": "summary", "content": "Append a synthetic portability note."}],
                    "changed_block_count": 1,
                    "citation_delta": {"left_count": 1, "right_count": 2, "delta": 1, "notes": []},
                    "warnings": [],
                    "conflict_flags": [],
                    "missing_artifacts": [],
                    "destructive": False,
                    "requires_confirmation": False,
                    "summary_lines": ["Synthetic staged action for clean worktree contracts."],
                    "completeness": "full",
                    "request": {
                        "editing_session_id": SESSION_ID,
                        "workspace_view_id": "report-authoring",
                        "action_type": "append_to_target_section",
                        "source_run_id": run_id,
                        "compare_partner_run_id": compare_run_id,
                        "source_section_id": "executive-summary",
                        "source_section_title": "Executive Summary",
                        "target_section_id": "research-gaps",
                        "target_position": "append",
                        "preferred_report_side": "both",
                        "source_paper_ids": [item["paper_id"] for item in items],
                        "payload": {"title": "Synthetic portability note", "body_markdown": "Append a synthetic portability note.", "paper_ids": [item["paper_id"] for item in items]},
                    },
                    "provenance": {"source": "pytest_synthetic_fixture", "action_id": STAGED_ACTION_ID},
                    "created_at": "2026-04-04T03:00:20+00:00",
                    "order": 1,
                }
            ],
        )
        _write_fixture_json(
            run_dir / "reports" / "outline_action_history.json",
            [
                {
                    "action_id": HISTORY_ACTION_ID,
                    "run_id": run_id,
                    "editing_session_id": SESSION_ID,
                    "source_run_id": run_id,
                    "compare_partner_run_id": compare_run_id,
                    "action_type": "append_to_target_section",
                    "summary": "Synthetic history entry for focus restoration.",
                    "timestamp": "2026-04-04T03:00:20+00:00",
                    "created_at": "2026-04-04T03:00:20+00:00",
                    "provenance": {"source": "pytest_synthetic_fixture", "source_run_id": run_id, "compare_partner_run_id": compare_run_id},
                    "route_restore_hints": {"compare_view": "report", "focus_section_id": "executive-summary", "focus_paper_id": items[0]["paper_id"]},
                    "affected_sections": [
                        {
                            "section_id": "executive-summary",
                            "title": "Executive Summary",
                            "before_excerpt": "Synthetic local fixture summary.",
                            "after_excerpt": "Synthetic local fixture summary for clean worktree validation.",
                            "before_paper_ids": [],
                            "after_paper_ids": [items[0]["paper_id"]],
                            "changed_block_count": 1,
                            "completeness": "full",
                            "preview_blocks": [],
                            "related_paper_ids": [items[0]["paper_id"]],
                            "warnings": [],
                        }
                    ],
                },
                {
                    "action_id": "synthetic-history-second",
                    "run_id": run_id,
                    "editing_session_id": SESSION_ID,
                    "source_run_id": run_id,
                    "compare_partner_run_id": compare_run_id,
                    "action_type": "append_to_target_section",
                    "summary": "Second synthetic history entry.",
                    "timestamp": "2026-04-04T03:01:20+00:00",
                    "created_at": "2026-04-04T03:01:20+00:00",
                    "provenance": {"source": "pytest_synthetic_fixture"},
                    "affected_sections": [
                        {
                            "section_id": "research-gaps",
                            "title": "Research Gaps / Open Questions",
                            "before_excerpt": "Fixture portability should stay narrow.",
                            "after_excerpt": "Fixture portability should stay narrow and deterministic.",
                            "before_paper_ids": [],
                            "after_paper_ids": [items[-1]["paper_id"]],
                            "changed_block_count": 1,
                            "completeness": "full",
                            "preview_blocks": [],
                            "related_paper_ids": [items[-1]["paper_id"]],
                            "warnings": [],
                        }
                    ],
                },
            ],
        )

    _write_fixture_json(
        run_dir / "run.json",
        {
            "run_id": run_id,
            "topic": "M3 Validation Run",
            "status": "completed",
            "source": "pytest_synthetic_fixture",
            "write_legacy": False,
            "created_at": "2026-04-04T03:00:20+00:00",
            "updated_at": "2026-04-04T03:00:20+00:00",
            "started_at": "2026-04-04T03:00:20+00:00",
            "finished_at": "2026-04-04T03:00:20+00:00",
            "request": {
                "topic": "M3 Validation Run",
                "kind": "research_report",
                "source_settings": {"enabledProviders": ["arxiv", "openalex"], "defaultProvider": "arxiv"},
            },
            "steps": [
                {"name": "scout", "status": "completed"},
                {"name": "triage", "status": "completed", "output_hint": "run:triage/triage.jsonl"},
                {"name": "summary", "status": "completed"},
                {"name": "synthesis", "status": "completed"},
                {"name": "experiment", "status": "skipped"},
            ],
            "artifacts": {
                "triage_jsonl": "run:triage/triage.jsonl",
                "outline_json": "run:reports/outline.json",
                "preview_markdown": "run:reports/preview.md",
                "report_markdown": "run:reports/report.md",
                "research_plan_json": "run:reports/research_plan.json",
                "research_plan_markdown": "run:reports/research_plan.md",
                "profile": {"profile": str((run_dir / "inputs" / "profile.json").resolve())},
            },
            "counts": {"triage_items": len(items), "reviewed_papers": len(items)},
            "warnings": ["Synthetic pytest fixture; not real research output."],
            "legacy_compatibility": {"authoritative_root": "run:."},
            "compare_ready": {
                "top_papers": [
                    {
                        "paper_id": item["paper_id"],
                        "title": item["title"],
                        "score": item["score"],
                        "theme": item["theme"],
                        "source_provider": item["source_provider"],
                        "keyword_hits": item["keyword_hits"],
                    }
                    for item in items
                ],
                "theme_counts": {item["theme"]: 1 for item in items},
                "outline_path": "run:reports/outline.json",
                "preview_path": "run:reports/preview.md",
                "outline_sections": [
                    {
                        "section_id": section["section_id"],
                        "title": section["title"],
                        "kind": section["kind"],
                        "body_hash": "synthetic",
                        "paper_ids": section.get("paper_ids", []),
                    }
                    for section in outline["sections"]
                ],
            },
            "rerun_context": {
                "compare_baseline_run_id": PRIMARY_RUN_ID if compare else compare_run_id,
                "selected_paper_ids": [item["paper_id"] for item in items],
            },
            "owner_user_id": None,
            "topic_profile_id": "large-language-models",
            "schedule_context": {},
            "profile": {
                "topic_profile_id": "large-language-models",
                "display_name": "Large Language Models",
                "sources": {"arxiv": {"enabled": True}, "openalex": {"enabled": True}},
            },
            "lineage": {
                "parent_run_id": PRIMARY_RUN_ID if compare else None,
                "root_run_id": PRIMARY_RUN_ID,
                "lineage_mode": "rerun_with_profile_overrides" if compare else "standalone",
                "source_paper_ids": [item["paper_id"] for item in items] if compare else [],
                "created_from_profile_snapshot": compare,
                "compare_baseline_run_id": PRIMARY_RUN_ID if compare else None,
                "branch_reason": None,
                "created_at": "2026-04-04T03:00:20+00:00",
                "notes": "Synthetic fixture lineage.",
            },
        },
    )


def _copy_or_create_run_fixture(
    source_runs_root: Path,
    temp_runs_root: Path,
    run_id: str,
    *,
    compare_run_id: str,
    compare: bool = False,
) -> Path:
    source_run_dir = source_runs_root / run_id
    target_run_dir = temp_runs_root / run_id
    use_real_fixture = os.getenv(REAL_RUN_FIXTURE_OPT_IN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    if use_real_fixture and source_run_dir.exists():
        shutil.copytree(source_run_dir, target_run_dir)
    else:
        _create_synthetic_run_fixture(target_run_dir, run_id, compare_run_id=compare_run_id, compare=compare)
    return target_run_dir


@pytest.fixture
def runtime_state_reset():
    return reset_runtime_state_for_tests


@pytest.fixture(autouse=True)
def _isolate_runtime_state_between_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env_snapshot = {key: os.environ.get(key) for key in AUTH_ENV_KEYS}
    _patch_storage_roots(monkeypatch, tmp_path / "pytest_runtime", set_env=False)
    # Redirect operator-audit writes to a per-test temp path so the full suite never appends
    # to the repo-local output/operator-audit/operator-audit.jsonl. Env-based so child
    # subprocesses inherit the isolation; per-test tmp_path keeps counts clean and parallel-safe.
    monkeypatch.setenv("XRML_OPERATOR_AUDIT_PATH", str(tmp_path / "operator-audit" / "operator-audit.jsonl"))
    reset_runtime_state_for_tests()
    try:
        yield
    finally:
        reset_runtime_state_for_tests()
        _restore_env(env_snapshot)


@pytest.fixture(autouse=True)
def _portable_known_evidence_manifest_verification(request, monkeypatch: pytest.MonkeyPatch):
    module_name = getattr(request.module, "__name__", "")
    module_file = Path(str(getattr(request.module, "__file__", ""))).name
    if not (module_name.startswith("tests.test_evidence_manifest_") or module_file.startswith("test_evidence_manifest_")):
        return
    if module_name in {"tests.test_evidence_manifest_verifier", "tests.test_evidence_manifest_seed"} or module_file in {
        "test_evidence_manifest_verifier.py",
        "test_evidence_manifest_seed.py",
    }:
        return

    from app.services import evidence_manifest_admission, evidence_manifest_verifier

    original_verify = evidence_manifest_verifier.verify_manifest
    original_check_batch_admission = evidence_manifest_admission.check_batch_admission

    def verify_manifest(path: str | Path, *, topic_id: str | None = None, paper_id: str | None = None, strict: bool = False) -> dict[str, Any]:
        return _verified_known_manifest(
            path,
            topic_id=topic_id,
            paper_id=paper_id,
            strict=strict,
            original_verify=original_verify,
        )

    def check_batch_admission(manifest_paths, *args, **kwargs):
        kwargs.setdefault("verify_func", verify_manifest)
        every_path_is_a_known_manifest = bool(manifest_paths) and all(
            _normalized_repo_path(p) in KNOWN_EVIDENCE_MANIFESTS for p in manifest_paths
        )
        # Scoped to the repository's own three manifests. Tests that build a
        # synthetic manifest to prove the tracking gate *rejects* must keep
        # seeing a live gate, so relaxing it for every call would silently
        # disarm them -- which it did on the first attempt.
        if every_path_is_a_known_manifest and not _is_git_checkout():
            # Admission proves a manifest is tracked by running `git ls-files
            # --error-unmatch`. A source archive has no Git, so every manifest
            # reads as untracked and the whole registry suite fails for a reason
            # that has nothing to do with the manifests.
            #
            # `require_tracked` is the API's own documented opt-out, so this says
            # "tracking is unknowable here" rather than faking a pass. It is
            # scoped to the no-Git case; inside a checkout the gate still runs.
            # Override rather than setdefault: callers such as
            # `evidence_manifest_registry_generate` pass `require_tracked`
            # explicitly, so a default would never be consulted.
            kwargs["require_tracked"] = False
        return original_check_batch_admission(manifest_paths, *args, **kwargs)

    monkeypatch.setattr(evidence_manifest_admission, "check_batch_admission", check_batch_admission)


@pytest.fixture
def isolated_run_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    source_runs_root = project_root / "storage" / "runs"
    source_storage_root = project_root / "storage"
    temp_storage_root = tmp_path / "storage"
    temp_runs_root = temp_storage_root / "runs"
    temp_runs_root.mkdir(parents=True, exist_ok=True)

    _copy_or_create_run_fixture(source_runs_root, temp_runs_root, PRIMARY_RUN_ID, compare_run_id=COMPARE_RUN_ID)
    _copy_or_create_run_fixture(source_runs_root, temp_runs_root, COMPARE_RUN_ID, compare_run_id=PRIMARY_RUN_ID, compare=True)

    # Copying the developer's accumulated runtime state made the suite depend on
    # whatever their checkout happened to contain -- state deliberately excluded
    # from the published source archive, so the archive could not run its own
    # tests. Generate it instead, behind the same opt-in that already guards the
    # run fixtures.
    if _use_real_repository_fixtures():
        source_metadata_index = source_storage_root / "metadata_index.sqlite3"
        if source_metadata_index.exists():
            shutil.copy2(source_metadata_index, temp_storage_root / "metadata_index.sqlite3")

        for library_name in ("topic_profiles", "topic_analytics", "topic_schedules"):
            source_library = source_storage_root / library_name
            if source_library.exists():
                shutil.copytree(source_library, temp_storage_root / library_name, dirs_exist_ok=True)
    else:
        _write_portable_topic_profiles_fixture(temp_storage_root)
        (temp_storage_root / "topic_schedules").mkdir(parents=True, exist_ok=True)

    _patch_storage_roots(monkeypatch, temp_storage_root, set_env=True)
    _seed_portable_metadata_index()
    _write_portable_topic_analytics_fixture(temp_storage_root)

    return {
        "data_root": temp_storage_root,
        "storage_root": temp_runs_root,
        "metadata_index_path": temp_storage_root / "metadata_index.sqlite3",
        "run_id": PRIMARY_RUN_ID,
        "compare_run_id": COMPARE_RUN_ID,
        "session_id": SESSION_ID,
        "staged_action_id": STAGED_ACTION_ID,
        "history_action_id": HISTORY_ACTION_ID,
        "primary_run_dir": temp_runs_root / PRIMARY_RUN_ID,
        "compare_run_dir": temp_runs_root / COMPARE_RUN_ID,
    }


@pytest.fixture
def app_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _factory(**env: str) -> Any:
        explicit_data_dir = "XRML_DATA_DIR" in env
        requested_data_dir = str(
            env["XRML_DATA_DIR"] if explicit_data_dir else os.environ.get("XRML_DATA_DIR", "")
        ).strip()
        data_root = Path(requested_data_dir).expanduser().resolve() if requested_data_dir else tmp_path / "app_factory_storage"
        for key in AUTH_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("TOPIC_SCHEDULER_ENABLED", "0")
        _patch_storage_roots(monkeypatch, data_root, set_env=bool(requested_data_dir))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        clear_auth_settings_cache()
        clear_auth_runtime_caches()
        return create_app()

    return _factory


@pytest.fixture
def client(isolated_run_repo: dict[str, Any], app_factory) -> TestClient:
    with TestClient(app_factory()) as test_client:
        yield test_client
