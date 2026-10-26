"""Explicit browser downloads -> verified local PDF -> existing Paper Bank.

The bridge carries a revocable job capability, never website cookies. Its first
implementation is local-auth only. Confirmation is always an app-user operation.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
import secrets
import tempfile
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import urlencode

from app.auth.config import get_auth_settings
from app.auth.resource_access import ResourceAction, ResourceActor, local_resource_actor, owner_user_id_for_actor
from app.domain import browser_collection as domain
from app.domain.path_safety import is_reparse_point
from app.repositories import browser_collections as repository
from app.repositories.files import iso_now_utc
from app.services import topic_profiles

_SEARCH_LOCK = Lock()
_SEARCH_LAST_ATTEMPT = 0.0


def _topic(topic: str, actor: ResourceActor) -> dict[str, Any]:
    profile = topic_profiles.get_topic_profile(topic, actor=actor)
    topic_profiles.authorize_topic_profile(profile, actor=actor, action=ResourceAction.UPDATE)
    return profile


def _access(job: dict[str, Any], actor: ResourceActor | None = None, token: str | None = None) -> ResourceActor:
    if token is not None:
        if get_auth_settings().auth_enabled or job["creator_auth_mode"] != "disabled":
            raise PermissionError("browser_collection_connection_unavailable")
        capability = job.get("capability")
        if (not capability or not isinstance(token, str) or len(token) != 43
                or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), capability["sha256"])
                or datetime.fromisoformat(capability["expires_at"]) <= datetime.now(timezone.utc)):
            raise PermissionError("browser_collection_connection_expired_or_revoked")
        actor = local_resource_actor()
    if actor is None:
        raise PermissionError("browser_collection_authorization_required")
    if actor.auth_mode != job["creator_auth_mode"] or owner_user_id_for_actor(actor) != job["owner_user_id"]:
        raise KeyError("browser_collection_not_found")
    _topic(job["topic_id"], actor)
    return actor


def _public(job: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(job)
    for key in ("capability", "owner_user_id", "creator_auth_mode", "request_digest"):
        value.pop(key, None)
    value["query_rejected"] = domain.query_contains_credentials(job["query"])
    if value["query_rejected"]:
        # Keep original evidence on disk, but never reflect a legacy unsafe paste.
        value["query"] = domain.REDACTED_QUERY
        value["request"]["query"] = domain.REDACTED_QUERY
        if isinstance(value.get("search"), dict):
            value["search"]["search_url"] = None
    capability = job.get("capability")
    value["connection"] = {"state": "DISCONNECTED" if capability is None else
        "EXPIRED" if datetime.fromisoformat(capability["expires_at"]) <= datetime.now(timezone.utc) else "ISSUED",
        "expires_at": capability["expires_at"] if capability else None}
    return value


def _item(job: dict[str, Any], item_id: str) -> dict[str, Any]:
    item = next((i for i in job["items"] if i["item_id"] == item_id), None)
    if item is None:
        raise KeyError("browser_collection_item_not_found")
    return item


def _event(item: dict[str, Any], name: str) -> None:
    item["updated_at"] = iso_now_utc()
    item["events"].append({"event": name, "at": item["updated_at"]})


def create(topic: str, request: domain.Create, actor: ResourceActor) -> dict[str, Any]:
    domain.validate_query(request.query)
    _topic(topic, actor)
    search_provenance = None
    if request.search_receipt_id:
        receipt = _search_receipt(request.search_receipt_id)
        if (receipt["query"] != request.query or any(
                candidate.model_dump() not in receipt["candidates"] for candidate in request.candidates)):
            raise ValueError("browser_collection_search_receipt_conflict")
        search_provenance = {key: receipt[key] for key in ("search_receipt_id", "source", "search_url", "retrieved_at")}
    job_id = "bc_" + domain.digest([topic, actor.auth_mode, owner_user_id_for_actor(actor), request.request_id])
    now = iso_now_utc()
    initial: dict[str, Any] = dict(schema_version=domain.SCHEMA, job_id=job_id, topic_id=topic,
        owner_user_id=owner_user_id_for_actor(actor), creator_auth_mode=actor.auth_mode,
        request=request.model_dump(), request_digest=domain.digest(request.model_dump()),
        query=request.query, allowed_hosts=request.allowed_hosts, source="arxiv", created_at=now,
        acquisition_mode="BROWSER_DOWNLOADS_API", external_ai_state="NOT_SENT", capability=None,
        metadata_binding="ARXIV_API_RECEIPT" if search_provenance else "USER_SELECTED_ARXIV_CANDIDATES",
        search=search_provenance, items=[dict(
            item_id="bci_" + domain.digest(c.arxiv_id), candidate=c.model_dump(), state="queued",
            active_attempt_id=None, attempts=[], events=[], pdf=None, saved=None, failure_code=None, updated_at=now,
        ) for c in request.candidates])
    def apply(job):
        _access(job, actor)
        if job["request_digest"] != initial["request_digest"]:
            raise ValueError("browser_collection_request_conflict")
        return _public(job)
    return repository.mutate(job_id, apply, initial=initial)


def listing(topic: str, actor: ResourceActor) -> dict[str, Any]:
    _topic(topic, actor)
    result = []
    for job in repository.listing(topic):
        try:
            _access(job, actor)
        except (KeyError, FileNotFoundError):
            continue
        result.append(_public(job))
    return {"items": result, "supported_hosts": list(domain.HOSTS), "browser": "Whale",
            "live_browser_validation": "NOT_YET_VERIFIED"}


def detail(job_id: str, actor: ResourceActor | None = None, *, token: str | None = None,
           topic: str | None = None) -> dict[str, Any]:
    job = repository.read(job_id)
    if topic is not None and job["topic_id"] != topic:
        raise KeyError("browser_collection_not_found")
    _access(job, actor, token)
    return _public(job)


def connection(topic: str, job_id: str, actor: ResourceActor) -> dict[str, str]:
    if actor.auth_mode != "disabled" or get_auth_settings().auth_enabled:
        raise PermissionError("browser_collection_connection_local_only")
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    def apply(job):
        if job["topic_id"] != topic:
            raise KeyError("browser_collection_not_found")
        _access(job, actor)
        domain.validate_query(job["query"])
        job["capability"] = {"sha256": hashlib.sha256(token.encode()).hexdigest(), "expires_at": expires}
        return dict(job_id=job_id, token=token, expires_at=expires)
    return repository.mutate(job_id, apply)


def revoke(topic: str, job_id: str, actor: ResourceActor) -> dict[str, Any]:
    def apply(job):
        if job["topic_id"] != topic:
            raise KeyError("browser_collection_not_found")
        _access(job, actor)
        job["capability"] = None
        return _public(job)
    return repository.mutate(job_id, apply)


def transition(job_id: str, item_id: str, request: domain.Transition, *, token: str) -> dict[str, Any]:
    def apply(job):
        _access(job, token=token)
        item = _item(job, item_id)
        if request.source_url is not None and request.source_url != item["candidate"]["pdf_url"]:
            raise ValueError("browser_collection_source_out_of_scope")
        if request.event == "resume":
            if item["state"] not in {"auth_required", "interrupted", "failed", "queued"}:
                raise ValueError("browser_collection_transition_conflict")
            item["state"], item["active_attempt_id"] = "queued", None
            item["failure_code"] = None
        elif request.event == "start":
            old = next((a for a in item["attempts"] if a["attempt_id"] == request.attempt_id), None)
            if old is not None:
                if item["active_attempt_id"] != request.attempt_id or item["state"] != "downloading":
                    raise ValueError("browser_collection_attempt_conflict")
                if request.download_id is not None and old["download_id"] not in (None, request.download_id):
                    raise ValueError("browser_collection_download_conflict")
                if request.download_id is not None:
                    old["download_id"] = request.download_id
            else:
                if item["state"] != "queued":
                    raise ValueError("browser_collection_transition_conflict")
                item["attempts"].append(dict(attempt_id=request.attempt_id, download_id=request.download_id,
                    source_url=item["candidate"]["pdf_url"], started_at=iso_now_utc()))
                item["active_attempt_id"], item["state"] = request.attempt_id, "downloading"
                item["failure_code"] = None
        else:
            if item["active_attempt_id"] != request.attempt_id or item["state"] not in {"downloading", request.event}:
                raise ValueError("browser_collection_transition_conflict")
            item["state"] = request.event
        _event(item, request.event)
        return _public(job)
    return repository.mutate(job_id, apply)


def control(topic: str, job_id: str, item_id: str, request: domain.Control, actor: ResourceActor) -> dict[str, Any]:
    def apply(job):
        if job["topic_id"] != topic:
            raise KeyError("browser_collection_not_found")
        _access(job, actor)
        item = _item(job, item_id)
        if request.action == "pause":
            if item["state"] not in {"queued", "downloading", "auth_required", "interrupted", "failed"}:
                raise ValueError("browser_collection_transition_conflict")
            item["state"] = "interrupted"
        else:
            if item["state"] not in {"interrupted", "auth_required", "failed", "queued"}:
                raise ValueError("browser_collection_transition_conflict")
            item["state"], item["active_attempt_id"] = "queued", None
            item["failure_code"] = None
        _event(item, request.action)
        return _public(job)
    return repository.mutate(job_id, apply)


def _upload_attempt(job: dict[str, Any], item_id: str, token: str, attempt_id: str,
                    download_id: int, source_url: str) -> dict[str, Any]:
    _access(job, token=token)
    item = _item(job, item_id)
    if (item["active_attempt_id"] != attempt_id
            or item["state"] not in {"downloading", "awaiting_confirmation", "saved"}):
        raise ValueError("browser_collection_attempt_conflict")
    attempt = next(a for a in item["attempts"] if a["attempt_id"] == attempt_id)
    if attempt["download_id"] != download_id or type(download_id) is not int:
        raise ValueError("browser_collection_download_conflict")
    if source_url != item["candidate"]["pdf_url"]:
        raise ValueError("browser_collection_source_out_of_scope")
    return item


def _validate_pdf(raw: bytes) -> int:
    from app.domain.review_pdf_extraction import MAX_DOCUMENT_PAGES
    if not 0 < len(raw) <= domain.MAX_PDF_BYTES:
        raise ValueError("browser_collection_pdf_size_invalid")
    if not raw.startswith(b"%PDF-") or not raw.rstrip().endswith(b"%%EOF"):
        raise ValueError("browser_collection_pdf_invalid_or_incomplete")
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError:
        raise ValueError("browser_collection_pdf_engine_unavailable") from None
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            if document.needs_pass or document.is_repaired or not 1 <= document.page_count <= MAX_DOCUMENT_PAGES:
                raise ValueError()
            return document.page_count
    except Exception:
        raise ValueError("browser_collection_pdf_invalid_or_incomplete") from None


def _publish_pdf(job_id: str, sha256: str, raw: bytes) -> None:
    path = repository.checked_path(job_id, sha256 + ".pdf")
    if path.exists():
        if path.stat().st_size != len(raw) or path.read_bytes() != raw:
            raise ValueError("browser_collection_pdf_changed")
        return
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        repository.checked_path(job_id, sha256 + ".pdf")
        # Caller holds the job lock, so cooperating uploads never replace a PDF.
        os.rename(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def upload(job_id: str, item_id: str, raw: bytes, *, token: str, attempt_id: str, download_id: int,
           source_url: str, pdf_sha256: str, download_state: str, download_bytes: int) -> dict[str, Any]:
    _upload_attempt(repository.read(job_id), item_id, token, attempt_id, download_id, source_url)
    if (download_state != "complete" or type(download_bytes) is not int or download_bytes != len(raw)
            or re.fullmatch(r"[a-f0-9]{64}", pdf_sha256) is None
            or hashlib.sha256(raw).hexdigest() != pdf_sha256):
        raise ValueError("browser_collection_download_incomplete")
    try:
        pages = _validate_pdf(raw)
    except ValueError as exc:
        failure = str(exc)
        def reject(job):
            item = _upload_attempt(job, item_id, token, attempt_id, download_id, source_url)
            # Never downgrade already-committed bytes on a malformed replay.
            if item["pdf"] is None:
                item["state"], item["failure_code"] = "failed", failure
                _event(item, "pdf_rejected")
        repository.mutate(job_id, reject)
        raise
    def apply(job):
        item = _upload_attempt(job, item_id, token, attempt_id, download_id, source_url)
        if item["pdf"] is not None:
            if item["pdf"]["sha256"] != pdf_sha256:
                raise ValueError("browser_collection_pdf_conflict")
            return _public(job)
        _publish_pdf(job_id, pdf_sha256, raw)
        item["pdf"] = dict(sha256=pdf_sha256, bytes=len(raw), page_count=pages, downloaded_at=iso_now_utc(),
            source_url=source_url, download_id=download_id, attempt_id=attempt_id,
            identity_binding="PENDING_USER_CONFIRMATION", extraction_state="NOT_EXTRACTED")
        item["state"] = "awaiting_confirmation"
        _event(item, "pdf_validated")
        return _public(job)
    return repository.mutate(job_id, apply)


def _save_to_papers(job: dict[str, Any], item: dict[str, Any], raw: bytes, actor: ResourceActor) -> dict[str, Any]:
    from app.domain import review_pdf_attachment
    from app.repositories import files, metadata_index, runs
    from app.services import paper_bank, review_pdf_attachment as attachments

    candidate = item["candidate"]
    same_version_other_bytes = False
    for previous in repository.listing(job["topic_id"]):
        if previous["job_id"] == job["job_id"]:
            continue
        try:
            _access(previous, actor)
        except (KeyError, FileNotFoundError):
            continue
        for old in previous["items"]:
            if old["state"] != "saved" or old["candidate"]["arxiv_id"] != candidate["arxiv_id"]:
                continue
            if old["pdf"]["sha256"] != item["pdf"]["sha256"]:
                same_version_other_bytes = True
                continue
            inventory = attachments.listing(job["topic_id"], old["saved"]["paper_version_id"], actor)
            attachment = next((a for a in inventory["attachments"]
                               if a["attachment_id"] == old["saved"]["attachment_id"]), None)
            if attachment is not None:
                _, observed = attachments.observe(attachment)
                if observed["sha256"] == item["pdf"]["sha256"]:
                    return {**old["saved"], "duplicate_of_job_id": previous["job_id"], "duplicate": True}
    run_id = "browser-" + domain.digest([job["job_id"], item["item_id"]])
    paths = runs.build_run_paths(run_id)
    for path in (paths.run_dir, paths.triage_dir, paths.run_json_path, paths.triage_dir / "triage.jsonl"):
        if any(is_reparse_point(p) for p in (*reversed(path.parents), path)):
            raise ValueError("browser_collection_store_invalid")
        if path.is_file() and path.stat().st_nlink != 1:
            raise ValueError("browser_collection_store_invalid")
    profile = _topic(job["topic_id"], actor)
    now = iso_now_utc()
    provenance = dict(collection_job_id=job["job_id"], collection_item_id=item["item_id"],
        query=job["query"], allowed_hosts=job["allowed_hosts"], collected_at=item["pdf"]["downloaded_at"],
        metadata_search=job["search"],
        pdf_sha256=item["pdf"]["sha256"], pdf_identity_binding="USER_CONFIRMED_NOT_INDEPENDENTLY_VERIFIED",
        external_ai_state="NOT_SENT")
    record = dict(run_id=run_id, topic=profile["topic"], topic_profile_id=job["topic_id"],
        status="completed", source="browser_collection", write_legacy=False,
        created_at=job["created_at"], updated_at=now, started_at=job["created_at"], finished_at=now,
        request={"source_provider": "arxiv", "collection": provenance},
        owner_user_id=job["owner_user_id"], counts={"triage_items": 1},
        artifacts={"triage_jsonl": "run:triage/triage.jsonl"},
        warnings=["Collection completed; PDF content was not extracted or reviewed by AI."],
        collection_job_id=job["job_id"], collection_item_id=item["item_id"])
    triage = dict(paper_id=candidate["arxiv_id"], source="arxiv", source_provider="arxiv",
        entry_url=candidate["source_url"], pdf_url=candidate["pdf_url"], title=candidate["title"],
        authors=candidate["authors"], published=candidate["published"], abstract=candidate["abstract"],
        collection_provenance=provenance)
    if paths.run_json_path.exists():
        existing = runs.load_run_record(run_id)
        if (existing.get("collection_job_id") != job["job_id"]
                or existing.get("collection_item_id") != item["item_id"]):
            raise ValueError("browser_collection_run_conflict")
    else:
        runs.ensure_run_layout(run_id)
    triage_path = paths.triage_dir / "triage.jsonl"
    if triage_path.exists():
        from app.repositories.jsonl import read_jsonl_strict_with_line_numbers
        if [row for _, row in read_jsonl_strict_with_line_numbers(triage_path)] != [triage]:
            raise ValueError("browser_collection_run_conflict")
    else:
        files.write_jsonl(triage_path, [triage])
    if not paths.run_json_path.exists():
        runs.save_run_record(record)
    catalog, _, _, _ = paper_bank._load_catalog(job["topic_id"], actor=actor)
    paper = next(p for p in catalog if any(o["runId"] == run_id for o in p["compatibility"]["occurrences"]))
    version = paper["paperVersionId"]
    occurrence = next(o for o in paper["compatibility"]["occurrences"] if o["runId"] == run_id)
    target = review_pdf_attachment.target_id(job["topic_id"], version, run_id, occurrence["paperId"])
    receipt = attachments.attach(job["topic_id"], version, target, item["pdf"]["sha256"], raw, actor)
    metadata_index.index_run(run_id)
    return dict(run_id=run_id, paper_version_id=version, canonical_paper_id=paper["canonicalPaperId"],
        attachment_id=receipt["attachment"]["attachment_id"],
        state="ATTACHED_NOT_EXTRACTED_OR_REVIEWED", external_ai_state="NOT_SENT", duplicate=False,
        same_version_other_bytes=same_version_other_bytes)


def confirm(topic: str, job_id: str, item_id: str, request: domain.Confirm, actor: ResourceActor) -> dict[str, Any]:
    from app.repositories.file_locks import file_lock, FileLockError
    observed = repository.read(job_id)
    if observed["topic_id"] != topic:
        raise KeyError("browser_collection_not_found")
    _access(observed, actor)
    item_key = _item(observed, item_id)["candidate"]["arxiv_id"]
    lock = repository.auxiliary_path("paper_locks", domain.digest([topic, item_key]), ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    def apply(job):
        if job["topic_id"] != topic:
            raise KeyError("browser_collection_not_found")
        _access(job, actor)
        item = _item(job, item_id)
        if (item["state"] not in {"awaiting_confirmation", "saved"} or item["pdf"] is None
                or item["pdf"]["sha256"] != request.expected_pdf_sha256):
            raise ValueError("browser_collection_confirmation_conflict")
        if item["state"] == "saved":
            return _public(job)
        path = repository.checked_path(job_id, request.expected_pdf_sha256 + ".pdf")
        if path.stat().st_size != item["pdf"]["bytes"]:
            raise ValueError("browser_collection_pdf_changed")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != request.expected_pdf_sha256:
            raise ValueError("browser_collection_pdf_changed")
        _validate_pdf(raw)
        item["saved"] = _save_to_papers(job, item, raw, actor)
        item["pdf"]["identity_binding"] = "USER_CONFIRMED_NOT_INDEPENDENTLY_VERIFIED"
        item["state"] = "saved"
        _event(item, "saved_to_papers")
        return _public(job)
    # Serialize the whole confirmation, including the saved job commit, across
    # jobs targeting this version. A second job observes the first attachment.
    try:
        with file_lock(lock):
            return repository.mutate(job_id, apply)
    except FileLockError:
        raise ValueError("browser_collection_confirmation_busy") from None


def _search_receipt(receipt_id: str) -> dict[str, Any]:
    from app.repositories.shared_state import load_json_state
    if re.fullmatch(r"bcs_[a-f0-9]{64}", receipt_id) is None:
        raise ValueError("browser_collection_search_receipt_invalid")
    path = repository.auxiliary_path("search_receipts", receipt_id[4:])
    value: dict[str, Any] = load_json_state(path, state_name="browser_collection_search", expected_type=dict, default=dict)
    try:
        if not value or "bcs_" + domain.digest({k: v for k, v in value.items() if k != "search_receipt_id"}) != receipt_id:
            raise ValueError()
        if (value["source"] != "ARXIV_OFFICIAL_API"
                or not value["search_url"].startswith("https://export.arxiv.org/api/query?")
                or datetime.fromisoformat(value["retrieved_at"]).utcoffset() is None):
            raise ValueError()
        for candidate in value["candidates"]:
            domain.Candidate.model_validate(candidate, strict=True)
        return value
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("browser_collection_search_receipt_invalid") from None


def search(topic: str, request: domain.Search, actor: ResourceActor) -> dict[str, Any]:
    global _SEARCH_LAST_ATTEMPT
    domain.validate_query(request.query)
    from app.services import retrieval
    from app.repositories import files, shared_state
    _topic(topic, actor)
    url = retrieval.ARXIV_API + "?" + urlencode(dict(search_query=request.query, start=0,
        max_results=request.max_results, sortBy="submittedDate", sortOrder="descending"))
    cache_key = domain.digest(request.model_dump())
    cache_path = repository.auxiliary_path("search_cache", cache_key)
    if not _SEARCH_LOCK.acquire(blocking=False):
        raise ValueError("browser_collection_search_busy")
    try:
        cached: dict[str, Any] = shared_state.load_json_state(cache_path, state_name="browser_collection_search_cache",
            expected_type=dict, default=dict)
        if cached:
            receipt = _search_receipt(cached.get("search_receipt_id", ""))
            if receipt["query"] != request.query or receipt["search_url"] != url:
                raise ValueError("browser_collection_search_receipt_invalid")
            if datetime.fromisoformat(receipt["retrieved_at"]).date() == datetime.now(timezone.utc).date():
                return {**receipt, "cached": True}
        if monotonic() - _SEARCH_LAST_ATTEMPT < 3:
            raise ValueError("browser_collection_search_rate_limited")
        _SEARCH_LAST_ATTEMPT = monotonic()
        try:
            entries = retrieval.parse_feed(retrieval.fetch_atom(url, timeout=20))
        except Exception:
            raise ValueError("browser_collection_search_unavailable") from None
        result = _search_candidates(request, url, entries)
        receipt_path = repository.auxiliary_path("search_receipts", result["search_receipt_id"][4:])
        files.write_json(receipt_path, result)
        files.write_json(cache_path, {"search_receipt_id": result["search_receipt_id"]})
        return {**result, "cached": False}
    finally:
        _SEARCH_LOCK.release()


def _search_candidates(request: domain.Search, url: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    rejected = 0
    for entry in entries[:request.max_results]:
        identity = str(entry.get("paper_id") or "")
        try:
            candidate = domain.Candidate(arxiv_id=identity, title=entry.get("title", ""),
                authors=entry.get("authors", []), abstract=entry.get("abstract", ""),
                published=str(entry.get("published", ""))[:10], source_url=f"https://arxiv.org/abs/{identity}",
                pdf_url=f"https://arxiv.org/pdf/{identity}")
        except ValueError:
            rejected += 1
            continue
        if not any(c["arxiv_id"] == identity for c in candidates):
            candidates.append(candidate.model_dump())
    result = dict(query=request.query, candidates=candidates, source="ARXIV_OFFICIAL_API", search_url=url,
        retrieved_at=iso_now_utc(), rejected_candidates=rejected, external_ai_state="NOT_SENT")
    result["search_receipt_id"] = "bcs_" + domain.digest(result)
    return result
