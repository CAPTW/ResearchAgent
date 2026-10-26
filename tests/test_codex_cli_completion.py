"""JSONL framing must preserve Unicode inside a completed response."""
import json

import pytest

from app.services.providers.base import ProviderError, ProviderReportedFailure
from app.services.providers.codex_cli_completion import read_completion


def events(final: str) -> list[dict]:
    return [
        dict(type="thread.started", thread_id="synthetic-thread"),
        dict(type="turn.started"),
        dict(type="item.completed", item=dict(type="agent_message", text=final)),
        dict(type="turn.completed", usage=dict(input_tokens=0, output_tokens=7)),
    ]


def encoded(rows: list[dict], separator: str = "\n") -> str:
    return separator.join(json.dumps(row, ensure_ascii=False) for row in rows)


@pytest.mark.parametrize("character", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("separator", ["\n", "\r\n"])
def test_unicode_inside_json_text_is_not_an_event_boundary(character, separator):
    final = json.dumps({"text": f" e\u0301 한글🙂 before{character}after "}, ensure_ascii=False)
    assert read_completion(encoded(events(final), separator) + separator, final, 0, error_prefix="test") == (
        "synthetic-thread", dict(input_tokens=0, output_tokens=7),
    )


@pytest.mark.parametrize("character", ["\u0085", "\u2028", "\u2029", "\r"])
def test_non_jsonl_event_separator_is_unconfirmed(character):
    with pytest.raises(ProviderError, match="^test_completion_unconfirmed$"):
        read_completion(encoded(events("{}"), character), "{}", 0, error_prefix="test")


@pytest.mark.parametrize("character", ["\u0085", "\u2028", "\u2029"])
def test_non_json_whitespace_between_events_is_not_discarded(character):
    rows = events("{}")
    stream = encoded(rows[:2]) + "\n" + character + "\n" + encoded(rows[2:])
    with pytest.raises(ProviderError, match="^test_completion_unconfirmed$"):
        read_completion(stream, "{}", 0, error_prefix="test")


def test_existing_ascii_blank_lines_and_escaped_newlines_remain_compatible():
    final = json.dumps({"text": "before\nmiddle\rafter"}, ensure_ascii=False)
    stream = "\r\n \t\r\n" + encoded(events(final), "\r\n") + "\r\n"
    assert read_completion(stream, final, 0, error_prefix="test")[0] == "synthetic-thread"


@pytest.mark.parametrize("change", ["mismatch", "nonzero", "tool", "missing_terminal", "duplicate_key", "malformed"])
def test_unicode_does_not_confirm_ambiguous_or_invalid_results(change):
    final = '{"text":"before\u2028after"}'
    rows, code = events(final), 0
    if change == "mismatch":
        final = '{"text":"different"}'
    elif change == "nonzero":
        code = 7
    elif change == "tool":
        rows.insert(2, dict(type="item.completed", item=dict(type="command_execution")))
    elif change == "missing_terminal":
        rows.pop()
    stream = encoded(rows)
    if change == "duplicate_key":
        stream = stream.replace('"type": "turn.completed"', '"type": "turn.completed", "type": "turn.completed"')
    elif change == "malformed":
        stream += "\nnot json"
    with pytest.raises(ProviderError) as error:
        read_completion(stream, final, code, error_prefix="test")
    assert type(error.value) is ProviderError
    assert str(error.value) == ("test_exit_outcome_unknown" if code else "test_completion_unconfirmed")


def test_reported_failure_with_unicode_diagnostic_stays_redacted():
    rows = events("")[:2] + [dict(type="turn.failed", error=dict(message="private\u2028diagnostic"))]
    with pytest.raises(ProviderReportedFailure) as error:
        read_completion(encoded(rows), "", 0, error_prefix="test")
    assert error.value.status == "failed"
    assert "private" not in str(error.value)
