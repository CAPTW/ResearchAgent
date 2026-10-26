from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain.journal_exchange import preserved_json
from app.domain.review_input import PACKET_VERSION, validate_packet
from app.repositories.files import iso_now_utc

AI_REVIEW_STATUSES = {"accepted", "needs_review", "rejected"}
AI_REVIEW_LEVELS = {"abstract_only", "full_text_grounded"}
INPUT_ORIGINS = {"FIXTURE_SYNTHETIC", "PROVIDER_RESPONSE", "MANUAL_IMPORT_UNVERIFIED"}
INPUT_RECEIPT_SCHEMA = "AI_REVIEW_INPUT_RECEIPT_V1"
# Only validation/save metadata is volatile. Do not recursively strip fields
# from normalized model output or change any text/codepoint in the packet.
_VOLATILE_VALIDATION_FIELDS = {"timestamp", "validated_at", "created_at", "recorded_at", "imported_at", "input_receipt_id"}


def _json_payload(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode_json_payload(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _row_to_draft(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["draft"] = _decode_json_payload(data.pop("draft_json", "{}"), {})
    data["validation"] = _decode_json_payload(data.pop("validation_json", "{}"), {})
    return data


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    topic_id = str(record.get("topic_id") or "").strip()
    run_id = str(record.get("run_id") or "").strip()
    paper_id = str(record.get("paper_id") or "").strip()
    if not topic_id:
        raise ValueError("topic_id is required")
    if not run_id:
        raise ValueError("run_id is required")
    if not paper_id:
        raise ValueError("paper_id is required")

    review_level = str(record.get("review_level") or "").strip()
    if review_level not in AI_REVIEW_LEVELS:
        raise ValueError(f"invalid review_level: {review_level}")
    status = str(record.get("status") or "").strip()
    if status not in AI_REVIEW_STATUSES:
        raise ValueError(f"invalid AI review draft status: {status}")

    now = iso_now_utc()
    return {
        "topic_id": topic_id,
        "run_id": run_id,
        "paper_id": paper_id,
        "source_text_sha256": str(record.get("source_text_sha256") or "").strip(),
        "review_level": review_level,
        "status": status,
        "draft_json": _json_payload(record.get("draft"), {}),
        "validation_json": _json_payload(record.get("validation"), {}),
        "created_at": str(record.get("created_at") or "").strip() or now,
        "imported_at": str(record.get("imported_at") or "").strip() or now,
    }


def _receipt_content(payload: dict[str, Any], packet: dict[str, Any], origin: str | None) -> dict[str, Any]:
    if origin not in INPUT_ORIGINS:
        raise ValueError("review_input_origin_invalid")
    scope = validate_packet(packet)
    # Existing abstract-only drafts use an empty SQLite key for an absent
    # source hash. Preserve the packet's None and only bridge that legacy case.
    packet_source_hash = packet.get("source_text_sha256")
    bound_source_hash = "" if packet_source_hash is None else packet_source_hash
    if (packet.get("packet_version") not in {PACKET_VERSION, "ai-review-packet-v3", "ai-review-packet-v4", "ai-review-packet-v5"}
            or any(packet.get(key) != payload[key] for key in ("topic_id", "paper_id"))
            or bound_source_hash != payload["source_text_sha256"]
            or scope.get("source", {}).get("run_id") != payload["run_id"]):
        raise ValueError("review_input_binding_invalid")
    draft = json.loads(payload["draft_json"])
    validation = json.loads(payload["validation_json"])
    if not isinstance(draft, dict) or not isinstance(validation, dict):
        raise ValueError("review_input_result_invalid")
    validation = {key: value for key, value in validation.items() if key not in _VOLATILE_VALIDATION_FIELDS}
    content = dict(schema_version=INPUT_RECEIPT_SCHEMA, input_origin=origin,
                   **{key: payload[key] for key in ("topic_id", "run_id", "paper_id", "source_text_sha256", "review_level", "status")},
                   packet=packet, draft=draft, validation=validation)
    execution = validation.get("execution")
    structured_offline = (isinstance(execution, dict) and execution.get("adapter_id") == "xrml_offline_review"
                          and execution.get("adapter_version") == "2")
    if "prepared_execution" in validation or structured_offline:
        # Lazy import avoids the receipt / review-validator initialization cycle.
        # Old V1/V2 and V3 records without this new contract remain unchanged.
        from app.domain.prepared_review import validate_record
        validate_record(content, packet)
        # Removing an unsigned execution marker must not turn this reserved
        # synthetic target into a Provider response. Manual imports remain
        # explicitly unverified and keep their existing compatibility path.
        if (origin == "PROVIDER_RESPONSE"
                and validation["prepared_execution"]["contract"]["provider_name"] == "xrml_offline_review"):
            raise ValueError("paper_offline_transport_invalid")
    codex_v2_manifest = (isinstance(execution, dict) and isinstance(execution.get("manifest"), dict)
                         and execution["manifest"].get("schema_version") == "CODEX_PREPARED_REVIEW_CLI_V2")
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-codex-prepared-review"
                                      or execution.get("schema_version") in {"PREPARED_REVIEW_CLI_TRANSPORT_V1", "PREPARED_REVIEW_CLI_TRANSPORT_V2"}
                                      or codex_v2_manifest):
        if execution.get("schema_version") == "PREPARED_REVIEW_CLI_TRANSPORT_V1" and execution.get("adapter_version") == "1":
            from app.services.providers.codex_prepared_review_plan import validate_transport
            validate_transport(content, packet)
        elif execution.get("schema_version") == "PREPARED_REVIEW_CLI_TRANSPORT_V2" and execution.get("adapter_version") == "2":
            from app.services.providers.codex_prepared_review_plan_v2 import validate_transport as validate_codex_v2_transport
            validate_codex_v2_transport(content, packet)
        else:
            raise ValueError("paper_codex_transport_invalid")
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-offline-prepared-review"
                                      or execution.get("schema_version") == "PREPARED_REVIEW_OFFLINE_TRANSPORT_V1"):
        from app.services.providers.offline_prepared_review_plan import validate_transport as validate_offline_transport
        validate_offline_transport(content, packet)
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-openai-prepared-review"
                                      or execution.get("schema_version") == "PREPARED_REVIEW_HTTP_TRANSPORT_V1"):
        from app.services.providers.openai_prepared_review_plan import validate_transport as validate_http_transport
        validate_http_transport(content, packet)
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-xai-prepared-review"
                                      or execution.get("schema_version") == "PREPARED_REVIEW_XAI_HTTP_TRANSPORT_V1"):
        from app.services.providers.xai_prepared_review import validate_transport as validate_xai_transport
        validate_xai_transport(content, packet)
    anthropic_v2_manifest = (isinstance(execution, dict) and isinstance(execution.get("manifest"), dict)
                             and execution["manifest"].get("schema_version") == "ANTHROPIC_PREPARED_REVIEW_WIRE_V2")
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-anthropic-prepared-review"
                                      or execution.get("schema_version") in {"PREPARED_REVIEW_ANTHROPIC_HTTP_TRANSPORT_V1", "PREPARED_REVIEW_ANTHROPIC_HTTP_TRANSPORT_V2"}
                                      or anthropic_v2_manifest):
        from app.services.providers.anthropic_prepared_review import validate_transport as validate_anthropic_transport
        from app.services.providers.anthropic_prepared_review import validate_origin as validate_anthropic_origin
        validate_anthropic_transport(content, packet)
        validate_anthropic_origin(execution["manifest"], origin)
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-claude-prepared-review"
                                      or execution.get("schema_version") == "PREPARED_REVIEW_CLAUDE_CLI_TRANSPORT_V1"):
        from app.services.providers.claude_prepared_review_plan import validate_transport as validate_claude_transport
        validate_claude_transport(content, packet)
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-antigravity-prepared-review"
                                      or execution.get("schema_version") == "PREPARED_REVIEW_ANTIGRAVITY_CLI_TRANSPORT_V1"):
        from app.services.providers.antigravity_prepared_review_plan import validate_transport as validate_antigravity_transport
        validate_antigravity_transport(content, packet)
    if isinstance(execution, dict) and (execution.get("adapter_id") == "xrml-grok-prepared-review"
                                      or execution.get("schema_version") == "PREPARED_REVIEW_GROK_ACP_TRANSPORT_V1"):
        from app.services.providers.grok_prepared_review_plan import validate_transport as validate_grok_transport
        validate_grok_transport(content, packet)
    return content


def _receipt_id(content: dict[str, Any]) -> str:
    return "aireceipt_" + hashlib.sha256(preserved_json(content).encode("utf-8")).hexdigest()


def _initialize_history(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS ai_review_input_history (
        receipt_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,
        input_origin TEXT NOT NULL, topic_id TEXT NOT NULL, run_id TEXT NOT NULL,
        paper_id TEXT NOT NULL, source_text_sha256 TEXT NOT NULL,
        review_level TEXT NOT NULL, status TEXT NOT NULL,
        packet_json TEXT NOT NULL, draft_json TEXT NOT NULL, validation_json TEXT NOT NULL
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_ai_review_input_topic
        ON ai_review_input_history(topic_id, paper_id, run_id, recorded_at)""")
    for operation in ("UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS ai_review_input_no_{operation.lower()}
            BEFORE {operation} ON ai_review_input_history
            BEGIN SELECT RAISE(ABORT,'ai_review_input_immutable'); END""")


def _history_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_review_input_history'"
    ).fetchone() is not None


def _row_to_receipt(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    raw = dict(row)
    try:
        content = _receipt_content(raw, json.loads(raw["packet_json"]), raw["input_origin"])
        if _receipt_id(content) != raw["receipt_id"] or json.loads(raw["validation_json"]) != content["validation"]:
            raise ValueError("review_input_receipt_corrupt")
        return dict(receipt_id=raw["receipt_id"], recorded_at=raw["recorded_at"], **content)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError("review_input_receipt_corrupt") from None


def append_input_receipt_in(
    connection: sqlite3.Connection, record: dict[str, Any], *, input_packet: dict[str, Any], input_origin: str,
) -> dict[str, Any]:
    """Append in the caller's transaction without updating the latest-draft cache."""
    if not connection.in_transaction:
        raise ValueError("review_input_transaction_required")
    payload = _normalize_record(record)
    content = _receipt_content(payload, input_packet, input_origin)
    identity = _receipt_id(content)
    _initialize_history(connection)
    connection.execute("""INSERT INTO ai_review_input_history (
        receipt_id,recorded_at,input_origin,topic_id,run_id,paper_id,source_text_sha256,
        review_level,status,packet_json,draft_json,validation_json
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(receipt_id) DO NOTHING""", (
        identity, iso_now_utc(), input_origin, payload["topic_id"], payload["run_id"], payload["paper_id"],
        payload["source_text_sha256"], payload["review_level"], payload["status"],
        preserved_json(content["packet"]), preserved_json(content["draft"]), preserved_json(content["validation"]),
    ))
    row = connection.execute("SELECT * FROM ai_review_input_history WHERE receipt_id=?", (identity,)).fetchone()
    if row is None:
        raise RuntimeError("AI review input receipt did not persist")
    return _row_to_receipt(row)


def upsert_draft(
    record: dict[str, Any], *, input_packet: dict[str, Any] | None = None, input_origin: str | None = None,
) -> dict[str, Any]:
    if input_packet is not None and input_packet.get("packet_version") in {"ai-review-packet-v3", "ai-review-packet-v4", "ai-review-packet-v5"}:
        raise ValueError("review_range_receipt_only")
    payload = _normalize_record(record)
    content = _receipt_content(payload, input_packet, input_origin) if input_packet is not None else None
    if input_packet is None and input_origin is not None:
        raise ValueError("review_input_binding_invalid")
    identity = _receipt_id(content) if content is not None else None
    validation = json.loads(payload["validation_json"])
    if isinstance(validation, dict):
        validation.pop("input_receipt_id", None)
        if identity is not None:
            validation["input_receipt_id"] = identity
        payload["validation_json"] = _json_payload(validation, {})
    with closing(connect_metadata_index()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if content is not None:
            _initialize_history(connection)
            connection.execute("""INSERT INTO ai_review_input_history (
                receipt_id,recorded_at,input_origin,topic_id,run_id,paper_id,source_text_sha256,
                review_level,status,packet_json,draft_json,validation_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(receipt_id) DO NOTHING""", (
                identity, iso_now_utc(), input_origin, payload["topic_id"], payload["run_id"], payload["paper_id"],
                payload["source_text_sha256"], payload["review_level"], payload["status"],
                preserved_json(content["packet"]), preserved_json(content["draft"]), preserved_json(content["validation"]),
            ))
            # A replay reuses its first receipt. Verify it before referencing it
            # from the mutable latest-draft cache, in the same transaction.
            receipt = connection.execute("SELECT * FROM ai_review_input_history WHERE receipt_id=?", (identity,)).fetchone()
            if receipt is None:
                raise RuntimeError("AI review input receipt did not persist")
            _row_to_receipt(receipt)
        connection.execute(
            """
            INSERT INTO ai_review_drafts (
              topic_id, run_id, paper_id, source_text_sha256, review_level, status,
              draft_json, validation_json, created_at, imported_at
            ) VALUES (
              :topic_id, :run_id, :paper_id, :source_text_sha256, :review_level, :status,
              :draft_json, :validation_json, :created_at, :imported_at
            )
            ON CONFLICT(topic_id, run_id, paper_id, source_text_sha256) DO UPDATE SET
              review_level = excluded.review_level,
              status = excluded.status,
              draft_json = excluded.draft_json,
              validation_json = excluded.validation_json,
              imported_at = excluded.imported_at
            """,
            payload,
        )
        row = connection.execute("""SELECT * FROM ai_review_drafts
            WHERE topic_id=:topic_id AND run_id=:run_id AND paper_id=:paper_id
                AND source_text_sha256=:source_text_sha256""", payload).fetchone()
        if row is None:
            raise RuntimeError("AI review draft upsert did not persist")
        # Reading before releasing the writer transaction returns this call's
        # result even when another result overwrites the legacy key immediately.
        stored = _row_to_draft(row)
    return stored


def get_input_receipt_in(connection: sqlite3.Connection, topic_id: str, receipt_id: str, *,
                         occurrences: list[tuple[str, str]] | None = None) -> dict[str, Any] | None:
    """Read and validate a receipt using the caller's transaction without writes."""
    if not topic_id:
        raise ValueError("review_input_topic_required")
    if occurrences == []:
        return None
    if not _history_exists(connection):
        return None
    where = "topic_id=? AND receipt_id=?"
    params: list[Any] = [topic_id, receipt_id]
    if occurrences is not None:
        # Hide unauthorized source pairs before decoding: even a corrupt hidden
        # receipt must be indistinguishable from a missing receipt to callers.
        where += f" AND (run_id,paper_id) IN (VALUES {','.join('(?,?)' for _ in occurrences)})"
        params.extend(value for pair in occurrences for value in pair)
    row = connection.execute(
        f"SELECT * FROM ai_review_input_history WHERE {where}", params
    ).fetchone()
    return _row_to_receipt(row) if row else None


def get_input_receipt(topic_id: str, receipt_id: str, *, read_only: bool = True,
                      occurrences: list[tuple[str, str]] | None = None) -> dict[str, Any] | None:
    if not topic_id:
        raise ValueError("review_input_topic_required")
    with closing(connect_metadata_index(read_only=read_only)) as connection:
        return get_input_receipt_in(connection, topic_id, receipt_id, occurrences=occurrences)


def list_input_history(
    *, topic_id: str, paper_ids: list[str] | None = None, limit: int = 50, offset: int = 0,
    occurrences: list[tuple[str, str]] | None = None, read_only: bool = True,
) -> list[dict[str, Any]]:
    if not topic_id:
        raise ValueError("review_input_topic_required")
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError("pagination_invalid")
    if paper_ids == [] or occurrences == []:
        return []
    clauses = ["topic_id=?"]
    params: list[Any] = [topic_id]
    if paper_ids is not None:
        clauses.append(f"paper_id IN ({','.join('?' for _ in paper_ids)})")
        params.extend(paper_ids)
    if occurrences is not None:
        # Match the complete authorized source pair before paginating. Row-value
        # membership also avoids SQLite's expression-depth limit for a large OR.
        clauses.append(f"(run_id,paper_id) IN (VALUES {','.join('(?,?)' for _ in occurrences)})")
        params.extend(value for pair in occurrences for value in pair)
    with closing(connect_metadata_index(read_only=read_only)) as connection:
        if not _history_exists(connection):
            return []
        rows = connection.execute(f"""SELECT * FROM ai_review_input_history WHERE {' AND '.join(clauses)}
            ORDER BY recorded_at DESC,receipt_id DESC LIMIT ? OFFSET ?""", [*params, limit, offset]).fetchall()
        return [_row_to_receipt(row) for row in rows]


def get_draft(
    topic_id: str,
    run_id: str,
    paper_id: str,
    source_text_sha256: str,
    *,
    read_only: bool = False,
) -> dict[str, Any] | None:
    with connect_metadata_index(read_only=read_only) as connection:
        row = connection.execute(
            """
            SELECT * FROM ai_review_drafts
            WHERE topic_id = ? AND run_id = ? AND paper_id = ? AND source_text_sha256 = ?
            """,
            (topic_id, run_id, paper_id, source_text_sha256 or ""),
        ).fetchone()
    return _row_to_draft(row) if row else None


def list_drafts(
    *,
    topic_id: str | None = None,
    run_id: str | None = None,
    paper_ids: list[str] | None = None,
    status: str | None = None,
    limit: int = 100,
    read_only: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if topic_id:
        clauses.append("topic_id = ?")
        params.append(topic_id)
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if paper_ids:
        placeholders = ", ".join("?" for _ in paper_ids)
        clauses.append(f"paper_id IN ({placeholders})")
        params.extend(paper_ids)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect_metadata_index(read_only=read_only) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM ai_review_drafts
            {where_sql}
            ORDER BY imported_at DESC, paper_id ASC
            LIMIT ?
            """,
            [*params, max(1, int(limit))],
        ).fetchall()
    return [_row_to_draft(row) for row in rows]
