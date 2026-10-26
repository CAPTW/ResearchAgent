from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.jobs import streams
from app.cli import review_refresh
from app.repositories import jsonl as jsonl_repository
from app.repositories.file_locks import file_lock
from app.repositories.jsonl import (
    JsonlAppendBlocked,
    JsonlCorrupt,
    JsonlEncodingInvalid,
    JsonlIntegrityError,
    JsonlLineTooLarge,
    JsonlPolicy,
    JsonlRecordInvalid,
    JsonlSchemaInvalid,
    JsonlStateBusy,
    JsonlTruncatedTail,
    append_jsonl_record,
    inspect_jsonl,
    read_jsonl_strict,
    replace_jsonl,
)
from app.services import (
    experiments,
    notes,
    oauth_account_links,
    oauth_dry_run,
    operator_audit,
    ranking,
    reviews,
    runs,
    synthesis,
)
from app.services.research_domain import (
    DomainContextSource,
    build_research_domain_context,
)


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _valid_line(index: int = 1) -> bytes:
    return json.dumps(
        {"id": index, "text": f"record-{index}"},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def test_strict_read_missing_is_empty_and_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "records.jsonl"

    assert read_jsonl_strict(path) == []
    assert not path.exists()
    assert not path.parent.exists()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", []),
        (_valid_line() + b"\n", [{"id": 1, "text": "record-1"}]),
        (
            _valid_line(1) + b"\n" + _valid_line(2) + b"\n",
            [
                {"id": 1, "text": "record-1"},
                {"id": 2, "text": "record-2"},
            ],
        ),
        (_valid_line(), [{"id": 1, "text": "record-1"}]),
        (
            '{"id":1,"text":"한글 😀"}\n'.encode(),
            [{"id": 1, "text": "한글 😀"}],
        ),
        (
            b"\n  \r\n" + _valid_line() + b"\n\t\n",
            [{"id": 1, "text": "record-1"}],
        ),
    ],
)
def test_strict_read_preserves_tracked_empty_blank_and_final_newline_semantics(
    tmp_path: Path,
    payload: bytes,
    expected: list[dict],
) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, payload)

    assert read_jsonl_strict(path) == expected
    assert path.read_bytes() == payload


@pytest.mark.parametrize(
    ("payload", "error_type", "code", "line_number"),
    [
        (b"{broken}\n" + _valid_line() + b"\n", JsonlCorrupt, "jsonl_corrupt", 1),
        (
            _valid_line() + b"\n{broken}\n" + _valid_line(2) + b"\n",
            JsonlCorrupt,
            "jsonl_corrupt",
            2,
        ),
        (_valid_line() + b"\n{broken}\n", JsonlCorrupt, "jsonl_corrupt", 2),
        (
            _valid_line() + b'\n{"id":2',
            JsonlTruncatedTail,
            "jsonl_truncated_tail",
            2,
        ),
        (b'{"id":1,"text":"\xff"}\n', JsonlEncodingInvalid, "jsonl_encoding_invalid", 1),
        (b"\xef\xbb\xbf" + _valid_line() + b"\n", JsonlEncodingInvalid, "jsonl_encoding_invalid", 1),
        (b"[1,2,3]\n", JsonlRecordInvalid, "jsonl_record_invalid", 1),
        (b'{"id":NaN}\n', JsonlCorrupt, "jsonl_corrupt", 1),
        (b'{"id":Infinity}\n', JsonlCorrupt, "jsonl_corrupt", 1),
    ],
)
def test_strict_read_distinguishes_corruption_without_returning_prefix(
    tmp_path: Path,
    payload: bytes,
    error_type: type[JsonlIntegrityError],
    code: str,
    line_number: int,
) -> None:
    path = tmp_path / "sensitive-absolute-path-records.jsonl"
    _write_bytes(path, payload)
    before = path.read_bytes()

    with pytest.raises(error_type) as exc_info:
        read_jsonl_strict(path)

    exc = exc_info.value
    assert exc.code == code
    assert exc.line_number == line_number
    assert str(exc) == code
    rendered = repr(exc)
    assert str(path) not in rendered
    assert "broken" not in rendered
    assert path.read_bytes() == before

    # The strict reader closed its handle even on the first corrupt record.
    renamed = path.with_name("renamed.jsonl")
    path.rename(renamed)
    assert renamed.read_bytes() == before


def test_strict_read_distinguishes_domain_schema_failure(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, b'{"id":0}\n')
    policy = JsonlPolicy(
        artifact_role="positive_ids",
        record_validator=lambda record: record["id"] > 0,
    )

    with pytest.raises(JsonlSchemaInvalid) as exc_info:
        read_jsonl_strict(path, policy=policy)

    assert exc_info.value.code == "jsonl_schema_invalid"
    assert exc_info.value.line_number == 1


def test_strict_read_rejects_oversized_line_without_unbounded_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, b'{"value":"' + (b"x" * 40) + b'"}\n')

    with pytest.raises(JsonlLineTooLarge) as exc_info:
        read_jsonl_strict(
            path,
            policy=JsonlPolicy(max_line_bytes=16),
        )

    assert exc_info.value.code == "jsonl_line_too_large"


def test_observational_inspection_is_explicit_bounded_and_not_list_like(
    tmp_path: Path,
) -> None:
    secret = "raw-secret-record-must-not-escape"
    path = tmp_path / "records.jsonl"
    _write_bytes(
        path,
        _valid_line(1)
        + b"\n"
        + f'{{"{secret}":\n'.encode()
        + _valid_line(2)
        + b"\n",
    )

    result = inspect_jsonl(path)

    assert result.complete is False
    assert result.total_line_count == 3
    assert result.valid_record_count == 2
    assert result.issue_count == 1
    assert result.first_issue_code == "jsonl_corrupt"
    assert result.first_issue_line_number == 2
    assert result.truncated is False
    assert result.source_digest
    assert [record["id"] for record in result.records] == [1]
    assert secret not in repr(result)
    assert str(path) not in repr(result)
    with pytest.raises(TypeError):
        iter(result)


def test_observational_issue_list_is_bounded_but_full_count_is_retained(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, b"{bad}\n{bad}\n{bad}\n{bad}\n")

    result = inspect_jsonl(
        path,
        policy=JsonlPolicy(max_issues=2),
    )

    assert result.complete is False
    assert result.issue_count == 4
    assert len(result.issues) == 2
    assert result.first_issue_line_number == 1


def test_strict_read_does_not_fallback_to_observational_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, _valid_line() + b"\n")
    monkeypatch.setattr(
        jsonl_repository,
        "inspect_jsonl",
        lambda *_args, **_kwargs: pytest.fail("strict read must not inspect/fallback"),
    )

    assert read_jsonl_strict(path) == [{"id": 1, "text": "record-1"}]


def test_observational_missing_read_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "records.jsonl"

    result = inspect_jsonl(path)

    assert result.complete is True
    assert result.records == ()
    assert result.source_digest is None
    assert not path.parent.exists()


def test_observational_valid_file_is_complete(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, _valid_line(1) + b"\n" + _valid_line(2))

    result = inspect_jsonl(path)

    assert result.complete is True
    assert result.valid_record_count == 2
    assert len(result.records) == 2
    assert result.issue_count == 0
    assert result.first_issue_code is None


def test_first_append_is_canonical_single_line_durable_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "new" / "records.jsonl"
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(jsonl_repository.os, "fsync", recording_fsync)
    append_jsonl_record(
        path,
        {"z": "line one\nline two", "a": "한글"},
    )

    payload = path.read_bytes()
    assert payload == '{"a":"한글","z":"line one\\nline two"}\n'.encode()
    assert fsync_calls
    assert read_jsonl_strict(path) == [
        {"a": "한글", "z": "line one\nline two"}
    ]
    assert path.with_name(".records.jsonl.lock").exists()


def test_append_after_complete_final_record_without_newline_adds_separator(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, b'{"id":1}')

    append_jsonl_record(path, {"id": 2})

    assert path.read_bytes() == b'{"id":1}\n{"id":2}\n'
    assert read_jsonl_strict(path) == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize(
    ("payload", "cause_code"),
    [
        (_valid_line() + b"\n{broken}\n", "jsonl_corrupt"),
        (_valid_line() + b'\n{"id":2', "jsonl_truncated_tail"),
        (b'{"text":"\xff"}\n', "jsonl_encoding_invalid"),
    ],
)
def test_append_blocks_corrupt_existing_source_without_byte_change(
    tmp_path: Path,
    payload: bytes,
    cause_code: str,
) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, payload)
    before = path.read_bytes()

    with pytest.raises(JsonlAppendBlocked) as exc_info:
        append_jsonl_record(path, {"id": 3})

    assert exc_info.value.code == "jsonl_append_blocked"
    assert exc_info.value.cause_code == cause_code
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*repair*"))
    assert not list(tmp_path.glob("*quarantine*"))


@pytest.mark.parametrize(
    "record",
    [
        {"id": float("nan")},
        {"id": float("inf")},
        {"value": object()},
    ],
)
def test_append_rejects_non_standard_or_unserializable_record(
    tmp_path: Path,
    record: dict,
) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, _valid_line() + b"\n")
    before = path.read_bytes()

    with pytest.raises(JsonlAppendBlocked) as exc_info:
        append_jsonl_record(path, record)

    assert exc_info.value.cause_code == "jsonl_record_invalid"
    assert path.read_bytes() == before


def test_append_domain_validator_failure_releases_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    policy = JsonlPolicy(
        record_validator=lambda record: bool(record.get("accepted")),
    )

    with pytest.raises(JsonlAppendBlocked) as exc_info:
        append_jsonl_record(path, {"accepted": False}, policy=policy)
    assert exc_info.value.cause_code == "jsonl_schema_invalid"

    append_jsonl_record(path, {"accepted": True}, policy=policy)
    assert read_jsonl_strict(path, policy=policy) == [{"accepted": True}]


def test_active_lock_times_out_without_changing_source(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    _write_bytes(path, _valid_line() + b"\n")
    before = path.read_bytes()
    lock_path = path.with_name(".records.jsonl.lock")

    with file_lock(lock_path):
        with pytest.raises(JsonlStateBusy) as exc_info:
            append_jsonl_record(
                path,
                {"id": 2},
                lock_timeout_seconds=0.02,
            )

    assert exc_info.value.code == "jsonl_state_busy"
    assert path.read_bytes() == before


def test_reparse_target_is_rejected_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "linked" / "records.jsonl"
    path.parent.mkdir()
    monkeypatch.setattr(
        jsonl_repository,
        "is_reparse_point",
        lambda candidate: Path(candidate) == path.parent,
    )

    with pytest.raises(JsonlAppendBlocked) as exc_info:
        append_jsonl_record(path, {"id": 1})

    assert exc_info.value.cause_code == "reparse_point"
    assert not path.exists()


def test_replace_is_atomic_and_refuses_corruption_compaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    replace_jsonl(path, [{"id": 1}, {"id": 2}])
    assert path.read_bytes() == b'{"id":1}\n{"id":2}\n'

    _write_bytes(path, path.read_bytes() + b"{broken}\n")
    before = path.read_bytes()
    with pytest.raises(JsonlAppendBlocked) as exc_info:
        replace_jsonl(path, [{"id": 3}])

    assert exc_info.value.cause_code == "jsonl_corrupt"
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.jsonl.tmp"))


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_twenty_process_append_is_serialized_without_loss_or_interleave(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    code = (
        "import sys;"
        "from pathlib import Path;"
        "from app.repositories.jsonl import append_jsonl_record;"
        "append_jsonl_record(Path(sys.argv[1]), {'id': int(sys.argv[2])})"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(path), str(index)],
            cwd=_repo_root(),
            env=_subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(20)
    ]
    failures: list[tuple[int, str]] = []
    for process in processes:
        _stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            failures.append((process.returncode or -1, stderr))

    assert failures == []
    records = read_jsonl_strict(path)
    assert len(records) == 20
    assert {record["id"] for record in records} == set(range(20))
    assert len(path.read_bytes().splitlines()) == 20


def _start_lock_holder(lock_path: Path) -> subprocess.Popen[str]:
    code = (
        "import sys,time;"
        "from pathlib import Path;"
        "from app.repositories.file_locks import file_lock;"
        "p=Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);"
        "\nwith file_lock(p):\n print('locked',flush=True)\n time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path)],
        cwd=_repo_root(),
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    return process


def test_process_death_releases_lock_and_stale_marker_is_not_ownership(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.jsonl"
    lock_path = path.with_name(".records.jsonl.lock")
    process = _start_lock_holder(lock_path)
    try:
        with pytest.raises(JsonlStateBusy):
            append_jsonl_record(
                path,
                {"id": 1},
                lock_timeout_seconds=0.02,
            )
    finally:
        process.kill()
        process.wait(timeout=10)

    # The marker remains, but the dead process's OS lock does not.
    assert lock_path.exists()
    append_jsonl_record(path, {"id": 2}, lock_timeout_seconds=1.0)
    assert read_jsonl_strict(path) == [{"id": 2}]


def test_authoritative_consumers_fail_before_output_or_fallback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jsonl"
    _write_bytes(source, _valid_line() + b"\n{broken}\n")
    keywords = tmp_path / "keywords.json"
    themes = tmp_path / "themes.json"
    keywords.write_text('{"primary":[],"secondary":[]}', encoding="utf-8")
    themes.write_text("{}", encoding="utf-8")
    context = build_research_domain_context(
        source=DomainContextSource.LEGACY_NEUTRAL,
    )

    rank_jsonl = tmp_path / "ranked.jsonl"
    rank_markdown = tmp_path / "ranked.md"
    with pytest.raises(JsonlCorrupt):
        ranking.rank_from_paths(
            input_path=source,
            keywords_path=keywords,
            themes_path=themes,
            output_jsonl=rank_jsonl,
            output_md=rank_markdown,
        )
    assert not rank_jsonl.exists()
    assert not rank_markdown.exists()

    report = tmp_path / "report.md"
    with pytest.raises(JsonlCorrupt):
        synthesis.synthesize_weekly(
            triage_path=source,
            out_path=report,
            top_n=5,
            domain_context=context,
        )
    assert not report.exists()

    template = tmp_path / "template.md"
    template.write_text("{title}", encoding="utf-8")
    valid_fallback = tmp_path / "fallback.jsonl"
    _write_bytes(valid_fallback, _valid_line(2) + b"\n")
    notes_dir = tmp_path / "notes"
    with pytest.raises(JsonlCorrupt):
        notes.summarize_from_paths(
            triage_path=source,
            inbox_path=valid_fallback,
            template_path=template,
            out_dir=notes_dir,
            top_n=1,
            domain_context=context,
        )
    assert not notes_dir.exists()

    pdf_dir = tmp_path / "pdfs"
    with pytest.raises(JsonlCorrupt):
        reviews.fetch_pdfs(
            triage_path=source,
            out_dir=pdf_dir,
            top_n=1,
        )
    assert not pdf_dir.exists()

    experiment_root = tmp_path / "experiment"
    with pytest.raises(experiments.ExperimentScaffoldError) as exc_info:
        experiments.create_experiment_scaffold(
            source_path=source,
            rank=1,
            out_root=experiment_root,
            domain_context=context,
        )
    assert exc_info.value.code == "experiment_scaffold_invalid"
    assert not experiment_root.exists()


def test_manual_jsonl_import_cli_returns_stable_nonzero_without_partial_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "manual-provider-output.jsonl"
    _write_bytes(path, _valid_line() + b"\n{raw-provider-secret}\n")
    monkeypatch.setattr(
        "app.services.ai_provider.ai_review.build_ai_review_packets",
        lambda *_args, **_kwargs: {"packets": []},
    )

    exit_code = review_refresh.main(
        [
            "--topic",
            "synthetic-topic",
            "--import-provider-drafts",
            str(path),
            "--validate-ai-review-drafts",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 2
    assert json.loads(output) == {
        "status": "failed",
        "error": "jsonl_corrupt",
    }
    assert "raw-provider-secret" not in output
    assert str(path) not in output


def test_run_authoritative_check_does_not_treat_corruption_as_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "run" / "raw" / "inbox.jsonl"
    _write_bytes(inbox, _valid_line() + b"\n{broken}\n")
    monkeypatch.setattr(
        runs,
        "build_run_paths",
        lambda _run_id: SimpleNamespace(raw_dir=inbox.parent),
    )

    with pytest.raises(JsonlCorrupt):
        runs._run_inbox_has_items("synthetic-run")


def test_run_pipeline_records_redacted_failure_and_never_completes_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / "run" / "raw" / "inbox.jsonl"
    _write_bytes(
        inbox,
        _valid_line() + b"\n{paper-secret-must-not-persist}\n",
    )
    source_before = inbox.read_bytes()
    paths = SimpleNamespace(raw_dir=inbox.parent)
    state = {
        "run_id": "synthetic-run",
        "profile": {},
        "request": {"skip_scout": True},
        "warnings": [],
    }
    saved: list[dict] = []
    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(runs, "_mark_run_started", lambda _run_id: state)
    monkeypatch.setattr(runs, "build_run_paths", lambda _run_id: paths)
    monkeypatch.setattr(
        runs,
        "load_run_record",
        lambda _run_id: state,
    )
    monkeypatch.setattr(
        runs,
        "save_run_record",
        lambda run: saved.append(dict(run)) or run,
    )
    monkeypatch.setattr(
        runs,
        "_append_event",
        lambda _run_id, event_type, payload: events.append(
            (event_type, dict(payload))
        ),
    )

    runs._execute_pipeline("synthetic-run")

    assert state["status"] == "failed"
    assert state["error"] == "jsonl_corrupt"
    assert saved[-1]["status"] == "failed"
    rendered = json.dumps({"state": state, "events": events})
    assert "paper-secret-must-not-persist" not in rendered
    assert str(inbox) not in rendered
    assert "Traceback" not in rendered
    assert events == [
        ("step.failed", {"error": "jsonl_corrupt", "complete": False})
    ]
    assert inbox.read_bytes() == source_before


def test_operator_audit_summary_is_strict_but_replay_is_explicit_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "operator-audit.jsonl"
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", path)
    operator_audit.record_event(
        action_type="view_dashboard",
        decision="allowed",
        timestamp="2026-07-27T00:00:00+00:00",
    )
    with path.open("ab") as handle:
        handle.write(b"{raw-secret-malformed}\n")
    before = path.read_bytes()

    with pytest.raises(JsonlCorrupt):
        operator_audit.summarize_events()
    replay = operator_audit.validate_audit_log()

    assert replay["auditLogStatus"] == "errors"
    assert replay["validEventCount"] == 1
    assert replay["invalidEventCount"] == 1
    assert replay["reasonCounts"]["jsonl_corrupt"] == 1
    assert "raw-secret-malformed" not in json.dumps(replay)
    with pytest.raises(JsonlAppendBlocked):
        operator_audit.record_event(
            action_type="view_dashboard",
            decision="allowed",
        )
    assert path.read_bytes() == before


def test_oauth_authoritative_state_and_link_reads_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "oauth-state.jsonl"
    link_path = tmp_path / "account-links.jsonl"
    monkeypatch.setattr(oauth_dry_run, "OAUTH_STATE_PATH", state_path)
    monkeypatch.setattr(
        oauth_account_links,
        "ACCOUNT_LINK_STORE_PATH",
        link_path,
    )
    _write_bytes(state_path, b"{secret-state}\n")
    _write_bytes(link_path, b"{secret-link}\n")
    state_before = state_path.read_bytes()

    with pytest.raises(JsonlCorrupt):
        oauth_dry_run.build_oauth_dry_run_status()
    with pytest.raises(JsonlCorrupt):
        oauth_account_links.list_account_links()
    health = oauth_account_links.build_account_link_health_summary()
    assert health["healthStatus"] == "errors"
    assert health["invalidLinkCount"] == 1
    assert "jsonl_corrupt" in health["reasonCodes"]
    assert "secret-link" not in json.dumps(health)

    with pytest.raises(JsonlAppendBlocked):
        oauth_dry_run._append_state(
            {
                "providerId": "provider",
                "state": "state",
                "expiresAt": "2026-07-27T01:00:00Z",
                "usedAt": None,
                "tokenExchangePerformed": False,
            }
        )
    assert state_path.read_bytes() == state_before


def test_sse_observation_exposes_degraded_integrity_without_raw_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    event = {
        "run_id": "run-1",
        "type": "step.completed",
        "timestamp": "2026-07-27T00:00:00+00:00",
        "payload": {"step": "triage"},
    }
    _write_bytes(
        path,
        json.dumps(event).encode()
        + b"\n{raw-secret-event}\n",
    )
    monkeypatch.setattr(
        streams,
        "build_run_paths",
        lambda _run_id: SimpleNamespace(events_path=path),
    )
    monkeypatch.setattr(
        streams,
        "load_run_record",
        lambda _run_id: {"status": "completed"},
    )

    async def collect() -> list[str]:
        iterator = streams.iter_sse_events("run-1")
        chunks = [await anext(iterator), await anext(iterator)]
        await iterator.aclose()
        return chunks

    chunks = asyncio.run(collect())

    assert chunks[0].startswith("data: ")
    assert chunks[1].startswith("event: integrity")
    assert '"complete":false' in chunks[1]
    assert '"degraded":true' in chunks[1]
    assert "jsonl_corrupt" in chunks[1]
    assert "raw-secret-event" not in "".join(chunks)
    assert str(path) not in "".join(chunks)


def test_jsonl_api_error_envelope_is_stable_and_redacted() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()

    @app.get("/_test/jsonl-corrupt")
    def raise_corruption() -> None:
        raise JsonlCorrupt(line_number=7)

    response = TestClient(app).get("/_test/jsonl-corrupt")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "jsonl_corrupt",
            "complete": False,
            "retryable": False,
            "lineNumber": 7,
        }
    }


def test_corrupt_jsonl_get_is_no_write_and_returns_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    path = tmp_path / "operator-audit.jsonl"
    _write_bytes(path, b"{raw-audit-secret}\n")
    monkeypatch.setattr(operator_audit, "AUDIT_LOG_PATH", path)
    before = {
        child.name: (child.stat().st_size, child.stat().st_mtime_ns)
        for child in tmp_path.iterdir()
    }

    response = TestClient(create_app()).get("/settings/status")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "jsonl_corrupt"
    assert response.json()["detail"]["complete"] is False
    assert "raw-audit-secret" not in response.text
    assert str(path) not in response.text
    after = {
        child.name: (child.stat().st_size, child.stat().st_mtime_ns)
        for child in tmp_path.iterdir()
    }
    assert after == before
    assert not path.with_name(".operator-audit.jsonl.lock").exists()


def test_production_jsonl_surfaces_have_no_direct_append_or_skip_parser() -> None:
    root = _repo_root()
    relative_paths = (
        "app/repositories/files.py",
        "app/services/experiments.py",
        "app/services/oauth_account_links.py",
        "app/services/oauth_dry_run.py",
        "app/services/operator_audit.py",
        "app/jobs/streams.py",
    )
    forbidden = (
        re.compile(r"\.open\(\s*[\"']a"),
        re.compile(r"json\.loads\(\s*line\s*\)"),
        re.compile(r"read_text\([^\n]*\)\.splitlines\(\)"),
    )

    for relative_path in relative_paths:
        source = (root / relative_path).read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern.search(source) is None, (
                relative_path,
                pattern.pattern,
            )


def test_tracked_jsonl_files_are_readable_by_the_strict_reader() -> None:
    """Every JSONL file committed to the repository must parse.

    `data/queues/inbox.jsonl` shipped with one unparseable orphan line: a record
    whose head had been lost, leaving a fragment that began mid-sentence. The
    strict reader fails closed by design, so the first research run on any clean
    checkout aborted with `jsonl_corrupt` and no report was produced. Nothing
    caught it because the suite reads isolated fixtures rather than the files the
    repository actually ships.
    """
    from app.repositories import jsonl as jsonl_repo

    repo_root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.jsonl"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode == 0:
        paths = [repo_root / name for name in tracked.stdout.split("\0") if name.strip()]
        assert paths, "expected the repository to track at least one .jsonl file"
    else:
        # A published source archive is not a Git checkout, so `git ls-files`
        # cannot enumerate anything. Every shipped `.jsonl` is on disk though,
        # and the point of this test is that the files the project ships are
        # readable -- so walk them directly rather than losing the coverage.
        skip = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache"}
        paths = [
            path
            for path in repo_root.rglob("*.jsonl")
            if not skip.intersection(path.relative_to(repo_root).parts)
        ]
        # No minimum here. The repository tracks `.jsonl` under `data/queues` and
        # `data/papers/raw`, both of which the package boundary excludes, so a
        # source archive legitimately ships none. The invariant being tested is
        # "nothing shipped is corrupt", which an empty set satisfies; demanding
        # a file would be asserting a property of the checkout, not the artifact.

    corrupt: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        report = jsonl_repo.inspect_jsonl(path)
        if report.first_issue_code:
            corrupt.append(
                f"{path.relative_to(repo_root).as_posix()}: "
                f"{report.first_issue_code} at line {report.first_issue_line_number}"
            )

    assert not corrupt, "tracked JSONL files must parse: " + "; ".join(corrupt)
