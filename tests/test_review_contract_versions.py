"""Upgrade reads/replays old contracts; new requests cannot select old validation."""
from contextlib import closing

import pytest

from app.db import bootstrap
from app.domain import prepared_review
from tests import test_review_execution as durable, test_review_transport_offline as transport
from tests import test_review_offline_api as atomic

range_source = atomic.range_source
seeded = atomic.seeded


@pytest.mark.parametrize("kind", ["atomic", "durable", "transport"])
@pytest.mark.parametrize("version", ["V1", "V2"])
def test_old_history_survives_upgrade_and_ready_can_finish(range_source, monkeypatch, kind, version):
    transport.enable(monkeypatch)
    durable.enable(monkeypatch)
    original = prepared_review.prepare

    def old_prepare(*args, **kwargs):
        return original(*args, **(kwargs | {"schema_version": "PREPARED_REVIEW_REQUEST_" + version}))

    with atomic.client() as http:
        with monkeypatch.context() as old:
            # Simulate the old factory. Checked-in pre-change V1/V2 archives
            # separately pin their exact schema, instructions and record bytes.
            old.setattr(prepared_review, "prepare", old_prepare)
            if kind == "atomic":
                preparation, root = atomic.prepared(http, range_source)
                listing = http.get(root + "/offline-review").json()
                body = dict(request_id="old-atomic", expected_preparation_digest=listing["preparation_digest"])
                base = root + "/offline-reviews"
                result = durable.post(http, base, body)
            elif kind == "durable":
                base, body = durable.prepared(http, range_source)
                result = durable.post(http, base, body)
            else:
                base, body, result = transport.request(http, range_source)
        if kind == "atomic":
            assert durable.post(http, base, body) == result
            saved = result
        else:
            assert result["contract"]["prepared"]["schema_version"] == "PREPARED_REVIEW_REQUEST_" + version
            url = base + "/" + result["contract"]["execution_id"]
            assert http.get(url).json() == result
            if kind == "durable":
                approved = durable.command(http, url, result, "approve")
                durable.post(http, url + "/dispatch", dict(request_id="dispatch", expected_sequence=approved["current"]["sequence"]))
            else:
                url, approved = transport.grant(http, base, result)
                durable.post(http, url + "/offline-dispatch", dict(request_id="dispatch", expected_sequence=approved["current"]["sequence"]))
            saved = durable.wait_state(http, url, "RESULT_SAVED")
        receipt_id = saved["receipt_id"] if kind == "atomic" else saved["current"]["receipt_id"]
        receipt_url = base.split("/review-preparations/")[0] + "/review-inputs/" + receipt_id
        receipt = http.get(receipt_url).json()
        assert receipt["validation"]["prepared_execution"]["schema_version"] == "PREPARED_REVIEW_RESULT_" + version
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            before = "\n".join(c.iterdump())
    with atomic.client() as http:
        assert http.get(receipt_url).json() == receipt
        if kind == "atomic":
            assert durable.post(http, base, body) == saved
        else:
            assert http.get(url).json() == saved
            assert durable.post(http, base + ("/offline-requests" if kind == "transport" else ""), body) == saved
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert "\n".join(c.iterdump()) == before
        new_body = body | {"request_id": "new-v3"}
        create_url = base + ("/offline-requests" if kind == "transport" else "")
        for field in ("schema_version", "prepared_schema"):
            assert http.post(create_url, json=new_body | {field: "PREPARED_REVIEW_REQUEST_" + version}).status_code == 422
        with closing(bootstrap.connect_metadata_index(read_only=True)) as c:
            assert "\n".join(c.iterdump()) == before
        new = durable.post(http, create_url, new_body)
        if kind == "atomic":
            new_receipt = http.get(receipt_url.rsplit("/", 1)[0] + "/" + new["receipt_id"]).json()
            assert new_receipt["validation"]["prepared_execution"]["contract"]["schema_version"] == prepared_review.SCHEMA
        else:
            assert new["contract"]["prepared"]["schema_version"] == prepared_review.SCHEMA
        assert http.get(receipt_url).json() == receipt
