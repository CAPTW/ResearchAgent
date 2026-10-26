from __future__ import annotations

import json
from pathlib import Path

from app.services import prototype_live_provider
from app.services.codex_cli_output_schema import build_codex_cli_report_draft_schema
from app.services.codex_cli_report_draft import (
    LAUNCHER_NODE,
    REASON_CLI_UNAVAILABLE,
    REASON_EXIT_NONZERO,
    REASON_USAGE_LIMIT,
    REASON_FINAL_OUTPUT_MISSING,
    REASON_OUTPUT_PARSE_FAILED,
    REASON_TIMEOUT,
    CliExecResult,
    bounded_excerpt,
    build_codex_cli_exec_argv,
    extract_final_candidate_json,
    extract_usage_limit_reset_time_text,
    human_reason_message,
    redact_cli_text,
    resolve_official_launcher,
    run_codex_cli_report_draft_once,
)


ROUTE = {
    "provider_id": "codex",
    "execution_surface": "codex_sdk_python",
    "stage": "report_draft_assist",
    "internal_output_class": "report_draft",
    "product_output_class": "weekly_report",
}


def _candidate(run_id: str = "run-1", evidence_ids: list[str] | None = None) -> dict[str, object]:
    first, _second = (evidence_ids or ["paper-a-evidence-1", "paper-b-evidence-1"])[:2]
    return {
        "schema_version": "prototype_provider_report_draft_v1",
        "run_id": run_id,
        **ROUTE,
        "title": "Provider-assisted synthetic evidence report",
        "executive_summary": "The supplied synthetic evidence supports bounded local Prototype findings.",
        "reviewed_literature_overview": [
            {
                "text": "The synthetic literature links latency and provenance findings.",
                "statement_type": "grounded",
                "evidence_ids": [first],
                "strength": "high",
            }
        ],
        "thematic_findings": [
            {
                "text": "A broader deployment effect remains an inference.",
                "statement_type": "inference",
                "evidence_ids": [],
                "strength": "low",
            }
        ],
        "grounded_claims": [
            {
                "claim_text": "The local fixture records a bounded latency improvement.",
                "evidence_ids": [first],
                "strength": "high",
                "claim_type": "grounded",
            }
        ],
        "limitations_and_uncertainties": [
            {
                "text": "The generated corpus cannot establish field generalization.",
                "statement_type": "uncertainty",
                "evidence_ids": [],
                "strength": "high",
            }
        ],
        "next_research_questions": [
            {
                "text": "Would the bounded findings hold for a small open-access corpus?",
                "statement_type": "proposal",
                "evidence_ids": [],
                "strength": "medium",
            }
        ],
        "referenced_evidence_ids": [first],
        "inference_or_proposal_sections": [],
    }


def _run(tmp_path: Path, result: CliExecResult) -> dict[str, object]:
    def runner(argv, prompt, cwd, env):
        assert argv[1] == "exec"
        assert "--output-schema" in argv
        assert "--output-last-message" in argv
        assert "--skip-git-repo-check" in argv
        assert "--json" in argv
        assert argv[-1] == "-"
        assert "openai_codex" not in " ".join(argv)
        assert prompt.startswith("{") or "report_draft_assist" in prompt
        return result

    return run_codex_cli_report_draft_once(
        prompt=json.dumps({"stage": "report_draft_assist", "run_id": "run-1"}),
        output_schema={"type": "object"},
        cwd=str(tmp_path / "cli-cwd"),
        timeout_seconds=30,
        runner=runner,
        executable="codex",
    )


def test_exec_argv_uses_only_installed_cli_flags() -> None:
    argv = build_codex_cli_exec_argv(
        executable="codex",
        cwd="D:/task/cwd",
        schema_path="D:/task/cwd/output-schema.json",
        last_message_path="D:/task/cwd/last-message.json",
    )
    assert argv[:2] == ["codex", "exec"]
    assert "--ask-for-approval" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert "openai_codex" not in argv


def test_successful_structured_last_message(tmp_path: Path) -> None:
    payload = json.dumps(_candidate(), sort_keys=True)
    result = _run(tmp_path, CliExecResult(0, "", "", last_message=payload))
    assert result["status"] == "completed"
    assert result["result_category"] == "provider_output_received"
    assert result["provider_call_count"] == 1
    assert json.loads(result["raw_output"])["title"] == "Provider-assisted synthetic evidence report"


def test_jsonl_progress_plus_valid_final_output(tmp_path: Path) -> None:
    payload = json.dumps(_candidate(), sort_keys=True)
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "item.started", "item": {"type": "reasoning"}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": payload}}),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    result = _run(tmp_path, CliExecResult(0, stdout, ""))
    assert result["status"] == "completed"
    assert json.loads(result["raw_output"])["run_id"] == "run-1"


def test_nonzero_exit(tmp_path: Path) -> None:
    result = _run(tmp_path, CliExecResult(2, "", "failed"))
    assert result["status"] == "failed"
    assert result["reason_category"] == REASON_EXIT_NONZERO
    assert result["provider_call_count"] == 1
    assert result["exit_code"] == 2
    assert result["timed_out"] is False


def test_timeout(tmp_path: Path) -> None:
    result = _run(tmp_path, CliExecResult(124, "", "", timed_out=True))
    assert result["status"] == "timeout"
    assert result["reason_category"] == REASON_TIMEOUT


def test_missing_final_output(tmp_path: Path) -> None:
    result = _run(tmp_path, CliExecResult(0, "", ""))
    assert result["status"] == "failed"
    assert result["reason_category"] == REASON_FINAL_OUTPUT_MISSING


def test_malformed_json(tmp_path: Path) -> None:
    result = _run(tmp_path, CliExecResult(0, "not-json", "", last_message="still not json"))
    assert result["status"] == "failed"
    assert result["reason_category"] == REASON_OUTPUT_PARSE_FAILED


def test_unavailable_cli() -> None:
    result = run_codex_cli_report_draft_once(
        prompt=json.dumps({"stage": "report_draft_assist"}),
        output_schema={"type": "object"},
        cwd="D:/unused",
        executable="",
    )
    assert result["status"] == "unavailable"
    assert result["reason_category"] == REASON_CLI_UNAVAILABLE
    assert result["provider_call_count"] == 0


def test_valid_json_wrong_schema_is_rejected_by_candidate_validator() -> None:
    raw = json.dumps({"schema_version": "wrong", "title": "x"}).encode("utf-8")
    validation = prototype_live_provider.validate_quarantined_output(
        raw_output=raw,
        run_id="run-1",
        evidence_ids=["paper-a-evidence-1"],
    )
    assert validation["valid"] is False
    assert validation["schema_valid"] is False
    assert "provider_output_schema_invalid" in validation["reasons"]


def test_unknown_evidence_and_paper_ids_are_rejected() -> None:
    candidate = _candidate()
    candidate["grounded_claims"][0]["evidence_ids"] = ["unknown-paper-evidence-9"]
    candidate["referenced_evidence_ids"] = ["unknown-paper-evidence-9"]
    validation = prototype_live_provider.validate_quarantined_output(
        raw_output=json.dumps(candidate).encode("utf-8"),
        run_id="run-1",
        evidence_ids=["paper-a-evidence-1"],
        paper_ids=["paper-a"],
    )
    assert validation["valid"] is False
    assert validation["evidence_valid"] is False
    assert validation["unknown_evidence_count"] == 1
    assert "provider_output_unknown_paper" in validation["reasons"]

    valid_shape = _candidate()
    paper_mismatch = prototype_live_provider.validate_quarantined_output(
        raw_output=json.dumps(valid_shape).encode("utf-8"),
        run_id="run-1",
        evidence_ids=["paper-a-evidence-1", "paper-b-evidence-1"],
        paper_ids=["paper-b"],
    )
    assert paper_mismatch["valid"] is False
    assert paper_mismatch["evidence_valid"] is False
    assert "provider_output_unknown_paper" in paper_mismatch["reasons"]


def test_extract_prefers_last_message_over_progress_jsonl() -> None:
    expected = json.dumps(_candidate(), sort_keys=True)
    extracted, reason = extract_final_candidate_json(
        last_message=expected,
        stdout=json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}),
    )
    assert reason is None
    assert json.loads(extracted or "{}")["title"] == "Provider-assisted synthetic evidence report"


def test_redaction_strips_tokens_cookies_paths_and_session_ids() -> None:
    raw = (
        "Bearer super-secret-value-123456 cookie=abc123 "
        "sk-abcdefghijklmnop access_token=zzzz "
        r"C:\Users\EXAMPLE\.codex\auth.json session_id=sess-99"
    )
    redacted = redact_cli_text(raw)
    excerpt = bounded_excerpt(raw)
    assert "super-secret-value-123456" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted
    assert "abc123" not in excerpt
    assert "sess-99" not in redacted
    assert "[REDACTED]" in redacted
    assert "[USER_PATH]" in redacted or "[CREDENTIAL_PATH]" in redacted


def test_node_official_launcher_prefers_codex_js_entry(tmp_path: Path) -> None:
    node = tmp_path / "node.exe"
    js_dir = tmp_path / "npm" / "node_modules" / "@openai" / "codex" / "bin"
    js_dir.mkdir(parents=True)
    js_path = js_dir / "codex.js"
    cmd = tmp_path / "npm" / "codex.cmd"
    node.write_text("", encoding="utf-8")
    js_path.write_text("", encoding="utf-8")
    cmd.write_text("", encoding="utf-8")
    launcher = resolve_official_launcher(node_path=str(node), official_js_path=str(js_path), cmd_path=str(cmd))
    assert launcher.kind == LAUNCHER_NODE
    assert launcher.argv_prefix == (str(node), str(js_path))


def test_argv_and_stdin_keep_prompt_out_of_the_command_line(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    prompt = json.dumps({"stage": "report_draft_assist", "run_id": "run-1"})

    def runner(argv, received_prompt, cwd, env):
        captured["argv"] = list(argv)
        captured["prompt"] = received_prompt
        captured["cwd"] = cwd
        return CliExecResult(0, "", "", last_message=json.dumps(_candidate(), sort_keys=True))

    result = run_codex_cli_report_draft_once(
        prompt=prompt,
        output_schema={"type": "object"},
        cwd=str(tmp_path / "cli-cwd"),
        timeout_seconds=30,
        runner=runner,
        launcher_argv=["node", "codex.js"],
        launcher_kind=LAUNCHER_NODE,
    )
    argv = list(captured["argv"])
    assert argv[:3] == ["node", "codex.js", "exec"]
    assert argv[-1] == "-"
    assert "--cd" in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path / "cli-cwd")
    assert prompt not in argv
    assert captured["prompt"] == prompt
    assert result["safe_diagnostics"]["launcher_kind"] == LAUNCHER_NODE
    assert result["safe_diagnostics"]["argv_shape"][-1] == "-"


def test_jsonl_uses_only_the_last_terminal_agent_message() -> None:
    first = json.dumps({"schema_version": "prototype_provider_report_draft_v1", "title": "first"})
    last = json.dumps(_candidate(), sort_keys=True)
    stdout = "\n".join(
        [
            json.dumps({"type": "item.started", "item": {"type": "agent_message", "text": first}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": first}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": last}}),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    extracted, reason = extract_final_candidate_json(last_message="", stdout=stdout)
    assert reason is None
    assert json.loads(extracted or "{}")["title"] == "Provider-assisted synthetic evidence report"


def test_turn_completion_without_final_message_is_missing(tmp_path: Path) -> None:
    stdout = json.dumps({"type": "turn.completed"})
    result = _run(tmp_path, CliExecResult(0, stdout, ""))
    assert result["reason_category"] == REASON_FINAL_OUTPUT_MISSING


def test_malformed_jsonl_without_terminal_message_is_parse_failed(tmp_path: Path) -> None:
    result = _run(tmp_path, CliExecResult(0, "{not-json\n", ""))
    assert result["reason_category"] == REASON_OUTPUT_PARSE_FAILED


def test_usage_limit_jsonl_error_event_is_classified_precisely(tmp_path: Path) -> None:
    stdout = json.dumps({
        "type": "error",
        "message": (
            "You've hit your usage limit. Visit the account usage page to purchase more credits. "
            "Retry after Aug 27, 2026 12:47 PM. Bearer leaked-token-value-123456 "
            r"C:\Users\EXAMPLE\.codex\auth.json session_id=sess-quota-1"
        ),
    })
    result = _run(tmp_path, CliExecResult(1, stdout, ""))
    diagnostics = result["safe_diagnostics"]
    usage_limit = diagnostics["usage_limit"]
    assert result["status"] == "failed"
    assert result["reason_category"] == REASON_USAGE_LIMIT
    assert "raw_output" not in result
    assert result["exit_code"] == 1
    assert result["timed_out"] is False
    assert diagnostics["source"] == "codex_cli"
    assert diagnostics["exit_code"] == 1
    assert usage_limit["source"] == "codex_cli"
    assert usage_limit["reset_time_text"] == "Aug 27, 2026 12:47 PM"
    assert usage_limit["reset_timezone_claimed"] is False
    assert "leaked-token-value-123456" not in json.dumps(diagnostics)
    assert "sess-quota-1" not in json.dumps(diagnostics)
    assert "C:\\Users\\EXAMPLE" not in json.dumps(diagnostics)
    assert extract_usage_limit_reset_time_text(stdout) == "Aug 27, 2026 12:47 PM"


def test_usage_limit_turn_failed_event_is_classified_precisely(tmp_path: Path) -> None:
    stdout = json.dumps({
        "type": "turn.failed",
        "error": {
            "message": "You've hit your usage limit. Retry after Aug 27, 2026 12:47 PM or purchase more credits.",
        },
    })
    result = _run(tmp_path, CliExecResult(1, stdout, ""))
    assert result["reason_category"] == REASON_USAGE_LIMIT
    assert result["safe_diagnostics"]["usage_limit"]["reset_time_text"] == "Aug 27, 2026 12:47 PM"
    assert "raw_output" not in result


def test_nonzero_exit_ignores_partial_schema_message(tmp_path: Path) -> None:
    stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_candidate())}})
    result = _run(tmp_path, CliExecResult(2, stdout, "schema rejected"))
    assert result["status"] == "failed"
    assert result["reason_category"] == REASON_EXIT_NONZERO
    assert "raw_output" not in result
    assert result["safe_diagnostics"]["exit_code"] == 2
    assert result["safe_diagnostics"]["stderr_excerpt"]
    assert "schema rejected" in result["safe_diagnostics"]["stderr_excerpt"]


def test_compiled_schema_candidate_still_passes_canonical_pydantic() -> None:
    compiled = build_codex_cli_report_draft_schema(prototype_live_provider.PrototypeProviderDraft.model_json_schema())
    rendered = json.dumps(compiled)
    assert "$ref" not in rendered
    candidate = _candidate()
    validated = prototype_live_provider.PrototypeProviderDraft.model_validate(candidate)
    assert validated.run_id == "run-1"


def test_human_reason_message_stays_secret_free() -> None:
    text = human_reason_message(REASON_EXIT_NONZERO)
    assert "token" not in text.lower()
    assert "C:\\" not in text


def test_acceptance_rejection_does_not_persist_a_report_version() -> None:
    reasons = prototype_live_provider._precise_rejection_reasons(
        {"reason_category": None},
        {"reasons": ["provider_output_schema_invalid"]},
    )
    assert reasons == ["CODEX_CANDIDATE_SCHEMA_INVALID"]
    evidence_reasons = prototype_live_provider._precise_rejection_reasons(
        {"reason_category": None},
        {"reasons": ["provider_output_unknown_evidence", "provider_output_unknown_paper"]},
    )
    assert evidence_reasons == ["CODEX_EVIDENCE_REFERENCE_INVALID"]
    assert human_reason_message("CODEX_CANDIDATE_REJECTED") == (
        "The candidate was rejected by acceptance policy."
    )
    assert "token" not in human_reason_message("CODEX_CANDIDATE_REJECTED").lower()
