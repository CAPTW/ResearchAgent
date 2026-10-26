# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 CAPTW
"""Contract tests for the Playwright truth-pass webhook-unavailable harness.

These tests exercise pure helpers and source-text contracts only. They do not
launch a browser, mutate `storage/topic_notifications/settings.json`, or send
network requests.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from tests.playwright_dashboard_truth import (
    assert_no_external_webhook_delivery,
    classify_persisted_webhook_destination,
    evaluate_notification_delivery_history_contract,
    evaluate_webhook_unavailable_contract,
)

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "playwright_dashboard_truth.py"
STALE_EMPTY_ASSERT = "assert not original_webhook_destination.strip()"
STALE_RELOAD_EMPTY_ASSERT = 'assert not (await webhook_channel.get_by_label("Webhook 대상").input_value()).strip()'
STALE_EMPTY_HISTORY_BODY_ASSERT = (
    'assert "아직 외부 전달 기록이 없습니다." in observed_notification_text'
)


def test_classify_empty_and_nonempty_persisted_destinations() -> None:
    assert classify_persisted_webhook_destination("") == "empty"
    assert classify_persisted_webhook_destination("   ") == "empty"
    assert classify_persisted_webhook_destination(None) == "empty"
    classified = classify_persisted_webhook_destination("http://127.0.0.1:9/playwright-notification-webhook")
    assert classified == "configured_nonempty"
    assert classified != "http://127.0.0.1:9/playwright-notification-webhook"


def test_unavailable_contract_accepts_empty_or_configured_nonempty() -> None:
    evaluate_webhook_unavailable_contract(
        input_disabled=True,
        send_action_disabled=True,
        destination_class="empty",
        delivery_attempted=False,
    )
    evaluate_webhook_unavailable_contract(
        input_disabled=True,
        send_action_disabled=True,
        destination_class="configured_nonempty",
        delivery_attempted=False,
    )


def test_unavailable_contract_rejects_enabled_input() -> None:
    with pytest.raises(AssertionError, match="destination input must be disabled"):
        evaluate_webhook_unavailable_contract(
            input_disabled=False,
            send_action_disabled=True,
            destination_class="configured_nonempty",
            delivery_attempted=False,
        )


def test_unavailable_contract_rejects_enabled_send_action() -> None:
    with pytest.raises(AssertionError, match="send/test action must be disabled"):
        evaluate_webhook_unavailable_contract(
            input_disabled=True,
            send_action_disabled=False,
            destination_class="empty",
            delivery_attempted=False,
        )


def test_unavailable_contract_rejects_raw_destination_in_diagnostic() -> None:
    with pytest.raises(AssertionError, match="raw destination"):
        evaluate_webhook_unavailable_contract(
            input_disabled=True,
            send_action_disabled=True,
            destination_class="configured_nonempty",
            delivery_attempted=False,
            diagnostic_record={"destination": "http://127.0.0.1:9/playwright-notification-webhook"},
        )


def test_unavailable_contract_allows_safe_diagnostic_classification() -> None:
    evaluate_webhook_unavailable_contract(
        input_disabled=True,
        send_action_disabled=True,
        destination_class="configured_nonempty",
        delivery_attempted=False,
        diagnostic_record={
            "availability": "unavailable",
            "destination_class": "configured_nonempty",
            "input_disabled": True,
            "send_action_disabled": True,
            "pathname": "/app/settings/notifications",
        },
    )


def test_no_external_delivery_guard_remains_required() -> None:
    assert_no_external_webhook_delivery(delivery_attempted=False)
    with pytest.raises(AssertionError, match="must not attempt external delivery"):
        assert_no_external_webhook_delivery(delivery_attempted=True)


def test_harness_source_rejects_unconditional_empty_destination_assert() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    assert STALE_EMPTY_ASSERT not in source
    assert STALE_RELOAD_EMPTY_ASSERT not in source
    tree = ast.parse(source)
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "classify_persisted_webhook_destination" in names
    assert "evaluate_webhook_unavailable_contract" in names
    assert "assert_no_external_webhook_delivery" in names


def test_harness_helpers_do_not_return_or_log_raw_destination() -> None:
    source = inspect.getsource(classify_persisted_webhook_destination)
    assert "return raw" not in source
    assert "http://" not in source


def test_in_memory_mutations_reject_workarounds() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    assert STALE_EMPTY_ASSERT not in source
    stale = source.replace(
        "destination_class = classify_persisted_webhook_destination(original_webhook_destination)",
        "assert not original_webhook_destination.strip()\n        destination_class = classify_persisted_webhook_destination(original_webhook_destination)",
        1,
    )
    assert STALE_EMPTY_ASSERT in stale
    assert "destination input must be disabled" in source
    assert "send/test action must be disabled" in source
    assert "if delivery_attempted:" in source
    assert "raw destination" in source


def test_harness_source_rejects_unavailable_empty_state_only_body_assert() -> None:
    source = HARNESS.read_text(encoding="utf-8")
    assert STALE_EMPTY_HISTORY_BODY_ASSERT not in source
    tree = ast.parse(source)
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "evaluate_notification_delivery_history_contract" in names
    assert "evaluate_notification_delivery_history_contract(" in source
    assert "region_text=history_text" in source
    assert "region_text=observed_notification_text" not in source
    assert "section.ai-card" in source
    assert 'filter(has_text="외부 전달 기록")' in source
    assert "history_record_count = await delivery_history.locator" in source
    assert "history_record_count = 0" not in source
    assert "channel=" not in source
    assert "executable_path" not in source
    assert "chrome.exe" not in source
    assert "max_retries" not in source
    assert "ignore console errors" not in source.casefold()
    assert "ignore request failures" not in source.casefold()


def test_delivery_history_contract_requires_visible_region() -> None:
    with pytest.raises(AssertionError, match="delivery-history region must exist"):
        evaluate_notification_delivery_history_contract(
            region_present=False,
            region_visible=False,
            record_count=0,
            region_text="",
            empty_state_visible=False,
        )
    with pytest.raises(AssertionError, match="delivery-history region must be visible"):
        evaluate_notification_delivery_history_contract(
            region_present=True,
            region_visible=False,
            record_count=0,
            region_text="아직 외부 전달 기록이 없습니다.",
            empty_state_visible=True,
        )


def test_delivery_history_contract_accepts_zero_and_nonempty_states() -> None:
    evaluate_notification_delivery_history_contract(
        region_present=True,
        region_visible=True,
        record_count=0,
        region_text="외부 전달 기록\n아직 외부 전달 기록이 없습니다.",
        empty_state_visible=True,
    )
    evaluate_notification_delivery_history_contract(
        region_present=True,
        region_visible=True,
        record_count=2,
        region_text="외부 전달 기록\n실패\nwebhook\n규칙 report_completed",
        empty_state_visible=False,
    )


def test_delivery_history_contract_rejects_missing_or_contradictory_state() -> None:
    with pytest.raises(AssertionError, match="empty-state"):
        evaluate_notification_delivery_history_contract(
            region_present=True,
            region_visible=True,
            record_count=0,
            region_text="외부 전달 기록",
            empty_state_visible=False,
        )
    with pytest.raises(AssertionError, match="empty-state"):
        evaluate_notification_delivery_history_contract(
            region_present=True,
            region_visible=True,
            record_count=3,
            region_text="외부 전달 기록\n아직 외부 전달 기록이 없습니다.",
            empty_state_visible=True,
        )
    with pytest.raises(AssertionError, match="delivery marker"):
        evaluate_notification_delivery_history_contract(
            region_present=True,
            region_visible=True,
            record_count=1,
            region_text="외부 전달 기록\nready",
            empty_state_visible=False,
        )
