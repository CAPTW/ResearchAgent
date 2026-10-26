"""Owner-private frozen candidates and atomic human review, separate from run journals."""
from contextlib import closing
import json
import sqlite3
from typing import Any

from app.db.bootstrap import connect_metadata_index
from app.domain import journal_review as reviews, saved_analysis_journal as domain
from app.domain.journal_exchange import preserved_json, sha
from app.domain.temporal_memory import digest as review_digest
from app.repositories import saved_analyses as archives
from app.repositories.files import iso_now_utc

digest = domain.exact_digest
REVIEW_SCHEMA = 'SAVED_ANALYSIS_JOURNAL_REVIEW_V1'


def checkpoint(phase: str) -> None:
    """Failure injection before commit."""


def _exists(c: sqlite3.Connection) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE name='saved_analysis_journals' AND type='table'").fetchone() is not None


def _initialize(c: sqlite3.Connection) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS saved_analysis_journals (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL UNIQUE,
        topic_id TEXT NOT NULL, analysis_id TEXT NOT NULL, decision_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS saved_analysis_journals_scope ON saved_analysis_journals(topic_id,analysis_id,sequence)')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_analysis_journal_reviews (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE, candidate_id TEXT NOT NULL,
        previous_id TEXT, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, UNIQUE(candidate_id,request_id))''')
    for table in ('saved_analysis_journals', 'saved_analysis_journal_reviews'):
        for operation in ('UPDATE', 'DELETE'):
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'saved_analysis_journal_immutable'); END''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS saved_analysis_journal_binding BEFORE INSERT ON saved_analysis_journals
        WHEN NOT EXISTS (SELECT 1 FROM saved_analysis_captures WHERE topic_id=NEW.topic_id AND analysis_id=NEW.analysis_id)
        OR NEW.decision_id IS NOT (SELECT decision_id FROM saved_analysis_decisions WHERE topic_id=NEW.topic_id AND analysis_id=NEW.analysis_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'saved_analysis_journal_binding_invalid'); END''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS saved_analysis_journal_review_binding BEFORE INSERT ON saved_analysis_journal_reviews
        WHEN NOT EXISTS (SELECT 1 FROM saved_analysis_journals WHERE candidate_id=NEW.candidate_id)
        OR NEW.previous_id IS NOT (SELECT review_id FROM saved_analysis_journal_reviews WHERE candidate_id=NEW.candidate_id ORDER BY sequence DESC LIMIT 1)
        BEGIN SELECT RAISE(ABORT,'saved_analysis_journal_review_binding_invalid'); END''')


def _candidate(c: sqlite3.Connection, archive: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    row = c.execute('SELECT * FROM saved_analysis_journals WHERE topic_id=? AND analysis_id=? AND candidate_id=?',
        (archive['topic_id'], archive['analysis_id'], candidate_id)).fetchone() if _exists(c) else None
    if row is None:
        raise KeyError(candidate_id)
    try:
        value = json.loads(row['payload_json'])
        decision = next(d for d in archive['decisions'] if d['decision_id'] == row['decision_id'])
        body = domain.Create(expected_decision_id=row['decision_id'], component_ids=value['selected_component_ids'])
        expected = domain.assemble(archive, body, decision, value['created_at'], render=False)
        # Validate frozen semantics without invoking today's prose renderer.
        preserved_fields = {'markdown', 'content_sha256'}
        if (digest(value) != row['payload_digest'] or sha(value['markdown']) != value['content_sha256']
                or preserved_json({k: v for k, v in value.items() if k not in preserved_fields})
                != preserved_json({k: v for k, v in expected.items() if k not in preserved_fields})
                or any(value[k] != row[k] for k in ('candidate_id', 'topic_id', 'analysis_id'))):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, StopIteration):
        raise ValueError('saved_analysis_journal_integrity_invalid') from None


def _records(c: sqlite3.Connection, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    previous = None
    try:
        for row in c.execute('SELECT * FROM saved_analysis_journal_reviews WHERE candidate_id=? ORDER BY sequence', (candidate['candidate_id'],)):
            value = json.loads(row['payload_json'])
            body = reviews.Save.model_validate(value['command'])
            reviews.validate(candidate, body)
            if (digest(value) != row['payload_digest'] or value['schema_version'] != REVIEW_SCHEMA
                    or value['review_id'] != 'ajreview_' + digest([candidate['candidate_id'], body.request_id])
                    or value['review_id'] != row['review_id'] or value['previous_id'] != previous or row['previous_id'] != previous
                    or body.expected_previous_id != previous or row['request_id'] != body.request_id
                    or value['actor_id'] != candidate['owner_id']
                    or row['request_digest'] != digest([body.model_dump(), value['actor_id']])
                    or any(value[k] != candidate[k] for k in ('topic_id', 'analysis_id', 'candidate_id'))
                    or value['candidate_digest'] != body.expected_candidate_digest
                    or any(value[k] != body.model_dump()[k] for k in ('state', 'note', 'entries'))):
                raise ValueError()
            report = value['report']
            if body.state == 'accepted':
                if (report['schema_version'] != reviews.REPORT_SCHEMA or report['review_id'] != value['review_id']
                        or report['version_id'] != 'jrpt_' + review_digest([reviews.REPORT_SCHEMA, value['review_id']])
                        or report['candidate_digest'] != review_digest(candidate)
                        or sha(report['content']) != report['content_sha256']):
                    raise ValueError()
            elif report is not None:
                raise ValueError()
            result.append(value)
            previous = value['review_id']
        return result
    except (KeyError, TypeError, ValueError):
        raise ValueError('saved_analysis_journal_integrity_invalid') from None


def create(topic: str, analysis_id: str, actor: str, body: domain.Create) -> dict[str, Any]:
    body = domain.Create.model_validate(body.model_dump())
    with closing(connect_metadata_index()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        archive = archives._get(c, topic, analysis_id, actor)
        identity = domain.identity(archive, body)
        if _exists(c) and c.execute('SELECT 1 FROM saved_analysis_journals WHERE candidate_id=?', (identity,)).fetchone():
            return _candidate(c, archive, identity)
        domain.require_current(archive, body.expected_decision_id)
        value = domain.assemble(archive, body, archive['latest_decision'], iso_now_utc())
        _initialize(c)
        c.execute('INSERT INTO saved_analysis_journals(candidate_id,topic_id,analysis_id,decision_id,payload_json,payload_digest) VALUES(?,?,?,?,?,?)',
            (identity, topic, analysis_id, body.expected_decision_id, preserved_json(value), digest(value)))
        checkpoint('candidate_saved')
        return value


def components(topic: str, analysis_id: str, actor: str) -> dict[str, Any]:
    return dict(items=domain.components(archives.detail(topic, analysis_id, actor)))


def listing(topic: str, analysis_id: str, actor: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError('pagination_invalid')
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute('BEGIN')
        archive = archives._get(c, topic, analysis_id, actor)
        items, total = [], 0
        if _exists(c):
            total = c.execute('SELECT count(*) FROM saved_analysis_journals WHERE topic_id=? AND analysis_id=?', (topic, analysis_id)).fetchone()[0]
            rows = c.execute('SELECT candidate_id FROM saved_analysis_journals WHERE topic_id=? AND analysis_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?', (topic, analysis_id, limit, (page - 1) * limit)).fetchall()
            for row in rows:
                value = _candidate(c, archive, row['candidate_id'])
                items.append(dict(candidate_id=value['candidate_id'], created_at=value['created_at'], title=value['proposal']['title'],
                    decision_id=value['source']['decision']['decision_id'], selected_component_ids=value['selected_component_ids']))
        return dict(items=items, total=total, page=page, limit=limit)


def detail(topic: str, analysis_id: str, actor: str, candidate_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute('BEGIN')
        return _candidate(c, archives._get(c, topic, analysis_id, actor), candidate_id)


def history(topic: str, analysis_id: str, actor: str, candidate_id: str, page: int = 1, limit: int = 10) -> dict[str, Any]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError('pagination_invalid')
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute('BEGIN')
        candidate = _candidate(c, archives._get(c, topic, analysis_id, actor), candidate_id)
        records = _records(c, candidate)
        items = list(reversed(records))
        return dict(items=items[(page - 1)*limit:page*limit], total=len(items), page=page, limit=limit,
            latest=records[-1] if records else None, candidate_digest=review_digest(candidate), entries=reviews.entries(candidate))


def save_review(topic: str, analysis_id: str, actor: str, candidate_id: str, body: reviews.Save) -> tuple[dict[str, Any], bool]:
    body = reviews.Save.model_validate(body.model_dump())
    request_digest = digest([body.model_dump(), actor])
    with closing(connect_metadata_index()) as c, c:
        c.execute('BEGIN IMMEDIATE')
        archive = archives._get(c, topic, analysis_id, actor)
        candidate = _candidate(c, archive, candidate_id)
        records = _records(c, candidate)
        old = next((r for r in records if r['command']['request_id'] == body.request_id), None)
        if old:
            if digest([old['command'], actor]) != request_digest:
                raise ValueError('journal_review_request_conflict')
            return old, False
        domain.require_current(archive, candidate['source']['decision']['decision_id'])
        previous = records[-1]['review_id'] if records else None
        if body.expected_previous_id != previous:
            raise ValueError('journal_review_conflict')
        reviews.validate(candidate, body)
        value = dict(schema_version=REVIEW_SCHEMA, review_id='ajreview_' + digest([candidate_id, body.request_id]),
            topic_id=topic, analysis_id=analysis_id, candidate_id=candidate_id, actor_id=actor, created_at=iso_now_utc(),
            previous_id=previous, candidate_digest=review_digest(candidate), command=body.model_dump(),
            state=body.state, entries=[entry.model_dump() for entry in body.entries], note=body.note, report=None)
        if body.state == 'accepted':
            value['report'] = reviews.report(candidate, value)
        c.execute('INSERT INTO saved_analysis_journal_reviews(review_id,candidate_id,previous_id,request_id,request_digest,payload_json,payload_digest) VALUES(?,?,?,?,?,?,?)',
            (value['review_id'], candidate_id, previous, body.request_id, request_digest, preserved_json(value), digest(value)))
        checkpoint('review_saved')
        return value, True


def _review(records: list[dict[str, Any]], review_id: str) -> dict[str, Any]:
    value = next((r for r in records if r['review_id'] == review_id), None)
    if value is None:
        raise KeyError(review_id)
    return value


def review_detail(topic: str, analysis_id: str, actor: str, candidate_id: str, review_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute('BEGIN')
        candidate = _candidate(c, archives._get(c, topic, analysis_id, actor), candidate_id)
        return _review(_records(c, candidate), review_id)


def compare(topic: str, analysis_id: str, actor: str, candidate_id: str, review_id: str, from_review_id: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute('BEGIN')
        candidate = _candidate(c, archives._get(c, topic, analysis_id, actor), candidate_id)
        records = _records(c, candidate)
        before, after = _review(records, from_review_id), _review(records, review_id)
        if review_id == from_review_id:
            raise ValueError('journal_review_comparison_same')
        return reviews.comparison(candidate, before, after)


def export(topic: str, analysis_id: str, actor: str, candidate_id: str, review_id: str, format: str) -> dict[str, Any]:
    with closing(connect_metadata_index(read_only=True)) as c:
        c.execute('BEGIN')
        archive = archives._get(c, topic, analysis_id, actor)
        candidate = _candidate(c, archive, candidate_id)
        domain.require_current(archive, candidate['source']['decision']['decision_id'])
        records = _records(c, candidate)
        if not records or records[-1]['state'] != 'accepted' or records[-1]['review_id'] != review_id:
            raise ValueError('journal_review_export_not_accepted')
        review = records[-1]
        if format not in {'markdown', 'json'}:
            raise ValueError('synthesis_export_format_invalid')
        content = review['report']['content'] if format == 'markdown' else preserved_json(dict(
            schema_version='SAVED_ANALYSIS_JOURNAL_EXPORT_V1', candidate=candidate, review=review))
        return dict(content=content, content_sha256=sha(content), filename=review['report']['version_id'] + ('.md' if format == 'markdown' else '.json'),
            media_type='text/markdown; charset=utf-8' if format == 'markdown' else 'application/json')
