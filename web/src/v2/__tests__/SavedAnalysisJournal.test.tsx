import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import * as api from '../../lib/api';
import { SavedAnalysisJournal } from '../components/SavedAnalysisJournal';
import type { SavedAnalysis } from '../savedAnalyses';

vi.mock('../../lib/api', async original => ({ ...await original<typeof api>(), fetchJson: vi.fn() }));
vi.mock('../views/JournalReviewPanel', () => ({ JournalReviewPanel: ({ root, enabled }: { root: string; enabled: boolean }) => <div data-testid="journal-review">{root} {enabled ? 'enabled' : 'disabled'}</div> }));
const record = { analysis_id: 'a', latest_decision: { decision_id: 'd1', state: 'reviewed' } } as SavedAnalysis;
const candidate = { candidate_id: 'c1', created_at: '2026-10-11T00:00:00Z', source: { decision: { decision_id: 'd1' } }, proposal: { title: 'Saved analysis', sections: [{ kind: 'overview', entries: [{ kind: 'interpretation', text: 'Original e\u0301 interpretation', citation_ids: ['ref'] }] }] }, citations: [{ citation_id: 'ref', title: 'Original source', text: 'Original evidence', locator: { page: 2 } }], selected_component_ids: ['/contribution_summary'] };
const root = '/api/topics/topic/saved-analyses/a/journals';
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.fetchJson).mockImplementation(async url => {
    if (url.endsWith('/components')) return { items: [{ component_id: '/contribution_summary', label: '기여 요약', text: 'Original e\u0301 interpretation', eligible: true }] } as never;
    if (url.endsWith('/c1')) return candidate as never;
    return { items: [], total: 0, page: 1, limit: 10 } as never;
  });
});
async function open() { fireEvent.click(screen.getByText('분석으로 Journal 후보 만들기')); await screen.findByLabelText('기여 요약 선택'); }

it('requires explicit component selection and a saved human source review', async () => {
  const view = render(<SavedAnalysisJournal topicId="topic" record={{ ...record, latest_decision: null }}/>);
  await open();
  expect(screen.getByRole('button', { name: '선택 분석으로 후보 저장' })).toHaveProperty('disabled', true);
  view.rerender(<SavedAnalysisJournal topicId="topic" record={record}/>);
  expect(screen.getByRole('button', { name: '선택 분석으로 후보 저장' })).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByLabelText('기여 요약 선택'));
  expect(screen.getByRole('button', { name: '선택 분석으로 후보 저장' })).toHaveProperty('disabled', false);
  expect(screen.getByText(/새 모델 호출/)).toBeTruthy();
});

it('retries the identical pinned selection after lost response and opens the shared review panel', async () => {
  const initial = vi.mocked(api.fetchJson).getMockImplementation()!;
  let posts = 0;
  const commands: unknown[] = [];
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === 'POST') { commands.push(JSON.parse(options.body as string)); if (++posts === 1) throw new Error('lost response'); return candidate as never; }
    return initial(url, options);
  });
  render(<SavedAnalysisJournal topicId="topic" record={record}/>); await open();
  fireEvent.click(screen.getByLabelText('기여 요약 선택'));
  fireEvent.click(screen.getByRole('button', { name: '선택 분석으로 후보 저장' }));
  await screen.findByRole('alert'); expect(posts).toBe(1);
  expect(screen.getByLabelText('기여 요약 선택')).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByRole('button', { name: '같은 후보 저장 재시도' }));
  await waitFor(() => expect(screen.getByTestId('journal-review').textContent).toBe(root + '/c1/reviews enabled'));
  expect(commands[0]).toEqual(commands[1]);
  expect(commands[0]).toEqual({ expected_decision_id: 'd1', component_ids: ['/contribution_summary'] });
  expect(screen.getByText('Original evidence')).toBeTruthy();
});

it('keeps historical candidate readable but disables review when the analysis decision changes', async () => {
  vi.mocked(api.fetchJson).mockImplementation(async url => url.endsWith('/components') ? { items: [] } as never : url.endsWith('/c1') ? candidate as never : { items: [{ candidate_id: 'c1', created_at: candidate.created_at, title: 'Saved analysis' }], total: 1, page: 1, limit: 10 } as never);
  const view = render(<SavedAnalysisJournal topicId="topic" record={record}/>);
  fireEvent.click(screen.getByText('분석으로 Journal 후보 만들기'));
  fireEvent.click(await screen.findByRole('button', { name: /후보 열기/ }));
  await screen.findByTestId('journal-review');
  view.rerender(<SavedAnalysisJournal topicId="topic" record={{ ...record, latest_decision: { ...record.latest_decision!, decision_id: 'd2', state: 'rejected' } }}/>);
  expect(screen.getByTestId('journal-review').textContent).toContain('disabled');
  expect(screen.getByText(/분석의 검토 기준이 바뀌어/)).toBeTruthy();
  expect(screen.getByText('Original evidence')).toBeTruthy();
});

it('locks candidate switching during a save and can cancel an uncertain retry without deleting stored candidates', async () => {
  let reject!: (reason: Error) => void;
  const pending = new Promise((_, fail) => { reject = fail; });
  const initial = vi.mocked(api.fetchJson).getMockImplementation()!;
  vi.mocked(api.fetchJson).mockImplementation(async (url, options) => {
    if (options?.method === 'POST') return await pending as never;
    if (url.includes('?page=')) return { items: [{ candidate_id: 'c1', created_at: candidate.created_at, title: 'Saved analysis' }], total: 1, page: 1, limit: 10 } as never;
    return initial(url, options);
  });
  render(<SavedAnalysisJournal topicId="topic" record={record}/>); await open();
  fireEvent.click(screen.getByLabelText('기여 요약 선택'));
  fireEvent.click(screen.getByRole('button', { name: '선택 분석으로 후보 저장' }));
  expect(screen.getByRole('button', { name: /후보 열기/ })).toHaveProperty('disabled', true);
  reject(new Error('lost')); await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: '후보 저장 재시도 취소' }));
  await waitFor(() => expect(screen.getByLabelText('기여 요약 선택')).toHaveProperty('disabled', false));
  expect(screen.getByLabelText('기여 요약 선택')).toHaveProperty('checked', true);
  expect(vi.mocked(api.fetchJson).mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  expect(screen.getByText(/서버에 저장된 후보는 삭제되지 않습니다/)).toBeTruthy();
});
