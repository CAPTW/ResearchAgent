"""Current-authority, atomicity, replay and isolation for the app review bridge."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import httpx
import pytest

from app.auth.resource_access import ResourceActor
from app.db import bootstrap
from app.domain.review_offline import Execute
from app.domain.review_preparation import encoded, sha
from app.repositories import paper_reviews, review_offline as repository
from app.services import review_preparation, review_offline as service
from tests import test_review_preparation as preparation_tests
from tests.test_review_offline_api import enable

linked = preparation_tests.linked
seeded = preparation_tests.seeded
selected = preparation_tests.selected


@pytest.fixture
def prepared(linked):
    value = review_preparation.prepare(linked["topic"], linked["version"], selected(linked), linked["actor"])
    return linked, value, Execute(request_id="one", expected_preparation_digest=sha(encoded(value)))


def execute(prepared, runtime=None, body=None, actor=None):
    context, value, request = prepared
    return service.execute(runtime, context["topic"], context["version"], value["preparation_id"],
                           body or request, actor or context["actor"])


def database():
    with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
        return "\n".join(c.iterdump())


def test_duplicate_concurrent_request_calls_adapter_once_and_never_updates_latest_cache(prepared, monkeypatch):
    context, value, _ = prepared
    before = paper_reviews.get_review(context["topic"], context["run"], context["paper"], read_only=True)
    barrier = Barrier(4)
    calls = []
    original = service.SyntheticReviewProvider.generate
    def capture(self, request):
        calls.append(deepcopy(request.packet))
        return original(self, request)
    monkeypatch.setattr(service.SyntheticReviewProvider, "generate", capture)
    def submit(_):
        barrier.wait(timeout=10)
        return execute(prepared, service.OfflineReviewRuntime())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert len(calls) == 1 and all(result == results[0] for result in results)
    with closing(bootstrap.connect_metadata_index()) as c:
        assert c.execute("SELECT count(*) FROM paper_review_offline_results").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM ai_review_input_history").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM ai_review_drafts").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute("UPDATE paper_review_offline_results SET actor_id='changed'")
    assert paper_reviews.get_review(context["topic"], context["run"], context["paper"], read_only=True) == before
    assert review_preparation.detail(context["topic"], context["version"], value["preparation_id"], context["actor"]) == value


@pytest.mark.parametrize("phase", ["after_receipt", "after_result"])
def test_partial_storage_rolls_back_and_identical_request_can_retry(prepared, monkeypatch, phase):
    before = database()
    def fail(current):
        if current == phase:
            raise RuntimeError("injected storage failure")
    monkeypatch.setattr(repository, "checkpoint", fail)
    with pytest.raises(RuntimeError, match="injected"):
        execute(prepared, service.OfflineReviewRuntime())
    assert database() == before
    monkeypatch.setattr(repository, "checkpoint", lambda _: None)
    result = execute(prepared, service.OfflineReviewRuntime())
    assert result == execute(prepared)  # completed replay is safe with execution disabled


def test_adapter_failure_is_not_empty_success_and_does_not_create_partial_history(prepared, monkeypatch):
    from app.services.ai_provider import ProviderResponse
    before = database()
    monkeypatch.setattr(service.SyntheticReviewProvider, "generate", lambda *args: ProviderResponse(
        status="failed", provider_name=service.ADAPTER, run_id="none", paper_id="none"))
    with pytest.raises(ValueError, match="review_offline_adapter_failed"):
        execute(prepared, service.OfflineReviewRuntime())
    assert database() == before


def test_changed_body_cross_preparation_and_denied_actor_do_not_write(prepared):
    context, value, body = prepared
    result = execute(prepared, service.OfflineReviewRuntime())
    before = database()
    with pytest.raises(ValueError, match="preparation_changed"):
        execute(prepared, service.OfflineReviewRuntime(), body=body.model_copy(update={"expected_preparation_digest": "0" * 64}))
    with pytest.raises(KeyError):
        execute(prepared, service.OfflineReviewRuntime(), actor=ResourceActor.oidc_user("outsider"))
    assert database() == before
    another = review_preparation.prepare(context["topic"], context["version"], selected(context, request="another"), context["actor"])
    other = (context, another, body.model_copy(update={"expected_preparation_digest": sha(encoded(another))}))
    before = database()
    with pytest.raises(ValueError, match="request_conflict"):
        execute(other, service.OfflineReviewRuntime())
    assert database() == before and execute(prepared) == result


def test_corrupt_receipt_is_not_hidden_as_disabled_or_success(prepared):
    context, value, _ = prepared
    result = execute(prepared, service.OfflineReviewRuntime())
    with closing(bootstrap.connect_metadata_index()) as c, c:
        c.execute("DROP TRIGGER ai_review_input_no_update")
        c.execute("UPDATE ai_review_input_history SET draft_json='{}' WHERE receipt_id=?", (result["receipt_id"],))
    with pytest.raises(ValueError, match="review_offline_integrity_invalid"):
        service.status(None, context["topic"], context["version"], value["preparation_id"], context["actor"])
    with pytest.raises(ValueError, match="review_offline_integrity_invalid"):
        execute(prepared)


@pytest.mark.parametrize("key,value", [("XRML_ENVIRONMENT", "production"), ("XRML_SHARED_DEPLOYMENT", "1"),
    ("TOPIC_SCHEDULER_ENABLED", "1"), ("XRML_RUN_EXECUTOR_MODE", "thread"),
    ("APP_BASE_URL", "https://example.com"), ("XRML_REVIEW_OFFLINE_DATA_ROOT", "relative")])
def test_enabled_runtime_requires_isolated_environment(tmp_path, monkeypatch, key, value):
    monkeypatch.setattr(bootstrap, "STORAGE_ROOT", tmp_path / "runs")
    enable(monkeypatch)
    assert service.build_runtime() is not None
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="review_offline_configuration_invalid"):
        service.build_runtime()


def test_journal_opt_in_cannot_enable_review_runtime(monkeypatch):
    monkeypatch.delenv("XRML_REVIEW_OFFLINE_ENABLED", raising=False)
    monkeypatch.setenv("XRML_JOURNAL_OFFLINE_ENABLED", "1")
    assert service.build_runtime() is None


def test_adapter_rejects_live_mode_model_and_command(prepared):
    from app.domain.review_range_input import build_packet
    from app.services.ai_provider import ProviderRequest
    packet = build_packet(prepared[1])
    for changes in ({"mode": "live_command"}, {"model": "real-model"}, {"command": ["provider"]}, {"explicit_live": True}):
        with pytest.raises(ValueError, match="target_mismatch"):
            service.SyntheticReviewProvider().generate(ProviderRequest(**dict(
                dict(provider_name=service.ADAPTER, packet=packet, mode="stub"), **changes)))


def test_selected_receipt_reaches_exact_journal_prompt_without_current_source(prepared):
    from app.domain import journal_exchange
    from app.repositories import local_synthesis
    from app.services import paper_bank
    context, preparation, _ = prepared
    saved = execute(prepared, service.OfflineReviewRuntime())
    context["path"].unlink()
    item = next(item for item in paper_bank.list_topic_papers(context["topic"], page_size=100)["items"]
                if item["paperVersionId"] == context["version"])
    selection = [dict(paper_version_id=context["version"], metadata_digest=item["metadataDigest"],
                      review_input=dict(receipt_id=saved["receipt_id"], component_ids=["/evidence_spans/0/text"]))]
    job, created = local_synthesis.create(context["topic"], selection, "explicit-report", context["actor"])
    assert created
    for step in range(job["total_steps"]):
        job, advanced = local_synthesis.advance(context["topic"], job["job_id"], step)
        assert advanced
    packet = journal_exchange.packet(job)
    assert packet["review_inputs"][0]["source_preparation"]["selection"]["start"] == preparation["selection"]["start"]
    reference = next(row for row in packet["references"] if row["kind"] == "review_input_excerpt")
    assert reference["text"] == preparation["selection"]["text"]
    assert reference["source_preparation"] == packet["review_inputs"][0]["source_preparation"]
    assert reference["locator"]["char_start"] == 0
    assert reference["locator"]["char_end"] == preparation["selection"]["chars"]
    assert journal_exchange.provider_prompt(packet) == journal_exchange.preserved_json({
        key: packet[key] for key in ("packet_id", "topic_label", "selected_paper_version_ids", "references", "review_inputs")})


def test_source_sidecar_executes_and_reopens_with_execution_disabled(prepared, tmp_path):
    context, preparation, body = prepared
    data = bootstrap.STORAGE_ROOT.parent.resolve()
    assert data.is_relative_to(tmp_path.resolve())
    dist = tmp_path / "bridge-sidecar-web"
    dist.mkdir()
    (dist / "index.html").write_text("<html><title>Offline review fixture</title></html>", encoding="utf-8")
    env = dict(os.environ, XRML_DATA_DIR=str(data), XRML_REVIEW_OFFLINE_DATA_ROOT=str(data),
               XRML_JOURNAL_OFFLINE_ENABLED="0", XRML_ENVIRONMENT="development", XRML_SHARED_DEPLOYMENT="0",
               XRML_RUN_EXECUTOR_MODE="disabled", TOPIC_SCHEDULER_ENABLED="0", AUTH_MODE="disabled", XRML_PROVIDER="disabled",
               APP_BASE_URL="http://127.0.0.1:8000")
    url = f'/api/topics/{context["topic"]}/paper-bank/{context["version"]}/review-preparations/{preparation["preparation_id"]}'
    saved = receipt = None
    for launch in range(2):
        env["XRML_REVIEW_OFFLINE_ENABLED"] = "1" if launch == 0 else "0"
        with (tmp_path / f"review-sidecar-{launch}.stderr.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "app.desktop.sidecar", "--web-dist", str(dist),
                "--data-dir", str(data), "--startup-timeout-seconds", "20"],
                cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                assert process.stdout is not None and process.stdin is not None
                ready = json.loads(process.stdout.readline())
                assert ready["event"] == "ready", ready
                with httpx.Client(base_url=f"http://127.0.0.1:{ready['port']}", timeout=5, trust_env=False) as client:
                    cap = client.get(url + "/offline-review").json()
                    assert cap["enabled"] is (launch == 0)
                    response = client.post(url + "/offline-reviews", json=body.model_dump())
                    assert response.status_code == 200, response.text
                    if launch == 0:
                        saved = response.json()
                    else:
                        assert response.json() == saved and cap["latest"] == saved
                        assert client.post(url + "/offline-reviews", json={**body.model_dump(), "request_id": "new"}).status_code == 409
                    result = client.get(url.split("/review-preparations/")[0] + "/review-inputs/" + response.json()["receipt_id"]).json()
                    if launch == 0:
                        receipt = result
                        context["path"].unlink()
                    else:
                        assert result == receipt
                    assert client.get(url).json() == preparation
                process.stdin.write("shutdown\n")
                process.stdin.flush()
                assert process.wait(timeout=15) == 0
                events = [json.loads(line) for line in process.stdout.read().splitlines() if line.strip()]
                assert any(row.get("event") == "shutdown" and row.get("reason") == "host_requested" for row in events)
                (tmp_path / f"review-sidecar-{launch}.events.json").write_text(json.dumps([ready, *events]), encoding="utf-8")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
