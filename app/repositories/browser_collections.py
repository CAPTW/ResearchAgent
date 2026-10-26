"""Atomic, resumable local jobs; no credentials or browser profile copies."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Callable, TypeVar

from app.db import bootstrap
from app.domain import browser_collection as domain
from app.domain.path_safety import is_reparse_point
from app.repositories import shared_state

T = TypeVar("T")


def root() -> Path:
    return bootstrap.STORAGE_ROOT.parent / "browser_collections"


def checked_path(job_id: str, filename: str = "job.json") -> Path:
    if re.fullmatch(r"bc_[a-f0-9]{64}", job_id) is None:
        raise KeyError("browser_collection_not_found")
    path = root() / job_id / filename
    if any(is_reparse_point(p) for p in (*reversed(path.parents), path)):
        raise ValueError("browser_collection_store_invalid")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("browser_collection_store_invalid")
    return path


def auxiliary_path(namespace: str, identity: str, suffix: str = ".json") -> Path:
    if namespace not in {"search_receipts", "search_cache", "paper_locks"} or re.fullmatch(r"[a-f0-9]{64}", identity) is None:
        raise ValueError("browser_collection_store_invalid")
    path = root() / namespace / (identity + suffix)
    if any(is_reparse_point(p) for p in (*reversed(path.parents), path)):
        raise ValueError("browser_collection_store_invalid")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("browser_collection_store_invalid")
    return path


def validate(value: Any, job_id: str) -> dict[str, Any]:
    try:
        if not isinstance(value, dict) or value["schema_version"] != domain.SCHEMA or value["job_id"] != job_id:
            raise ValueError()
        request = domain.Create.model_validate(value["request"], strict=True)
        if value["request_digest"] != domain.digest(request.model_dump()):
            raise ValueError()
        if (value["job_id"] != "bc_" + domain.digest([value["topic_id"], value["creator_auth_mode"],
                value["owner_user_id"], request.request_id])
                or value["query"] != request.query or value["allowed_hosts"] != request.allowed_hosts
                or value["source"] != "arxiv" or value["acquisition_mode"] != "BROWSER_DOWNLOADS_API"
                or value["creator_auth_mode"] not in {"disabled", "oidc"}
                or not isinstance(value["topic_id"], str) or not value["topic_id"]):
            raise ValueError()
        if value["capability"] is not None:
            cap = value["capability"]
            if (set(cap) != {"sha256", "expires_at"} or re.fullmatch(r"[a-f0-9]{64}", cap["sha256"]) is None
                    or datetime.fromisoformat(cap["expires_at"]).utcoffset() is None):
                raise ValueError()
        if (value["external_ai_state"] != "NOT_SENT" or not isinstance(value["items"], list)
                or len(value["items"]) != len(request.candidates)
                or len({i["item_id"] for i in value["items"]}) != len(value["items"])):
            raise ValueError()
        for item, candidate in zip(value["items"], request.candidates):
            if (item["item_id"] != "bci_" + domain.digest(candidate.arxiv_id)
                    or item["candidate"] != candidate.model_dump() or item["state"] not in domain.STATES
                    or not isinstance(item["attempts"], list) or not isinstance(item["events"], list)):
                raise ValueError()
            attempts = item["attempts"]
            if len({a["attempt_id"] for a in attempts}) != len(attempts):
                raise ValueError()
            for attempt in attempts:
                if (re.fullmatch(domain.IDENTIFIER, attempt["attempt_id"]) is None
                        or attempt["source_url"] != candidate.pdf_url
                        or (attempt["download_id"] is not None
                            and (type(attempt["download_id"]) is not int or attempt["download_id"] < 0))):
                    raise ValueError()
            if item["active_attempt_id"] is not None and not any(
                    a["attempt_id"] == item["active_attempt_id"] for a in attempts):
                raise ValueError()
            pdf = item["pdf"]
            if pdf is not None and (re.fullmatch(r"[a-f0-9]{64}", pdf["sha256"]) is None
                    or type(pdf["bytes"]) is not int or not 0 < pdf["bytes"] <= domain.MAX_PDF_BYTES
                    or pdf["source_url"] != candidate.pdf_url
                    or pdf["attempt_id"] != item["active_attempt_id"]):
                raise ValueError()
            if item["state"] in {"awaiting_confirmation", "saved"} and pdf is None:
                raise ValueError()
            if item["state"] == "saved" and not isinstance(item["saved"], dict):
                raise ValueError()
        return value
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError("browser_collection_store_invalid") from None


def read(job_id: str) -> dict[str, Any]:
    value: dict[str, Any] = shared_state.load_json_state(checked_path(job_id), state_name="browser_collection",
        expected_type=dict, default=dict)
    if not value:
        raise KeyError("browser_collection_not_found")
    return deepcopy(validate(value, job_id))


def mutate(job_id: str, operation: Callable[[dict[str, Any]], T], *, initial: dict[str, Any] | None = None) -> T:
    path = checked_path(job_id)
    # Validate the lock too: shared-state guards its target, not every ancestor.
    checked_path(job_id, shared_state.lock_path_for(path).name)
    def apply(value):
        if not value and initial is None:
            raise KeyError("browser_collection_not_found")
        current = validate(value or deepcopy(initial), job_id)
        result = operation(current)
        validate(current, job_id)
        return current, deepcopy(result)
    return shared_state.mutate_json_state(path, state_name="browser_collection", expected_type=dict,
        default=dict, mutate=apply)


def listing(topic_id: str) -> list[dict[str, Any]]:
    if not root().exists():
        return []
    jobs = []
    for path in root().glob("bc_*/job.json"):
        job = read(path.parent.name)
        if job["topic_id"] == topic_id:
            jobs.append(job)
    return sorted(jobs, key=lambda j: (j["created_at"], j["job_id"]), reverse=True)
