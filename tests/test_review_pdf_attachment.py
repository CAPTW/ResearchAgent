"""User-selected PDF copies must preserve excerpt history and stay local."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from app.auth.resource_access import ResourceActor
from app.db import bootstrap
from app.domain import review_pdf_attachment as domain, review_pdf_extraction as extraction
from app.repositories import review_pdf_attachments as repository
from app.services import review_pdf_attachment as service

from tests import test_review_preparation
from tests.test_review_preparation_api import client

linked = test_review_preparation.linked
seeded = test_review_preparation.seeded


def root(f):
    return f'/api/topics/{f["topic"]}/paper-bank/{f["version"]}/review-preparations'


@pytest.fixture
def upload(linked):
    fitz = pytest.importorskip("fitz", reason="Locked optional fulltext dependency not installed")
    with fitz.open() as document:
        document.new_page().insert_textbox((60, 60, 550, 700), "Public synthetic paper body. " * 30)
        raw = document.tobytes()
    return {**linked, "raw": raw, "digest": hashlib.sha256(raw).hexdigest(),
            "target": domain.target_id(linked['topic'], linked['version'], linked['run'], linked['paper'])}


def attach(f):
    return service.attach(f['topic'], f['version'], f['target'], f['digest'], f['raw'], f['actor'])


def records(f):
    return repository.listing(f['topic'], f['version'], [(f['run'], f['paper'])])


def destination(f):
    return bootstrap.STORAGE_ROOT / f['run'] / domain.reference(f['digest'])[4:]


def test_rollback_after_publication_and_explicit_identical_retry(upload, monkeypatch):
    monkeypatch.setattr(repository, 'checkpoint', lambda _: (_ for _ in ()).throw(RuntimeError('link failure')))
    with pytest.raises(RuntimeError, match='link failure'):
        attach(upload)
    assert records(upload) == []
    assert destination(upload).read_bytes() == upload['raw']
    monkeypatch.setattr(repository, 'checkpoint', lambda _: None)
    first = attach(upload)
    assert first['created'] is True
    assert attach(upload) == {**first, 'created': False}
    with closing(bootstrap.connect_metadata_index()) as c:
        with pytest.raises(Exception, match='immutable'):
            c.execute('DELETE FROM paper_pdf_attachments')


def test_three_simultaneous_identical_uploads_have_one_exact_record(upload):
    barrier = Barrier(3)
    def run(_):
        barrier.wait()
        return attach(upload)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(3)))
    assert sum(r['created'] for r in results) == 1
    assert all(r['attachment'] == results[0]['attachment'] for r in results)
    assert records(upload) == [results[0]['attachment']]
    assert destination(upload).read_bytes() == upload['raw']


@pytest.mark.parametrize('kind', ['hash', 'empty', 'nonpdf', 'malformed', 'oversized', 'encrypted', 'page_limit'])
def test_invalid_pdf_rejected_without_a_file_or_record(upload, monkeypatch, kind):
    if kind == 'hash':
        upload['digest'] = '0' * 64
    elif kind in {'empty', 'nonpdf', 'malformed'}:
        upload['raw'] = {'empty': b'', 'nonpdf': b'not pdf', 'malformed': b'%PDF-1.7\nbroken'}[kind]
        upload['digest'] = hashlib.sha256(upload['raw']).hexdigest()
    elif kind == 'oversized':
        monkeypatch.setattr(extraction, 'MAX_PDF_BYTES', 10)
    elif kind == 'page_limit':
        monkeypatch.setattr(extraction, 'MAX_DOCUMENT_PAGES', 0)
    else:
        import fitz
        with fitz.open() as doc:
            doc.new_page()
            upload['raw'] = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='reader')
        upload['digest'] = hashlib.sha256(upload['raw']).hexdigest()
    with pytest.raises(ValueError):
        attach(upload)
    assert records(upload) == []
    assert not destination(upload).exists()
    assert not list(destination(upload).parent.glob('*.tmp'))


@pytest.mark.parametrize('kind', ['conflict', 'hardlink', 'reparse'])
def test_existing_unsafe_destination_cannot_be_replaced(upload, monkeypatch, kind):
    path = destination(upload)
    path.parent.mkdir(parents=True)
    original = b'owner bytes' if kind == 'conflict' else upload['raw']
    path.write_bytes(original)
    if kind == 'hardlink':
        path.with_suffix('.other').hardlink_to(path)
    elif kind == 'reparse':
        real = service.is_reparse_point
        monkeypatch.setattr(service, 'is_reparse_point', lambda p: p == path.parent or real(p))
    with pytest.raises(ValueError):
        attach(upload)
    assert path.read_bytes() == original and records(upload) == []


def test_attachment_history_remains_readable_but_changed_or_missing_pdf_is_not_extractable(upload):
    from app.services import review_pdf_extraction
    first = attach(upload)['attachment']
    destination(upload).write_bytes(b'changed')
    listing = review_pdf_extraction.inventory(upload['topic'], upload['version'], upload['actor'])
    assert not any(p.get('attachment_id') == first['attachment_id'] for p in listing['pdfs'])
    assert any(d['diagnostic_id'] == first['attachment_id'] for d in listing['unavailable'])
    destination(upload).unlink()
    assert service.listing(upload['topic'], upload['version'], upload['actor'])['attachments'] == [first]
    with pytest.raises(KeyError):
        review_pdf_extraction.extract(upload['topic'], upload['version'], extraction.Extract(
            run_id=upload['run'], paper_id=upload['paper'], expected_pdf_sha256=upload['digest'], attachment_id=first['attachment_id']), upload['actor'])


def test_actual_api_authorization_precedes_body_and_hidden_target_is_denied(upload, monkeypatch):
    from app.api import paper_bank as api
    from app.services import review_input_history, ai_provider
    monkeypatch.setattr(ai_provider, 'run_ai_review_provider', lambda *a, **k: pytest.fail('no model calls'))
    url = root(upload) + f'/pdf-attachments/{upload["target"]}/{upload["digest"]}'
    with client() as http:
        assert http.put(url.replace(upload['target'], 'pdft_' + '0' * 64), content=upload['raw'], headers={'Content-Type': 'application/pdf'}).status_code == 404
        assert http.put(url.replace(upload['topic'], upload['other_topic']), content=upload['raw'], headers={'Content-Type': 'application/pdf'}).status_code == 404
        profiles = review_input_history.topic_profiles
        monkeypatch.setattr(profiles, 'SYSTEM_TEMPLATE_PROFILE_IDS', profiles.SYSTEM_TEMPLATE_PROFILE_IDS | {upload['topic']})
        monkeypatch.setattr(api, 'resource_actor_for_user', lambda _: ResourceActor.oidc_user('public-reader'))
        response = http.put(url, content=b'oversized unread', headers={'Content-Type': 'application/pdf', 'Content-Length': '25000001'})
        assert response.status_code == 404
    assert records(upload) == [] and not destination(upload).exists()


def test_api_stream_limit_bad_content_type_and_hash_mismatch_do_not_store(upload, monkeypatch):
    url = root(upload) + f'/pdf-attachments/{upload["target"]}/{upload["digest"]}'
    with client() as http:
        assert http.put(url, content=upload['raw'], headers={'Content-Type': 'application/json'}).status_code == 422
        assert http.put(url + '?path=C:/private.pdf', content=upload['raw'], headers={'Content-Type': 'application/pdf'}).status_code == 422
        assert http.put(url, content=b'%PDF-1.7\ndifferent', headers={'Content-Type': 'application/pdf'}).status_code == 409
        monkeypatch.setattr(extraction, 'MAX_PDF_BYTES', 10)
        assert http.put(url, content=iter([b'%PDF-', b'x' * 6]), headers={'Content-Type': 'application/pdf'}).status_code == 413
    assert records(upload) == [] and not destination(upload).exists()


def test_v1_identity_and_v2_binding_cannot_be_confused(upload):
    body = extraction.Extract(run_id='run', paper_id='paper', expected_pdf_sha256='a' * 64)
    original = 'pdfx_' + extraction.sha(extraction.encoded(['PAPER_PDF_EXTRACTION_V1', 'LOCAL_PDF_TEXT_V1', 'topic', 'version', {
        'run_id': 'run', 'paper_id': 'paper', 'expected_pdf_sha256': 'a' * 64}]))
    assert extraction.identity('topic', 'version', body) == original
    first = attach(upload)['attachment']
    from app.services import review_pdf_extraction
    request = extraction.Extract(run_id=upload['run'], paper_id='other-paper', expected_pdf_sha256=upload['digest'], attachment_id=first['attachment_id'])
    with pytest.raises(KeyError):
        review_pdf_extraction.extract(upload['topic'], upload['version'], request, upload['actor'])


def test_read_only_inventory_does_not_initialize_tables_or_change_database(upload):
    before = bootstrap.metadata_index_path().read_bytes()
    assert service.listing(upload['topic'], upload['version'], upload['actor'])['attachments'] == []
    assert bootstrap.metadata_index_path().read_bytes() == before


def test_new_targets_use_canonical_occurrences_without_private_aliases(upload, monkeypatch):
    from app.services import review_input_history
    original = review_input_history.visible_paper
    private = 'C:/private/owner/paper.pdf'
    def aliases(*args):
        profile, item = original(*args)
        return profile, {**item, '_reviewOccurrenceKeys': [*item['_reviewOccurrenceKeys'], (upload['run'], private)]}
    monkeypatch.setattr(review_input_history, 'visible_paper', aliases)
    listing = service.listing(upload['topic'], upload['version'], upload['actor'])
    assert len([t for t in listing['targets'] if t['run_id'] == upload['run']]) == 1
    assert private not in str(listing)
    with pytest.raises(KeyError):
        service.authorize(upload['topic'], upload['version'], domain.target_id(upload['topic'], upload['version'], upload['run'], private), upload['actor'])


@pytest.mark.parametrize('existing', [False, True])
def test_revoked_target_during_preflight_does_not_publish(upload, monkeypatch, existing):
    from app.services import review_pdf_extraction
    if existing:
        destination(upload).parent.mkdir(parents=True)
        destination(upload).write_bytes(upload['raw'])
    original = review_pdf_extraction._preflight_pdf
    def revoke(path):
        original(path)
        monkeypatch.setattr(service, '_targets', lambda *args: [])
    monkeypatch.setattr(review_pdf_extraction, '_preflight_pdf', revoke)
    with pytest.raises(KeyError):
        attach(upload)
    assert records(upload) == []
    if existing:
        assert destination(upload).read_bytes() == upload['raw']
    else:
        assert not destination(upload).exists()


def test_interrupted_request_stream_never_creates_an_attachment(upload):
    from fastapi import Request
    from app.api.paper_bank import attach_local_pdf
    from starlette.requests import ClientDisconnect
    import asyncio
    calls = 0
    async def receive():
        nonlocal calls
        calls += 1
        return {'type': 'http.request', 'body': b'%PDF-', 'more_body': True} if calls == 1 else {'type': 'http.disconnect'}
    request = Request({'type': 'http', 'headers': [(b'content-type', b'application/pdf')], 'query_string': b''}, receive)
    with pytest.raises(ClientDisconnect):
        asyncio.run(attach_local_pdf(upload['topic'], upload['version'], upload['target'], upload['digest'], request, None))
    assert records(upload) == [] and not destination(upload).exists()


def test_excerpt_only_app_can_attach_then_explicitly_extract(linked):
    fitz = pytest.importorskip("fitz", reason="Locked optional fulltext dependency not installed")
    from app.db import bootstrap
    from app.repositories import paper_reviews

    with fitz.open() as document:
        document.new_page().insert_textbox((60, 60, 550, 700), "Local PDF body, not a model review. " * 30)
        raw = document.tobytes()
    digest = hashlib.sha256(raw).hexdigest()
    old = paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True)
    excerpt = linked["path"].read_bytes()
    with client() as http:
        response = http.get(root(linked) + '/pdf-attachments')
        assert response.status_code == 200, response.text
        target = next(t for t in response.json()['targets'] if t['run_id'] == linked['run'])
        url = root(linked) + f'/pdf-attachments/{target["target_id"]}/{digest}'
        response = http.put(url, content=raw, headers={'Content-Type': 'application/pdf'})
        assert response.status_code == 200, response.text
        saved = response.json()['attachment']
        assert response.json()['created'] is True
        assert (bootstrap.STORAGE_ROOT / saved['run_id'] / saved['artifact_ref'][4:]).read_bytes() == raw
        assert http.put(url, content=raw, headers={'Content-Type': 'application/pdf'}).json() == {'attachment': saved, 'created': False}
        inventory = http.get(root(linked) + '/pdf-extractions').json()
        assert inventory['extractions'] == []
        pdf = next(p for p in inventory['pdfs'] if p.get('attachment_id') == saved['attachment_id'])
        extracted = http.post(root(linked) + '/pdf-extractions', json=dict(run_id=pdf['run_id'], paper_id=pdf['paper_id'], expected_pdf_sha256=digest, attachment_id=saved['attachment_id']))
        assert extracted.status_code == 200, extracted.text
        assert extracted.json()['extraction']['schema_version'] == 'PAPER_PDF_EXTRACTION_V2'
        assert extracted.json()['extraction']['pdf']['attachment_id'] == saved['attachment_id']
    assert paper_reviews.get_review(linked["topic"], linked["run"], linked["paper"], read_only=True) == old
    assert linked["path"].read_bytes() == excerpt
