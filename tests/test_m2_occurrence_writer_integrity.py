from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from app.repositories import jsonl, runs
from app.repositories.files import read_jsonl, write_jsonl
from app.services import paper_occurrences, workbench


VALID_ID = "occ-" + "a" * 32
OTHER_VALID_ID = "occ-" + "b" * 32


def _record(occurrence_id: object = VALID_ID, *, title: str = "A") -> dict[str, object]:
    return {
        "occurrence_id": occurrence_id,
        "source_provider": "unknown",
        "title": title,
        "authors": ["Researcher"],
        "status": "accepted",
    }


@pytest.mark.parametrize(
    "invalid",
    [
        "banana",
        "occ-café-authority",
        "OCC-0123456789ABCDEF0123456789ABCDEF",
        "occ-0123456789abcdef0123456789abcde",
        "occ-0123456789abcdef0123456789abcdef-extra",
        "",
        "  ",
        "https://example.test/occurrence",
        "folder/occurrence",
        "occ-0123456789abcdef0123456789abcde\n",
    ],
)
def test_supplied_occurrence_id_requires_an_exact_known_grammar(invalid: str) -> None:
    with pytest.raises(paper_occurrences.OccurrenceIdentityInvalid):
        paper_occurrences.normalize_occurrence_id(invalid)


def test_generated_and_legacy_occurrence_id_grammars_are_distinct_and_exact() -> None:
    generated = "occ-0123456789abcdef0123456789abcdef"
    legacy = paper_occurrences.legacy_coordinate_id(
        run_id="legacy-contract-run",
        logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
        physical_line_number=7,
    )

    assert paper_occurrences.normalize_occurrence_id(generated) == generated
    assert legacy.startswith("occ-legacy-")
    assert len(legacy) == len("occ-legacy-") + 32
    assert paper_occurrences.normalize_occurrence_id(legacy) == legacy


def test_duplicate_ids_inside_one_batch_fail_before_destination_mutation(tmp_path: Path) -> None:
    destination = tmp_path / "triage.jsonl"

    with pytest.raises(paper_occurrences.OccurrenceIdentityDuplicate):
        paper_occurrences.write_triage_records(
            destination,
            [_record(title="A"), _record(title="B")],
        )

    assert not destination.exists()


def test_invalid_member_rejects_the_whole_batch_without_mutating_callers_or_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "triage.jsonl"
    valid = _record(None, title="valid")
    invalid = _record("banana", title="invalid")
    original = [dict(valid), dict(invalid)]

    with pytest.raises(paper_occurrences.OccurrenceIdentityInvalid):
        paper_occurrences.write_triage_records(destination, [valid, invalid])

    assert [valid, invalid] == original
    assert not destination.exists()


def test_fresh_restore_preserves_explicit_generated_and_legacy_ids_without_mutating_callers(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "restored-triage.jsonl"
    legacy_id = paper_occurrences.legacy_coordinate_id(
        run_id="restored-run",
        logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
        physical_line_number=3,
    )
    records = [_record(VALID_ID, title="generated"), _record(legacy_id, title="legacy")]
    original = [dict(record) for record in records]

    persisted = paper_occurrences.write_triage_records(destination, records)

    assert records == original
    assert [record["occurrence_id"] for record in persisted] == [VALID_ID, legacy_id]
    assert read_jsonl(destination) == persisted


@pytest.mark.parametrize("same_body", [False, True])
def test_duplicate_id_against_destination_fails_and_preserves_bytes(
    tmp_path: Path,
    same_body: bool,
) -> None:
    destination = tmp_path / "triage.jsonl"
    first = _record(title="A")
    paper_occurrences.write_triage_records(destination, [first])
    before = destination.read_bytes()
    attempted = dict(first) if same_body else _record(title="B")

    with pytest.raises(paper_occurrences.OccurrenceIdentityDuplicate):
        paper_occurrences.write_triage_records(destination, [attempted])

    assert destination.read_bytes() == before
    assert read_jsonl(destination) == [first]


def test_concurrent_duplicate_writers_persist_exactly_one_record(tmp_path: Path) -> None:
    destination = tmp_path / "triage.jsonl"

    def write(title: str) -> str:
        try:
            paper_occurrences.write_triage_records(destination, [_record(title=title)])
        except paper_occurrences.OccurrenceIdentityDuplicate:
            return "duplicate"
        return "persisted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(write, ["A", "B"]))

    assert outcomes == ["duplicate", "persisted"]
    stored = read_jsonl(destination)
    assert len(stored) == 1
    assert stored[0]["occurrence_id"] == VALID_ID


def test_failed_write_leaves_bytes_unchanged_and_retry_reuses_prepared_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "triage.jsonl"
    prepared = paper_occurrences.prepare_for_persistence([_record(None)])
    assigned = prepared[0]["occurrence_id"]
    real_write_all = jsonl._write_all

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected_before_write")

    monkeypatch.setattr(jsonl, "_write_all", fail_write)
    with pytest.raises(jsonl.JsonlAppendBlocked):
        paper_occurrences.write_triage_records(destination, prepared)
    assert not destination.exists()

    monkeypatch.setattr(jsonl, "_write_all", real_write_all)
    persisted = paper_occurrences.write_triage_records(destination, prepared)
    assert persisted[0]["occurrence_id"] == assigned
    assert read_jsonl(destination)[0]["occurrence_id"] == assigned


def test_projection_rejects_corrupt_duplicate_occurrence_ids_before_grouping(tmp_path: Path) -> None:
    run_id = "duplicate-projection-run"
    paths = runs.ensure_run_layout(run_id)
    triage_path = paths.triage_dir / "triage.jsonl"
    write_jsonl(triage_path, [_record(title="A"), _record(title="B")])
    runs.save_run_record(
        {
            "run_id": run_id,
            "topic": "Duplicate projection",
            "status": "completed",
            "request": {"source_provider": "unknown"},
            "artifacts": {"triage_jsonl": str(triage_path)},
        }
    )

    with pytest.raises(paper_occurrences.OccurrenceIdentityDuplicate):
        workbench.load_run_triage_items(run_id)


def test_legacy_physical_line_authority_matrix_freezes_current_scope_rewrite_prohibition() -> None:
    run_id = "legacy-physical-line-matrix-run"
    paths = runs.ensure_run_layout(run_id)
    triage_path = paths.triage_dir / "triage.jsonl"
    records = [_record(None, title="A"), _record(None, title="B")]
    encoded = [
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    runs.save_run_record(
        {
            "run_id": run_id,
            "topic": "Legacy physical line matrix",
            "status": "completed",
            "request": {"source_provider": "unknown"},
            "artifacts": {"triage_jsonl": str(triage_path)},
        }
    )

    def load_ids(payload: bytes) -> dict[str, str]:
        triage_path.write_bytes(payload)
        return {
            str(record["title"]): str(record["occurrence_id"])
            for record in workbench.load_run_triage_items(run_id)
        }

    baseline_lf = f"{encoded[0]}\n\n{encoded[1]}\n".encode()
    baseline = load_ids(baseline_lf)
    assert baseline == {
        "A": paper_occurrences.legacy_coordinate_id(
            run_id=run_id,
            logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
            physical_line_number=1,
        ),
        "B": paper_occurrences.legacy_coordinate_id(
            run_id=run_id,
            logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
            physical_line_number=3,
        ),
    }
    assert load_ids(baseline_lf.replace(b"\n", b"\r\n")) == baseline
    appended = json.dumps(
        _record(None, title="C"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    after_append = load_ids(baseline_lf + (appended + "\n").encode())
    assert after_append["A"] == baseline["A"]
    assert after_append["B"] == baseline["B"]

    normalized_same_lines = (
        json.dumps(records[0], ensure_ascii=False)
        + "\n\n"
        + json.dumps(records[1], ensure_ascii=False)
        + "\n"
    ).encode()
    assert load_ids(normalized_same_lines) == baseline

    reordered = load_ids(f"{encoded[1]}\n\n{encoded[0]}\n".encode())
    compacted = load_ids(f"{encoded[0]}\n{encoded[1]}\n".encode())
    assert reordered["A"] != baseline["A"]
    assert reordered["B"] != baseline["B"]
    assert compacted["B"] != baseline["B"]

    triage_path.write_bytes(f"{encoded[0]}\n{{malformed}}\n{encoded[1]}\n".encode())
    with pytest.raises(jsonl.JsonlCorrupt, match="jsonl_corrupt"):
        workbench.load_run_triage_items(run_id)


def test_mixed_generated_and_legacy_ids_load_but_duplicate_legacy_id_fails_closed() -> None:
    run_id = "legacy-mixed-duplicate-run"
    paths = runs.ensure_run_layout(run_id)
    triage_path = paths.triage_dir / "triage.jsonl"
    legacy_id = paper_occurrences.legacy_coordinate_id(
        run_id=run_id,
        logical_artifact=paper_occurrences.TRIAGE_LOGICAL_ARTIFACT,
        physical_line_number=1,
    )
    runs.save_run_record(
        {
            "run_id": run_id,
            "topic": "Legacy duplicate matrix",
            "status": "completed",
            "request": {"source_provider": "unknown"},
            "artifacts": {"triage_jsonl": str(triage_path)},
        }
    )
    write_jsonl(triage_path, [_record(None, title="legacy"), _record(VALID_ID, title="generated")])
    assert {record["occurrence_id"] for record in workbench.load_run_triage_items(run_id)} == {
        legacy_id,
        VALID_ID,
    }

    write_jsonl(triage_path, [_record(legacy_id, title="A"), _record(legacy_id, title="B")])
    with pytest.raises(paper_occurrences.OccurrenceIdentityDuplicate):
        workbench.load_run_triage_items(run_id)


def test_production_triage_writers_do_not_bypass_the_occurrence_seam() -> None:
    root = Path(__file__).resolve().parents[1]
    modules = {
        "app/services/ranking.py",
        "app/services/evidence_manifest_seed.py",
        "app/services/prototype_real_corpus.py",
    }
    direct_calls: list[str] = []
    seam_calls = 0

    for relative in sorted(modules):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "write_jsonl":
                direct_calls.append(f"{relative}:{node.lineno}")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "paper_occurrences"
                and node.func.attr == "write_triage_records"
            ):
                seam_calls += 1

    assert direct_calls == []
    assert seam_calls == 5
