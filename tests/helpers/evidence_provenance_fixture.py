from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


FIXTURE_KIND = "evidence_provenance_real_backend"
FIXTURE_SCHEMA_VERSION = "evidence-provenance-real-backend-fixture-v1"
FIXTURE_ID = "evidence-provenance-real-backend-v1"
CREATED_BY = "scripts/test_evidence_provenance_real_backend.py"
FIXED_TIMESTAMP = "2026-01-15T12:00:00+00:00"
RUN_ID = "evidence-browser-real-backend-run"
PAPER_ID = "2601.00001v1"
CANONICAL_PAPER_ID = "2601.00001"
PAPER_TITLE = "Synthetic Spatial Computing Provenance"
TOPIC_ID = "spatial-computing"
TOPIC_NAME = "Spatial Computing"
REPORT_SECTION_ID = "spatial-computing-evidence-findings"
REPORT_SECTION_TITLE = "Spatial Computing Evidence Findings"
PORTABLE_ARTIFACT_REF = f"run:raw/{PAPER_ID}.txt"
ARTIFACT_BYTES = b"Synthetic local evidence artifact for spatial computing provenance.\n"

TABLES = (
    "runs",
    "run_topics",
    "papers",
    "paper_reviews",
    "evidence",
    "report_sections",
    "index_state",
)
HASHED_FIXTURE_FILES = (
    "fixture-sentinel.json",
    "topic_profiles/profiles.json",
    f"runs/{RUN_ID}/run.json",
    f"runs/{RUN_ID}/triage/triage.jsonl",
    f"runs/{RUN_ID}/reviews/{PAPER_ID}.json",
    f"runs/{RUN_ID}/raw/{PAPER_ID}.txt",
    f"runs/{RUN_ID}/reports/research_plan.json",
)
EXPECTED_TREE_FILES = frozenset((*HASHED_FIXTURE_FILES, "fixture-manifest.json", "metadata_index.sqlite3"))


@dataclass(frozen=True)
class FileDigest:
    path: str
    sha256: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> FileDigest:
        return cls(path=str(payload["path"]), sha256=str(payload["sha256"]))


@dataclass(frozen=True)
class TableCount:
    table: str
    count: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TableCount:
        return cls(table=str(payload["table"]), count=int(payload["count"]))


@dataclass(frozen=True)
class SeedManifest:
    schemaVersion: str
    fixtureKind: str
    fixtureId: str
    synthetic: bool
    topicId: str
    runId: str
    paperId: str
    canonicalPaperId: str
    reportSectionId: str
    resultId: str
    logicalDbFingerprint: str
    logicalCorpusFingerprint: str
    files: tuple[FileDigest, ...]
    tableCounts: tuple[TableCount, ...]

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> SeedManifest:
        raw_files = payload.get("files")
        raw_counts = payload.get("tableCounts")
        if not isinstance(raw_files, list) or not isinstance(raw_counts, list):
            raise ValueError("fixture manifest files/tableCounts must be lists")
        return cls(
            schemaVersion=str(payload["schemaVersion"]),
            fixtureKind=str(payload["fixtureKind"]),
            fixtureId=str(payload["fixtureId"]),
            synthetic=bool(payload["synthetic"]),
            topicId=str(payload["topicId"]),
            runId=str(payload["runId"]),
            paperId=str(payload["paperId"]),
            canonicalPaperId=str(payload["canonicalPaperId"]),
            reportSectionId=str(payload["reportSectionId"]),
            resultId=str(payload["resultId"]),
            logicalDbFingerprint=str(payload["logicalDbFingerprint"]),
            logicalCorpusFingerprint=str(payload["logicalCorpusFingerprint"]),
            files=tuple(FileDigest.from_payload(item) for item in raw_files if isinstance(item, dict)),
            tableCounts=tuple(TableCount.from_payload(item) for item in raw_counts if isinstance(item, dict)),
        )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_safe_fixture_root(root: Path, *, require_empty: bool) -> Path:
    resolved = root.expanduser().resolve()
    repo = _repo_root()
    temp_root = Path(tempfile.gettempdir()).resolve()
    home = Path.home().resolve()
    drive_root = Path(resolved.anchor).resolve()

    if resolved in {repo, repo.parent, temp_root, home, drive_root}:
        raise ValueError(f"unsafe fixture root: {resolved}")
    if not _is_relative_to(resolved, temp_root):
        raise ValueError(f"fixture root must be under the OS temp directory: {resolved}")
    if _is_relative_to(resolved, repo) or _is_relative_to(repo, resolved):
        raise ValueError(f"fixture root overlaps the repository: {resolved}")
    if require_empty and resolved.exists() and any(resolved.iterdir()):
        raise ValueError(f"fixture root must be empty before seeding: {resolved}")
    return resolved


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sentinel_payload() -> dict[str, object]:
    return {
        "schemaVersion": FIXTURE_SCHEMA_VERSION,
        "synthetic": True,
        "fixtureKind": FIXTURE_KIND,
        "fixtureId": FIXTURE_ID,
        "expectedRunId": RUN_ID,
        "expectedPaperId": PAPER_ID,
        "createdBy": CREATED_BY,
    }


def _sanitize_process_environment(root: Path) -> None:
    forbidden_prefixes = (
        "OPENAI_",
        "ANTHROPIC_",
        "GOOGLE_",
        "GEMINI_",
        "OIDC_",
        "MCP_",
        "CODEX_",
        "XRML_PUBLIC_MCP_",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    )
    forbidden_exact = {
        "SESSION_SECRET",
        "ALLOW_REAL_PROVIDER_SMOKE",
        "ALLOW_LIVE_OAUTH_SMOKE",
        "XRML_TEST_USE_REAL_RUN_FIXTURES",
        "XRML_PROVIDER_LIVE_EXECUTION_APPROVED",
        "XRML_PROVIDER_LIVE_OUTPUT_CLASS",
        "XRML_PROVIDER_LIVE_PROOF_STATUS",
        "XRML_PROVIDER_LIVE_PROOF_ARTIFACT",
    }
    for name in tuple(os.environ):
        upper = name.upper()
        if upper in forbidden_exact or upper.startswith(forbidden_prefixes):
            os.environ.pop(name, None)

    os.environ.update(
        {
            "AUTH_MODE": "disabled",
            "APP_BASE_URL": "http://127.0.0.1:8002",
            "WEB_APP_BASE_PATH": "/app",
            "XRML_DATA_DIR": str(root),
            "XRML_OPERATOR_AUDIT_PATH": str(root / "operator-audit" / "operator-audit.jsonl"),
            "XRML_ENVIRONMENT": "local",
            "XRML_DEPLOYMENT_PROFILE": "single-service",
            "XRML_STRICT_ENV_VALIDATION": "false",
            "TRUST_PROXY_HEADERS": "false",
            "TOPIC_SCHEDULER_MODE": "disabled",
            "TOPIC_SCHEDULER_ENABLED": "0",
            "SCHEDULER_MODE": "single_node_file_backed",
            "XRML_ALLOW_PROVIDER_RESEARCH_OUTPUT": "0",
            "ALLOW_REAL_PROVIDER_SMOKE": "0",
            "ALLOW_LIVE_OAUTH_SMOKE": "0",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )


def _write_fixture_files(root: Path) -> None:
    run_root = root / "runs" / RUN_ID
    raw_path = run_root / "raw" / f"{PAPER_ID}.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(ARTIFACT_BYTES)

    _write_json(
        root / "topic_profiles" / "profiles.json",
        [
            {
                "profile_id": TOPIC_ID,
                "title": TOPIC_NAME,
                "description": "Synthetic local-only topic for real-backend browser validation.",
                "topic": TOPIC_NAME,
                "queries": ["spatial computing"],
                "categories": ["cs.CV"],
                "max_results": 1,
                "source_provider": "arxiv",
                "top_papers": 1,
                "weekly_top": 1,
                "is_favorite": False,
                "status": "active",
                "archived_at": None,
                "created_at": FIXED_TIMESTAMP,
                "updated_at": FIXED_TIMESTAMP,
            }
        ],
    )

    _write_json(
        run_root / "run.json",
        {
            "run_id": RUN_ID,
            "topic": TOPIC_NAME,
            "topic_profile_id": TOPIC_ID,
            "status": "completed",
            "source": "synthetic_test_fixture",
            "write_legacy": False,
            "created_at": FIXED_TIMESTAMP,
            "updated_at": FIXED_TIMESTAMP,
            "started_at": FIXED_TIMESTAMP,
            "finished_at": FIXED_TIMESTAMP,
            "owner_user_id": "local-internal-user",
            "owner_assigned_at": FIXED_TIMESTAMP,
            "owner_assigned_by": "synthetic_test_fixture",
            "ownership_source": "test_fixture",
            "steps": [],
            "counts": {"triage_items": 1},
            "warnings": ["Synthetic local-only fixture; not scholarly evidence."],
            "artifacts": {
                "triage_jsonl": "run:triage/triage.jsonl",
                "research_plan_json": "run:reports/research_plan.json",
            },
            "lineage": {
                "parent_run_id": None,
                "root_run_id": RUN_ID,
                "lineage_mode": "standalone",
                "source_paper_ids": [],
                "created_from_profile_snapshot": True,
                "compare_baseline_run_id": None,
                "branch_reason": None,
                "created_at": FIXED_TIMESTAMP,
                "notes": "Synthetic fixture lineage.",
            },
            "compare_ready": {},
            "rerun_context": {},
            "schedule_context": {},
        },
    )

    triage_row = {
        "paper_id": PAPER_ID,
        "title": PAPER_TITLE,
        "abstract": "Synthetic spatial computing metadata used only for a local integration test.",
        "theme": "spatial computing",
        "score": 97,
        "status": "reviewed",
        "published": "2026-01-01",
        "dedup": {"cluster_id": "synthetic-spatial-computing", "is_primary": True},
    }
    _write_jsonl(run_root / "triage" / "triage.jsonl", triage_row)

    evidence_note = "Synthetic spatial computing evidence remains inside the disposable local fixture."
    _write_json(
        run_root / "reviews" / f"{PAPER_ID}.json",
        {
            "paper_id": PAPER_ID,
            "title": PAPER_TITLE,
            "theme": "spatial computing",
            "read_level": "SYNTHETIC_FULL_TEXT",
            "review_ko": {
                "one_liner": "Synthetic spatial computing provenance is deterministic and local-only.",
                "why_it_matters": ["It exercises persisted provenance.", "It avoids real scholarly data."],
                "key_contributions": ["Deterministic test metadata.", "Portable local artifact reference."],
                "method": {
                    "problem": "Validate browser-to-backend evidence provenance without external data.",
                    "model": "Deterministic local fixture",
                    "inputs_outputs": "Synthetic metadata to read-only evidence responses.",
                    "training": "None.",
                    "inference": "None.",
                    "system_constraints": ["Loopback only."],
                },
                "experiments": {"key_results": ["All fixture identities remain deterministic."]},
                "xr_takeaways": ["Synthetic provenance only."],
                "limitations": ["This is not scholarly or production evidence."],
                "lab_actions": ["Run the local browser test.", "Keep runtime paths in OS temp."],
                "confidence": 0.9,
                "evidence": [
                    {
                        "page": 1,
                        "section": "Synthetic Fixture",
                        "reference": f"{PAPER_ID}:span-1",
                        "note": evidence_note,
                        "summary": "Bounded synthetic evidence span.",
                        "source_type": "synthetic_fixture",
                        "quality_hint": "test_fixture",
                        "confidence": 0.9,
                        "artifact_path": PORTABLE_ARTIFACT_REF,
                    }
                ],
            },
            "generated_at_utc": FIXED_TIMESTAMP,
        },
    )

    _write_json(
        run_root / "reports" / "research_plan.json",
        {
            "run_id": RUN_ID,
            "topic": TOPIC_NAME,
            "generated_at": FIXED_TIMESTAMP,
            "evidence_count": 1,
            "source_count": 1,
            "generator": {
                "mode": "heuristic",
                "provider_name": None,
                "model": None,
                "response_id": None,
                "warnings": ["Synthetic local-only fixture; no report generation occurred."],
            },
            "sections": [
                {
                    "section_id": REPORT_SECTION_ID,
                    "title": REPORT_SECTION_TITLE,
                    "summary": "Standalone synthetic report-section projection.",
                    "paper_ids": [PAPER_ID],
                    "claims": [
                        {
                            "claim_id": "synthetic-spatial-claim",
                            "claim_text": "The fixture exposes one bounded local evidence item.",
                            "importance": "medium",
                            "source_ids": [PAPER_ID],
                            "citations": [
                                {
                                    "evidence_id": f"{PAPER_ID}-evidence-1",
                                    "source_id": PAPER_ID,
                                    "paper_id": PAPER_ID,
                                    "artifact_path": PORTABLE_ARTIFACT_REF,
                                    "citation_text": f"{PAPER_TITLE} [{PAPER_ID}]",
                                    "snippet": evidence_note,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )


def _normalize_db_value(value: object, root: Path) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.replace(str(root), "<FIXTURE_ROOT>")
    normalized = normalized.replace(root.as_posix(), "<FIXTURE_ROOT>")
    return normalized.replace("\\", "/")


def _logical_database_payload(root: Path) -> dict[str, list[dict[str, object]]]:
    db_path = root / "metadata_index.sqlite3"
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        payload: dict[str, list[dict[str, object]]] = {}
        for table in TABLES:
            rows = [
                {key: _normalize_db_value(row[key], root) for key in row.keys()}
                for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
            ]
            payload[table] = sorted(rows, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
        return payload
    finally:
        connection.close()


def _read_manifest(path: Path) -> SeedManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("fixture manifest must be a JSON object")
    return SeedManifest.from_payload(payload)


def seed_evidence_provenance_overlay(root: Path) -> SeedManifest:
    resolved = _assert_safe_fixture_root(root, require_empty=True)
    resolved.mkdir(parents=True, exist_ok=True)
    _write_json(resolved / "fixture-sentinel.json", _sentinel_payload())

    _sanitize_process_environment(resolved)
    repo = _repo_root()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    loaded_constants = sys.modules.get("app.domain.constants")
    if loaded_constants is not None:
        loaded_root = Path(getattr(loaded_constants, "STORAGE_ROOT")).resolve().parent
        if loaded_root != resolved:
            raise RuntimeError(f"app runtime modules were imported before fixture isolation: {loaded_root}")

    _write_fixture_files(resolved)

    from app.repositories import metadata_index, paper_reviews
    from app.services.evidence_search import encode_result_id

    index_result = metadata_index.index_run(RUN_ID)
    if index_result != {
        "run_id": RUN_ID,
        "papers_indexed": 1,
        "evidence_indexed": 1,
        "report_sections_indexed": 1,
    }:
        raise AssertionError(f"unexpected production index result: {index_result}")

    artifact_hash = hashlib.sha256(ARTIFACT_BYTES).hexdigest()
    paper_reviews.upsert_review(
        {
            "topic_id": TOPIC_ID,
            "run_id": RUN_ID,
            "paper_id": PAPER_ID,
            "title": PAPER_TITLE,
            "source_provider": "synthetic_fixture",
            "authors": [],
            "published_at": "2026-01-01",
            "abstract_text": "Synthetic local-only abstract metadata.",
            "text_excerpt": "Synthetic spatial computing evidence span.",
            "full_text_status": "reviewed",
            "relevance_score": 0.9,
            "novelty_score": 0.8,
            "evidence_strength": 0.9,
            "method_summary": "Deterministic local fixture method.",
            "key_findings": ["The browser reads persisted synthetic provenance."],
            "limitations": ["Not scholarly evidence and not production proof."],
            "why_it_matters": "It validates the real local read-only service path.",
            "topic_fit_rationale": "The fixture topic exactly matches Spatial Computing.",
            "review_model": "deterministic-local-fixture-v1",
            "reviewer": "synthetic-fixture",
            "reviewed_at": FIXED_TIMESTAMP,
            "source_text_sha256": artifact_hash,
            "full_text_artifact_path": PORTABLE_ARTIFACT_REF,
            "text_char_count": len(ARTIFACT_BYTES),
            "evidence_spans": [
                {
                    "evidence_span_id": f"{PAPER_ID}:span-1",
                    "text": "Synthetic spatial computing evidence span.",
                    "source_path": PORTABLE_ARTIFACT_REF,
                }
            ],
            "review_source_type": "full_text",
            "full_text_reviewed_at": FIXED_TIMESTAMP,
        }
    )
    metadata_index.touch_index_state("last_indexed_at", FIXED_TIMESTAMP)
    metadata_index.touch_index_state("last_indexed_run_id", RUN_ID)

    logical_payload = _logical_database_payload(resolved)
    logical_db_fingerprint = _fingerprint(logical_payload)
    table_counts = tuple(TableCount(table=table, count=len(logical_payload[table])) for table in TABLES)
    file_digests = tuple(
        FileDigest(path=relative, sha256=_sha256(resolved / relative)) for relative in HASHED_FIXTURE_FILES
    )
    result_id = encode_result_id("paper", CANONICAL_PAPER_ID)
    logical_corpus_fingerprint = _fingerprint(
        {
            "topicId": TOPIC_ID,
            "runId": RUN_ID,
            "paperId": PAPER_ID,
            "canonicalPaperId": CANONICAL_PAPER_ID,
            "reportSectionId": REPORT_SECTION_ID,
            "resultId": result_id,
            "logicalDbFingerprint": logical_db_fingerprint,
            "files": [asdict(item) for item in file_digests],
            "tableCounts": [asdict(item) for item in table_counts],
        }
    )
    manifest = SeedManifest(
        schemaVersion=FIXTURE_SCHEMA_VERSION,
        fixtureKind=FIXTURE_KIND,
        fixtureId=FIXTURE_ID,
        synthetic=True,
        topicId=TOPIC_ID,
        runId=RUN_ID,
        paperId=PAPER_ID,
        canonicalPaperId=CANONICAL_PAPER_ID,
        reportSectionId=REPORT_SECTION_ID,
        resultId=result_id,
        logicalDbFingerprint=logical_db_fingerprint,
        logicalCorpusFingerprint=logical_corpus_fingerprint,
        files=file_digests,
        tableCounts=table_counts,
    )
    _write_json(resolved / "fixture-manifest.json", manifest.to_payload())
    return manifest


def verify_evidence_provenance_overlay(root: Path, manifest: SeedManifest) -> None:
    resolved = _assert_safe_fixture_root(root, require_empty=False)
    sentinel_path = resolved / "fixture-sentinel.json"
    if json.loads(sentinel_path.read_text(encoding="utf-8")) != _sentinel_payload():
        raise AssertionError("fixture sentinel mismatch")
    if _read_manifest(resolved / "fixture-manifest.json") != manifest:
        raise AssertionError("fixture manifest file does not match the supplied typed manifest")

    actual_files = {
        path.relative_to(resolved).as_posix() for path in resolved.rglob("*") if path.is_file()
    }
    if actual_files != EXPECTED_TREE_FILES:
        raise AssertionError(
            f"fixture tree mismatch: missing={sorted(EXPECTED_TREE_FILES - actual_files)} "
            f"unexpected={sorted(actual_files - EXPECTED_TREE_FILES)}"
        )

    for item in manifest.files:
        relative = Path(item.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise AssertionError(f"unsafe manifest relative path: {item.path}")
        if _sha256(resolved / relative) != item.sha256:
            raise AssertionError(f"fixture file hash mismatch: {item.path}")

    logical_payload = _logical_database_payload(resolved)
    if _fingerprint(logical_payload) != manifest.logicalDbFingerprint:
        raise AssertionError("logical database fingerprint mismatch")
    actual_counts = tuple(TableCount(table=table, count=len(logical_payload[table])) for table in TABLES)
    if actual_counts != manifest.tableCounts:
        raise AssertionError(f"fixture table count mismatch: {actual_counts}")
    expected_counts = {
        "runs": 1,
        "run_topics": 1,
        "papers": 1,
        "paper_reviews": 1,
        "evidence": 1,
        "report_sections": 1,
        "index_state": 2,
    }
    if {item.table: item.count for item in actual_counts} != expected_counts:
        raise AssertionError(f"unexpected logical table counts: {actual_counts}")

    manifest_text = json.dumps(manifest.to_payload(), sort_keys=True)
    forbidden_fragments = (str(_repo_root()), str(resolved), "C:\\Users\\", "/home/", "D:\\dev\\repos\\")
    if any(fragment and fragment in manifest_text for fragment in forbidden_fragments):
        raise AssertionError("absolute repository or fixture path leaked into manifest")
    if re.search(r"(?:BEGIN [A-Z ]+PRIVATE KEY|Bearer\s+[A-Za-z0-9._-]+)", manifest_text, re.IGNORECASE):
        raise AssertionError("secret-like text leaked into manifest")

    for suffix in ("-journal", "-wal", "-shm"):
        if Path(f"{resolved / 'metadata_index.sqlite3'}{suffix}").exists():
            raise AssertionError(f"SQLite sidecar persisted: {suffix}")


def _safe_subprocess_environment() -> dict[str, str]:
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LOCALAPPDATA",
        "APPDATA",
        "LANG",
        "LC_ALL",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1,localhost"})
    return environment


def _git_status(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo,
        env=_safe_subprocess_environment(),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.splitlines()


def _runtime_guard_snapshot(repo: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "runtime_data_guard.py"), "--repo-root", str(repo)],
        cwd=repo,
        env=_safe_subprocess_environment(),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("runtime guard did not return a JSON object")
    return payload


def _seed_in_subprocess(root: Path) -> SeedManifest:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--root", str(root), "--json"],
        cwd=_repo_root(),
        env=_safe_subprocess_environment(),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        stderr_tail = "\n".join(result.stderr.splitlines()[-20:])
        raise RuntimeError(f"fixture seed subprocess failed ({result.returncode}):\n{stderr_tail}")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise AssertionError("fixture seed subprocess did not return a JSON object")
    return SeedManifest.from_payload(payload)


def _remove_owned_fixture_root(root: Path) -> None:
    resolved = _assert_safe_fixture_root(root, require_empty=False)
    sentinel = resolved / "fixture-sentinel.json"
    if not sentinel.is_file() or json.loads(sentinel.read_text(encoding="utf-8")) != _sentinel_payload():
        raise RuntimeError(f"refusing to remove fixture root without the expected sentinel: {resolved}")
    shutil.rmtree(resolved)
    if resolved.exists():
        raise RuntimeError(f"fixture root cleanup failed: {resolved}")


def _self_test() -> dict[str, object]:
    repo = _repo_root()
    status_before = _git_status(repo)
    runtime_before = _runtime_guard_snapshot(repo)
    roots = [
        Path(tempfile.mkdtemp(prefix="xrml-evidence-fixture-a-")),
        Path(tempfile.mkdtemp(prefix="xrml-evidence-fixture-b-")),
    ]
    manifests: list[SeedManifest] = []
    cleanup_errors: list[str] = []
    try:
        for root in roots:
            manifest = _seed_in_subprocess(root)
            verify_evidence_provenance_overlay(root, manifest)
            manifests.append(manifest)
        if manifests[0] != manifests[1]:
            raise AssertionError("independent fixture manifests are not logically deterministic")
        if _git_status(repo) != status_before:
            raise AssertionError("fixture self-test changed repository status")
        if _runtime_guard_snapshot(repo) != runtime_before:
            raise AssertionError("fixture self-test changed protected runtime artifacts")
    finally:
        for root in roots:
            if not root.exists():
                continue
            try:
                _remove_owned_fixture_root(root)
            except Exception as exc:  # pragma: no cover - cleanup failure must surface verbatim
                cleanup_errors.append(str(exc))
    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))
    return {
        "status": "passed",
        "fixtureKind": FIXTURE_KIND,
        "logicalCorpusFingerprint": manifests[0].logicalCorpusFingerprint,
        "tableCounts": [asdict(item) for item in manifests[0].tableCounts],
        "overlaysSeeded": 2,
        "overlaysCleaned": 2,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed and verify a disposable evidence provenance runtime overlay.")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        print(json.dumps(_self_test(), sort_keys=True))
        return 0
    if args.root is None:
        parser.error("--root is required unless --self-test is used")
    manifest = seed_evidence_provenance_overlay(args.root)
    verify_evidence_provenance_overlay(args.root, manifest)
    if args.json:
        print(json.dumps(manifest.to_payload(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
