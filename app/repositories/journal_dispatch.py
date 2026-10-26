"""Atomic manual admission and per-execution capacity reservation.

Every prior attempt counts, including older unregistered attempts and known
unstarted failures. Capacity is never refunded or confused with actual usage/cost.
No new table or mutable counter; the immutable LEASED event is the reservation.
"""
from contextlib import closing
from datetime import datetime
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_dispatch as domain
from app.repositories import journal_executions as executions
from app.repositories import journal_authority as authority


def _replayed(events: list[dict[str, Any]], body: domain.Dispatch, actor: str) -> bool:
    for event in events:
        receipt = event.get("dispatch")
        if receipt and receipt["request"]["request_id"] == body.request_id:
            if receipt["request"] != body.model_dump() or receipt["actor_id"] != actor:
                raise ValueError("journal_dispatch_conflict")
            return True
    return False


def replay(topic: str, job_id: str, execution_id: str, body: domain.Dispatch, actor: str) -> dict[str, Any] | None:
    saved = executions.detail(topic, job_id, execution_id)
    return saved if _replayed(saved["events"], body, actor) else None


def admit(topic: str, job_id: str, execution_id: str, body: domain.Dispatch, actor: str,
          grant: domain.Grant | domain.AuthorityReference, binding: domain.AdapterBinding, *, now: datetime | None = None,
          lease_seconds: int = 120, adapter_manifest: dict[str, Any] | None = None) -> tuple[dict[str, Any], executions.Claim | None]:
    try:
        return _admit(topic, job_id, execution_id, body, actor, grant, binding, now=now,
                      lease_seconds=lease_seconds, adapter_manifest=adapter_manifest)
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY:
            raise
        # The failed transaction has closed. A competing admission may already
        # have committed this exact request. Read once; never retry the write or
        # return a new claim. Missing, conflicting or unreadable receipts fail.
        saved = replay(topic, job_id, execution_id, body, actor)
        if saved is not None:
            return saved, None
        raise


def _admit(topic: str, job_id: str, execution_id: str, body: domain.Dispatch, actor: str,
           grant: domain.Grant | domain.AuthorityReference, binding: domain.AdapterBinding, *, now: datetime | None = None,
           lease_seconds: int = 120, adapter_manifest: dict[str, Any] | None = None) -> tuple[dict[str, Any], executions.Claim | None]:
    stamp = executions._time(now)
    with closing(connect_metadata_index()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        contract, events = executions._read(c, topic, job_id, execution_id)
        if _replayed(events, body, actor):
            return executions._public(contract, events), None  # Never reissue a claim token.
        current = events[-1] if events else executions._initial()
        if current["sequence"] != body.expected_sequence:
            raise ValueError("journal_dispatch_conflict")
        ref = grant if isinstance(grant, domain.AuthorityReference) else None
        if ref is not None:
            grant = authority.current_grant(c, contract, ref, actor, binding, stamp)
        elif authority.requires_stored(c, execution_id):
            raise ValueError("journal_dispatch_stored_authority_required")
        assert isinstance(grant, domain.Grant)
        domain.validate_grant(grant, contract, actor, binding, stamp)
        tokens = contract["specification"]["max_output_tokens"]
        attempt = current["attempt"] + 1
        if attempt > grant.max_attempts or attempt * tokens > grant.max_reserved_output_tokens:
            raise ValueError("journal_dispatch_budget_exhausted")
        fields = dict(request=body, actor_id=actor, grant=grant, binding=binding, reserved_output_tokens=tokens)
        receipt = domain.RegisteredReceipt(**fields, authority=ref) if ref is not None else domain.Receipt(**fields)
        claimed = executions._claim(c, contract, events, expected_attempt=current["attempt"],
                                    acknowledge_unknown=body.acknowledge_unknown, lease_seconds=lease_seconds,
                                    stamp=stamp, dispatch=receipt.model_dump(mode="json"), adapter_manifest=adapter_manifest)
        return executions._public(contract, events), claimed
