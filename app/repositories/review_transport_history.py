"""Scoped transport history with authority status in one read-only snapshot."""
from contextlib import closing
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import review_execution as domain, review_transport_execution as transport
from app.repositories import review_authorities, review_executions as executions
from app.repositories.files import iso_now_utc


def _view(c: sqlite3.Connection, saved: dict[str, Any], now: str) -> dict[str, Any]:
    return dict(**saved, authority=review_authorities.describe_in(c, saved, domain.timestamp(now)))


def listing(scope: domain.Scope, page: int, page_size: int) -> dict[str, Any]:
    executions._page_start(page, page_size)
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        result = executions._listing_in(c, scope, page, page_size, schema=transport.SCHEMA)
        now = iso_now_utc()
        return dict(result, items=[_view(c, saved, now) for saved in result["items"]])


def detail(scope: domain.Scope, identity: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute("BEGIN")
        saved = executions._schema(executions._detail(c, scope, identity, executions._preparation(c, scope)), transport.SCHEMA)
        return _view(c, saved, iso_now_utc())
