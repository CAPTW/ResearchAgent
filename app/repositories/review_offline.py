"""Atomic synthetic request replay and receipt linking, without latest-cache writes."""
from contextlib import closing
from datetime import datetime
import json
import sqlite3
from typing import Any, Callable

from app.db.bootstrap import connect_metadata_index
from app.domain import prepared_review
from app.domain.review_offline import ADAPTER, VERSION, MODEL, MODE, Execute
from app.domain.review_preparation import encoded, sha
from app.domain.review_range_input import build_packet
from app.repositories import ai_review_drafts, review_preparations
from app.repositories.files import iso_now_utc


def checkpoint(phase: str) -> None:
    """Bounded failure seam: neither receipt nor replay may survive rollback."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_review_offline_results'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS paper_review_offline_results (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, paper_version_id TEXT NOT NULL, preparation_id TEXT NOT NULL,
        actor_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        UNIQUE(topic_id,paper_version_id,actor_id,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS review_offline_preparation ON paper_review_offline_results(topic_id,paper_version_id,preparation_id,actor_id,sequence)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS review_offline_no_{operation.lower()}
            BEFORE {operation} ON paper_review_offline_results
            BEGIN SELECT RAISE(ABORT,'review_offline_immutable'); END""")


def _identity(topic: str, version: str, actor: str, request: str) -> str:
    return "roffline_" + sha(encoded(["REVIEW_OFFLINE_RESULT_V1", topic, version, actor, request]))


def _read(c: sqlite3.Connection, row: sqlite3.Row, preparation: dict[str, Any],
          occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    try:
        envelope = json.loads(row["payload_json"])
        if (not isinstance(envelope, dict) or set(envelope) != {"schema_version", "topic_id", "paper_version_id", "request", "result"}
                or sha(encoded(envelope)) != row["payload_sha256"] or envelope["schema_version"] != "REVIEW_OFFLINE_RESULT_V1"):
            raise ValueError()
        body = Execute.model_validate(envelope["request"], strict=True)
        result = envelope["result"]
        if (not isinstance(result, dict) or set(result) != {"execution_id", "preparation_id", "receipt_id", "actor_id", "created_at", "state", "mode", "adapter_id", "adapter_version"}
                or any(envelope[key] != row[key] or envelope[key] != preparation[key] for key in ("topic_id", "paper_version_id"))
                or any(result[key] != row[key] for key in ("execution_id", "preparation_id", "actor_id"))
                or result["preparation_id"] != preparation["preparation_id"]
                or body.request_id != row["request_id"] or body.expected_preparation_digest != sha(encoded(preparation))
                or result["execution_id"] != _identity(row["topic_id"], row["paper_version_id"], row["actor_id"], body.request_id)
                or result["state"] != "RESULT_SAVED" or result["mode"] != MODE
                or result["adapter_id"] != ADAPTER or result["adapter_version"] not in {"1", "2"}
                or datetime.fromisoformat(result["created_at"]).tzinfo is None):
            raise ValueError()
        receipt = ai_review_drafts.get_input_receipt_in(c, row["topic_id"], result["receipt_id"], occurrences=occurrences)
        if (receipt is None or receipt["packet"] != build_packet(preparation)
                or receipt["input_origin"] != "FIXTURE_SYNTHETIC"
                or receipt["validation"].get("execution") != {"mode": MODE, "adapter_id": ADAPTER, "adapter_version": result["adapter_version"],
                                                            "scientific_assessment": "NOT_ASSESSED"}):
            raise ValueError()
        if result["adapter_version"] == "2":
            prepared_review.validate_record(receipt, receipt["packet"])
            contract = receipt["validation"]["prepared_execution"]["contract"]
            if contract != prepared_review.prepare(receipt["packet"], provider_name=ADAPTER, model=MODEL, schema_version=contract["schema_version"]):
                raise ValueError()
        return envelope
    except (ValueError, KeyError, TypeError, IndexError, RecursionError):
        raise ValueError("review_offline_integrity_invalid") from None


def status(topic: str, version: str, preparation_id: str, actor: str,
           occurrences: list[tuple[str, str]]) -> tuple[str, dict[str, Any] | None]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        preparation = review_preparations.detail_in(c, topic, version, preparation_id, occurrences)
        latest = None
        if _exists(c):
            row = c.execute("""SELECT * FROM paper_review_offline_results
                WHERE topic_id=? AND paper_version_id=? AND preparation_id=? AND actor_id=?
                ORDER BY sequence DESC LIMIT 1""", (topic, version, preparation_id, actor)).fetchone()
            if row is not None:
                latest = _read(c, row, preparation, occurrences)["result"]
        return sha(encoded(preparation)), latest


def save(topic: str, version: str, preparation_id: str, actor: str, body: Execute,
         occurrences: list[tuple[str, str]], factory: Callable[[dict[str, Any]], dict[str, Any]] | None) -> dict[str, Any]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        preparation = review_preparations.detail_in(c, topic, version, preparation_id, occurrences)
        if body.expected_preparation_digest != sha(encoded(preparation)):
            raise ValueError("review_offline_preparation_changed")
        if _exists(c):
            row = c.execute("""SELECT * FROM paper_review_offline_results
                WHERE topic_id=? AND paper_version_id=? AND actor_id=? AND request_id=?""",
                (topic, version, actor, body.request_id)).fetchone()
            if row is not None:
                if row["preparation_id"] != preparation_id:
                    raise ValueError("review_offline_request_conflict")
                saved = _read(c, row, preparation, occurrences)
                if saved["request"] != body.model_dump():
                    raise ValueError("review_offline_request_conflict")
                return saved["result"]
        if factory is None:
            raise ValueError("review_offline_disabled")
        packet = build_packet(preparation)
        # Only the reserved pure local adapter runs in this transaction. Never
        # put a network/subprocess Provider here; that requires durable dispatch.
        record = factory(packet)
        if packet != build_packet(preparation):
            raise ValueError("review_offline_input_changed")
        receipt = ai_review_drafts.append_input_receipt_in(c, record, input_packet=packet, input_origin="FIXTURE_SYNTHETIC")
        checkpoint("after_receipt")
        result = dict(execution_id=_identity(topic, version, actor, body.request_id), preparation_id=preparation_id,
                      receipt_id=receipt["receipt_id"], actor_id=actor, created_at=iso_now_utc(), state="RESULT_SAVED",
                      mode=MODE, adapter_id=ADAPTER, adapter_version=VERSION)
        envelope = dict(schema_version="REVIEW_OFFLINE_RESULT_V1", topic_id=topic, paper_version_id=version,
                        request=body.model_dump(), result=result)
        payload = encoded(envelope)
        _initialize(c)
        c.execute("""INSERT INTO paper_review_offline_results
            (execution_id,topic_id,paper_version_id,preparation_id,actor_id,request_id,payload_json,payload_sha256)
            VALUES (?,?,?,?,?,?,?,?)""", (result["execution_id"], topic, version, preparation_id, actor, body.request_id,
                                        payload, sha(payload)))
        checkpoint("after_result")
        row = c.execute("SELECT * FROM paper_review_offline_results WHERE execution_id=?", (result["execution_id"],)).fetchone()
        return _read(c, row, preparation, occurrences)["result"]
