"""Reserved paper transport composition through the actual source-sidecar entrypoint."""
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Thread
import time

import httpx

from app.db import bootstrap
from tests import test_review_offline as previous

linked = previous.linked
seeded = previous.seeded
selected = previous.selected
prepared = previous.prepared


def test_source_sidecar_transport_approval_receipt_and_disabled_restart(prepared, tmp_path):
    context, preparation, request = prepared
    data = bootstrap.STORAGE_ROOT.parent.resolve()
    assert data.is_relative_to(tmp_path.resolve())
    dist = tmp_path / "transport-sidecar-web"
    dist.mkdir()
    (dist / "index.html").write_text("<html><title>Offline transport fixture</title></html>", encoding="utf-8")
    env = dict(os.environ, XRML_DATA_DIR=str(data), XRML_PAPER_TRANSPORT_OFFLINE_DATA_ROOT=str(data),
               XRML_JOURNAL_OFFLINE_ENABLED="0", XRML_REVIEW_OFFLINE_ENABLED="0",
               XRML_REVIEW_EXECUTION_OFFLINE_ENABLED="0", XRML_ENVIRONMENT="development",
               XRML_SHARED_DEPLOYMENT="0", XRML_RUN_EXECUTOR_MODE="disabled", TOPIC_SCHEDULER_ENABLED="0",
               AUTH_MODE="disabled", XRML_PROVIDER="disabled", APP_BASE_URL="http://127.0.0.1:8000")
    root = f'/api/topics/{context["topic"]}/paper-bank/{context["version"]}'
    base = root + f'/review-preparations/{preparation["preparation_id"]}/transport-executions'
    body = request.model_dump()
    saved = receipt = approval = dispatch = revocation = url = None
    for launch in range(2):
        env["XRML_PAPER_TRANSPORT_OFFLINE_ENABLED"] = "1" if launch == 0 else "0"
        with (tmp_path / f"transport-sidecar-{launch}.stderr.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "app.desktop.sidecar", "--web-dist", str(dist),
                "--data-dir", str(data), "--startup-timeout-seconds", "20"],
                cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            startup: Queue[str] = Queue()
            reader = None
            try:
                assert process.stdout is not None and process.stdin is not None
                reader = Thread(target=lambda: startup.put(process.stdout.readline()), daemon=True)
                reader.start()
                ready = json.loads(startup.get(timeout=30))
                reader.join(timeout=1)
                assert ready["event"] == "ready", ready
                with httpx.Client(base_url=f"http://127.0.0.1:{ready['port']}", timeout=5, trust_env=False) as client:
                    listing = client.get(base).json()
                    assert listing["offline"]["enabled"] is (launch == 0)
                    assert all(listing["capabilities"][key] is False for key in ("create", "approve", "dispatch"))
                    response = client.post(base + "/offline-requests", json=body)
                    assert response.status_code == 200, response.text
                    if launch == 0:
                        execution = response.json()
                        assert execution["current"]["state"] == "READY" and execution["authority"] is None
                        url = base + "/" + execution["contract"]["execution_id"]
                        approval = dict(request_id="approve", expected_sequence=0,
                                        expected_contract_sha256=execution["contract_sha256"])
                        granted = client.post(url + "/offline-approval", json=approval)
                        assert granted.status_code == 200, granted.text
                        assert granted.json()["current"]["state"] == "APPROVED"
                        dispatch = dict(request_id="dispatch", expected_sequence=1)
                        started = client.post(url + "/offline-dispatch", json=dispatch)
                        assert started.status_code == 200, started.text
                        deadline = time.monotonic() + 10
                        while True:
                            saved = client.get(url).json()
                            if saved["current"]["state"] == "RESULT_SAVED" or time.monotonic() >= deadline:
                                break
                            time.sleep(0.05)
                        assert [event["state"] for event in saved["events"]] == [
                            "READY", "APPROVED", "LEASED", "RUNNING", "RESULT_RECEIVED", "RESULT_SAVED"]
                        # The immutable receipt remains saved after revocation;
                        # REVOKED is stable across restart unlike clock validity.
                        revocation = dict(request_id="revoke", expected_sequence=saved["current"]["sequence"])
                        revoked = client.post(url + "/offline-revocation", json=revocation)
                        assert revoked.status_code == 200, revoked.text
                        assert revoked.json()["current"] == saved["current"]
                        saved = revoked.json()
                        assert saved["authority"]["validity"] == "REVOKED"
                    else:
                        assert response.json() == saved
                        assert client.get(url).json() == saved
                        for suffix, command in (("offline-approval", approval), ("offline-dispatch", dispatch),
                                                ("offline-revocation", revocation)):
                            replay = client.post(url + "/" + suffix, json=command)
                            assert replay.status_code == 200 and replay.json() == saved
                        assert client.post(base + "/offline-requests", json=body | {"request_id": "new"}).status_code == 409
                    current_receipt = client.get(root + "/review-inputs/" + saved["current"]["receipt_id"]).json()
                    if launch == 0:
                        receipt = current_receipt
                        assert receipt["input_origin"] == "FIXTURE_SYNTHETIC"
                        assert receipt["validation"]["execution"]["schema_version"] == "PREPARED_REVIEW_OFFLINE_TRANSPORT_V1"
                        context["path"].unlink()
                    else:
                        assert current_receipt == receipt
                process.stdin.write("shutdown\n")
                process.stdin.flush()
                assert process.wait(timeout=15) == 0
                events = [json.loads(line) for line in process.stdout.read().splitlines() if line.strip()]
                assert any(row.get("event") == "shutdown" and row.get("reason") == "host_requested" for row in events)
                (tmp_path / f"transport-sidecar-{launch}.events.json").write_text(json.dumps([ready, *events]), encoding="utf-8")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if reader is not None:
                    reader.join(timeout=1)
