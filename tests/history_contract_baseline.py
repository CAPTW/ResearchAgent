"""Fail-closed access to finite, source-provenanced historical test baselines."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

FIXTURE = Path(__file__).parent / "fixtures" / "history_contracts" / "history_contracts.json"
EXPECTED_ENTRIES_SHA256 = "8048c66f98fb845e2a76a5026b11b1b6631f98e6ec699d0da0b30f970757475f"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
URL = re.compile(r"https?://[^\s`'\")<>]+")
USES = {
    "raw_byte_identity",
    "parsed_url_differential",
    "parsed_document_role_map",
    "text_line_differential",
}


def _fail(message: str) -> NoReturn:
    raise AssertionError(f"historical baseline contract: {message}")


@lru_cache(maxsize=1)
def _entries() -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"fixture cannot be read: {exc}")
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        _fail("unsupported fixture schema")
    entries = document.get("entries")
    if not isinstance(entries, dict) or not entries:
        _fail("fixture entries are absent")
    canonical = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if document.get("entries_sha256") != digest or digest != EXPECTED_ENTRIES_SHA256:
        _fail("fixture entries checksum mismatch")
    typed = cast(dict[str, dict[str, Any]], entries)
    aliases: dict[str, str] = {}
    for key, entry in typed.items():
        commit = entry.get("source_commit")
        path = entry.get("source_path")
        use = entry.get("use")
        if entry.get("fixture_schema_version") != 1:
            _fail(f"entry schema mismatch: {key}")
        if not isinstance(commit, str) or HEX40.fullmatch(commit) is None:
            _fail(f"malformed source commit: {key}")
        if not isinstance(path, str) or key != f"{commit}:{path}":
            _fail(f"entry identity mismatch: {key}")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or "\\" in path:
            _fail(f"unsafe source path: {key}")
        for field in ("source_tree", "source_blob"):
            value = entry.get(field)
            if not isinstance(value, str) or HEX40.fullmatch(value) is None:
                _fail(f"malformed {field}: {key}")
        raw_sha256 = entry.get("raw_sha256")
        if not isinstance(raw_sha256, str) or HEX64.fullmatch(raw_sha256) is None:
            _fail(f"malformed raw_sha256: {key}")
        size = entry.get("raw_byte_size")
        if type(size) is not int or size < 0:  # bool is deliberately not an integer here
            _fail(f"invalid raw byte size: {key}")
        source_mode = entry.get("source_mode")
        if source_mode is not None and source_mode not in {"100644", "100755"}:
            _fail(f"invalid source mode: {key}")
        requests = entry.get("baseline_request_commits", [])
        if (
            not isinstance(requests, list)
            or requests != sorted(set(requests))
            or not all(isinstance(value, str) and HEX40.fullmatch(value) for value in requests)
            or commit in requests
        ):
            _fail(f"invalid baseline request commits: {key}")
        for request_commit in requests:
            alias = f"{request_commit}:{path}"
            if alias in typed or alias in aliases:
                _fail(f"duplicate baseline request identity: {alias}")
            aliases[alias] = key
        if use not in USES:
            _fail(f"unsupported baseline use: {key}")
        _validate_semantic_shape(key, entry)
    return typed


def _validate_semantic_shape(key: str, entry: dict[str, Any]) -> None:
    use = entry["use"]
    semantic = entry.get("semantic")
    if use == "raw_byte_identity":
        if semantic is not None:
            _fail(f"unexpected semantic payload: {key}")
        return
    if not isinstance(semantic, dict):
        _fail(f"missing semantic payload: {key}")
    if use == "parsed_url_differential":
        urls = semantic.get("urls")
        if not isinstance(urls, list) or not all(isinstance(value, str) for value in urls):
            _fail(f"invalid URL payload: {key}")
        if urls != sorted(set(urls)):
            _fail(f"non-canonical URL payload: {key}")
    elif use == "parsed_document_role_map":
        documents = semantic.get("documents")
        if not isinstance(documents, dict) or not all(
            isinstance(path, str) and isinstance(role, str) for path, role in documents.items()
        ):
            _fail(f"invalid document-role payload: {key}")
    else:
        hashes = semantic.get("line_sha256")
        indexes = semantic.get("absolute_link_line_indexes")
        if not isinstance(hashes, list) or not all(
            isinstance(value, str) and HEX64.fullmatch(value) for value in hashes
        ):
            _fail(f"invalid line-hash payload: {key}")
        if not isinstance(indexes, list) or not all(
            type(value) is int and 0 <= value < len(hashes) for value in indexes
        ):
            _fail(f"invalid absolute-link indexes: {key}")


def _git(repo: str, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args], input=input_bytes, capture_output=True, check=False
        )
    except OSError as exc:
        _fail(f"Git executable failure for {args!r}: {exc}")


def _stdout(repo: str, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = _git(repo, *args, input_bytes=input_bytes)
    if result.returncode:
        _fail(
            f"Git command failed ({result.returncode}) for {args!r}: "
            + result.stderr.decode("utf-8", "replace").strip()
        )
    return result.stdout


def _commit_entries(commit: str) -> list[dict[str, Any]]:
    rows = [entry for entry in _entries().values() if entry["source_commit"] == commit]
    if not rows:
        _fail(f"commit has no fixture entries: {commit}")
    return rows


def _verify_payload(entry: dict[str, Any], raw: bytes) -> None:
    key = f"{entry['source_commit']}:{entry['source_path']}"
    if len(raw) != entry["raw_byte_size"] or hashlib.sha256(raw).hexdigest() != entry["raw_sha256"]:
        _fail(f"historical bytes disagree with fixture provenance: {key}")
    use = entry["use"]
    if use == "raw_byte_identity":
        return
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"semantic baseline is not UTF-8: {key}: {exc}")
    semantic = entry["semantic"]
    if use == "parsed_url_differential":
        actual: object = sorted(set(URL.findall(text)))
        expected: object = semantic["urls"]
    elif use == "parsed_document_role_map":
        actual = json.loads(text)["documents"]
        expected = semantic["documents"]
    else:
        lines = text.splitlines()
        actual = {
            "line_sha256": [hashlib.sha256(line.encode("utf-8")).hexdigest() for line in lines],
            "absolute_link_line_indexes": [i for i, line in enumerate(lines) if "](/" in line],
        }
        expected = semantic
    if actual != expected:
        _fail(f"historical semantics disagree with fixture: {key}")


def _verify_live_commit(repo: str, commit: str) -> None:
    entries = _commit_entries(commit)
    trees = {entry["source_tree"] for entry in entries}
    if len(trees) != 1:
        _fail(f"fixture has conflicting source trees: {commit}")
    actual_tree = _stdout(repo, "show", "-s", "--format=%T", commit).decode("ascii").strip()
    if actual_tree not in trees:
        _fail(f"live commit tree disagrees with fixture: {commit}")
    tree_rows = _stdout(repo, "ls-tree", "-r", "-z", commit).split(b"\0")
    tree: dict[str, tuple[str, str, str]] = {}
    for row in tree_rows:
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split(" ")
        tree[raw_path.decode("utf-8")] = (mode, kind, blob)
    for entry in entries:
        found = tree.get(entry["source_path"])
        expected_mode = entry.get("source_mode")
        if (
            found is None
            or found[1:] != ("blob", entry["source_blob"])
            or (expected_mode is not None and found[0] != expected_mode)
        ):
            _fail(f"live historical path/blob mismatch: {commit}:{entry['source_path']}")
    request = b"".join(entry["source_blob"].encode("ascii") + b"\n" for entry in entries)
    output = _stdout(repo, "cat-file", "--batch", input_bytes=request)
    offset = 0
    for entry in entries:
        end_header = output.find(b"\n", offset)
        expected_header = (
            f"{entry['source_blob']} blob {entry['raw_byte_size']}".encode("ascii")
        )
        if end_header < 0 or output[offset:end_header] != expected_header:
            _fail(f"unexpected cat-file batch header: {commit}:{entry['source_path']}")
        start = end_header + 1
        end = start + entry["raw_byte_size"]
        if end >= len(output) or output[end : end + 1] != b"\n":
            _fail(f"truncated cat-file batch payload: {commit}:{entry['source_path']}")
        _verify_payload(entry, output[start:end])
        offset = end + 1
    if offset != len(output):
        _fail(f"unexpected trailing cat-file batch output: {commit}")


@lru_cache(maxsize=None)
def _commit_mode(repo: str, commit: str) -> str:
    if HEX40.fullmatch(commit) is None:
        _fail(f"malformed requested commit: {commit}")
    _commit_entries(commit)
    if _stdout(repo, "rev-parse", "--is-inside-work-tree").strip() != b"true":
        _fail("path is not a Git worktree")
    _stdout(repo, "cat-file", "-e", "HEAD^{commit}")
    _stdout(repo, "cat-file", "-e", "HEAD^{tree}")
    shallow = _stdout(repo, "rev-parse", "--is-shallow-repository").strip()
    if shallow not in {b"true", b"false"}:
        _fail("Git returned an invalid shallow-repository status")
    available = _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    if available.returncode == 0:
        _verify_live_commit(repo, commit)
        return "live"
    expected = f"fatal: Not a valid object name {commit}^{{commit}}\n".encode("ascii")
    stderr = available.stderr.replace(b"\r\n", b"\n")
    if available.returncode != 128 or stderr != expected:
        _fail(f"unexpected commit-availability failure for {commit}: {stderr!r}")
    if shallow != b"true":
        _fail(f"historical commit is absent from a non-shallow repository: {commit}")
    return "fixture"


def _resolve_entry(
    entries: dict[str, dict[str, Any]], commit: str, path: str
) -> dict[str, Any]:
    key = f"{commit}:{path}"
    entry = entries.get(key)
    if entry is None:
        matches = [
            candidate
            for candidate in entries.values()
            if candidate.get("source_path") == path
            and commit in candidate.get("baseline_request_commits", [])
        ]
        if len(matches) > 1:
            _fail(f"baseline key is ambiguous: {key}")
        entry = matches[0] if matches else None
    if entry is None:
        _fail(f"baseline key is not tracked: {key}")
    return entry


def _entry(repo: Path, commit: str, path: str) -> dict[str, Any]:
    entry = _resolve_entry(_entries(), commit, path)
    _commit_mode(str(repo), entry["source_commit"])
    return entry


def current_bytes_match(repo: Path, commit: str, path: str) -> bool:
    entry = _entry(repo, commit, path)
    try:
        raw = (repo / path).read_bytes()
    except OSError as exc:
        _fail(f"current path cannot be read: {path}: {exc}")
    return len(raw) == entry["raw_byte_size"] and hashlib.sha256(raw).hexdigest() == entry["raw_sha256"]


def historical_urls(repo: Path, commit: str, path: str) -> frozenset[str]:
    entry = _entry(repo, commit, path)
    if entry["use"] != "parsed_url_differential":
        _fail(f"baseline is not a URL differential: {commit}:{path}")
    return frozenset(entry["semantic"]["urls"])


def historical_documents(repo: Path, commit: str, path: str) -> dict[str, str]:
    entry = _entry(repo, commit, path)
    if entry["use"] != "parsed_document_role_map":
        _fail(f"baseline is not a document-role map: {commit}:{path}")
    return dict(entry["semantic"]["documents"])


def historical_line_contract(
    repo: Path, commit: str, path: str
) -> tuple[tuple[str, ...], frozenset[int]]:
    entry = _entry(repo, commit, path)
    if entry["use"] != "text_line_differential":
        _fail(f"baseline is not a line differential: {commit}:{path}")
    semantic = entry["semantic"]
    return tuple(semantic["line_sha256"]), frozenset(semantic["absolute_link_line_indexes"])
