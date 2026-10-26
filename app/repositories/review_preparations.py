"""Append-only local input preparations on the canonical metadata connection."""
from contextlib import closing
import json
import sqlite3
from typing import Any, Callable

from app.db.bootstrap import connect_metadata_index
from app.domain import review_preparation as domain
from app.repositories.files import iso_now_utc


def checkpoint(phase: str) -> None:
    """Bounded transaction failure seam."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_review_preparations'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS paper_review_preparations (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, preparation_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, paper_version_id TEXT NOT NULL, actor_id TEXT NOT NULL, request_id TEXT NOT NULL,
        run_id TEXT NOT NULL, paper_id TEXT NOT NULL, request_json TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        UNIQUE(topic_id,paper_version_id,actor_id,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS review_preparations_topic ON paper_review_preparations(topic_id,paper_version_id,sequence)")
    for operation in ("UPDATE", "DELETE"):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS review_preparations_no_{operation.lower()}
            BEFORE {operation} ON paper_review_preparations
            BEGIN SELECT RAISE(ABORT,'review_preparation_immutable'); END""")


def _scope(topic: str, version: str, occurrences: list[tuple[str, str]]) -> tuple[str, list[Any]]:
    where = "topic_id=? AND paper_version_id=? AND (run_id,paper_id) IN (VALUES "
    where += ",".join("(?,?)" for _ in occurrences) + ")"
    return where, [topic, version, *(value for pair in occurrences for value in pair)]


def _read(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
        request_type = domain.DocumentPrepare if isinstance(payload, dict) and payload.get("schema_version") == domain.DOCUMENT_SCHEMA else domain.Prepare
        body = request_type.model_validate_json(row["request_json"], strict=True)
        value = domain.validate(payload, body)
        if (domain.sha(domain.encoded(value)) != row["payload_sha256"]
                or any(value[key] != row[key] for key in ("preparation_id", "topic_id", "paper_version_id", "actor_id"))
                or value["source"]["run_id"] != row["run_id"] or value["source"]["paper_id"] != row["paper_id"]
                or body.request_id != row["request_id"]):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, IndexError, RecursionError):
        raise ValueError("review_preparation_integrity_invalid") from None


def _replay(c: sqlite3.Connection, topic: str, version: str, actor: str, body: domain.Prepare,
            occurrences: list[tuple[str, str]]) -> dict[str, Any] | None:
    if not occurrences or not _exists(c):
        return None
    where, params = _scope(topic, version, occurrences)
    row = c.execute(f"SELECT * FROM paper_review_preparations WHERE {where} AND actor_id=? AND request_id=?",
                    (*params, actor, body.request_id)).fetchone()
    if row is None:
        return None
    value = _read(row)
    if json.loads(row["request_json"]) != body.model_dump():
        raise ValueError("review_preparation_request_conflict")
    return value


def save(topic: str, version: str, actor: str, body: domain.Prepare, occurrences: list[tuple[str, str]],
         factory: Callable[[], tuple[dict[str, Any], str]], *,
         context_factory: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        _initialize(c)
        replay = _replay(c, topic, version, actor, body, occurrences)
        if replay is not None:
            return replay
        source, text = factory()
        value = domain.build(topic, version, actor, body, source, text, iso_now_utc(),
                             context=context_factory() if context_factory is not None else None)
        payload = domain.encoded(value)
        c.execute("""INSERT INTO paper_review_preparations
            (preparation_id,topic_id,paper_version_id,actor_id,request_id,run_id,paper_id,request_json,payload_json,payload_sha256)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (value["preparation_id"], topic, version, actor, body.request_id,
            body.run_id, body.paper_id, domain.encoded(body.model_dump()), payload, domain.sha(payload)))
        checkpoint("after_insert")
        return value


def listing(topic: str, version: str, occurrences: list[tuple[str, str]], page: int, page_size: int) -> dict[str, Any]:
    if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 20:
        raise ValueError("review_preparation_request_invalid")
    offset = (page - 1) * page_size
    if offset > 9223372036854775807:
        raise ValueError("review_preparation_request_invalid")
    items = []
    if occurrences:
        with closing(connect_metadata_index(read_only=True)) as c:
            c.execute("BEGIN")
            if _exists(c):
                where, params = _scope(topic, version, occurrences)
                rows = c.execute(f"SELECT * FROM paper_review_preparations WHERE {where} ORDER BY sequence DESC LIMIT ? OFFSET ?",
                                 (*params, page_size + 1, offset)).fetchall()
                items = [domain.summary(_read(row)) for row in rows]
    return dict(items=items[:page_size], hasMore=len(items) > page_size, page=page, pageSize=page_size)


def detail(topic: str, version: str, identity: str, occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        return detail_in(c, topic, version, identity, occurrences)


def detail_in(c: sqlite3.Connection, topic: str, version: str, identity: str,
              occurrences: list[tuple[str, str]]) -> dict[str, Any]:
    """Scope before decoding, on the receipt writer's transaction snapshot."""
    if occurrences and _exists(c):
        where, params = _scope(topic, version, occurrences)
        row = c.execute(f"SELECT * FROM paper_review_preparations WHERE {where} AND preparation_id=?", (*params, identity)).fetchone()
        if row is not None:
            return _read(row)
    raise KeyError("review_preparation_not_found")
