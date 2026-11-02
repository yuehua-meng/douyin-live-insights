import copy
import tempfile
from pathlib import Path
import unittest

from analysis.case_library import get_case, list_cases
from analysis.store import Store, packed


class CaseLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-case-library-', dir=Path(__file__).resolve().parents[1])
        self.store = Store(Path(self.temp.name))
        self.clock = 100

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def case(project='p1', text='我们讨论先整理客户咨询，再交给业务人员确认。', title='客户咨询整理'):
        return dict(case_id=project, title=title, stage='方案讨论', text=text, refs=['s1'], missing_details=[])

    def job(self, cases=None, collection='c1', tids=None, summary=None, state='completed', mode='business', **result_fields):
        self.clock += 1
        tids = tids if tids is not None else ['t1']
        payload = dict(transcript_ids=tids, nonce=self.clock)
        if collection: payload['collection_id'] = collection
        job = self.store.create_job(mode, payload)
        result = dict(transcript_ids=tids, **result_fields)
        if cases is not None:
            result['script'] = dict(cases=copy.deepcopy(cases), transcript_ids=tids,
                evidence=[dict(id='s1', recording_id='r1', start_ms=0, end_ms=1000, text='客户咨询讨论'),
                          dict(id='s2', recording_id='r2', start_ms=0, end_ms=1000, text='另一个项目')])
        if summary is not None: result['summary'] = summary
        with self.store.db(True) as db:
            db.execute('UPDATE jobs SET state=?,result=?,created=?,updated=? WHERE id=?',
                       (state, packed(result), self.clock, self.clock, job['id']))
        return job

    @staticmethod
    def summary():
        return dict(items=[dict(heading='咨询流程', text='先整理咨询再人工确认。', refs=['s1']),
                           dict(heading='另一业务', text='另一项目的需求。', refs=['s2'])],
                    evidence=[dict(id='s1', recording_id='r1', start_ms=0, end_ms=1000, text='客户咨询讨论'),
                              dict(id='s2', recording_id='r2', start_ms=0, end_ms=1000, text='另一个项目')])

    def test_lists_individual_cases_and_selects_matching_summary(self):
        first = self.case()
        second = self.case('p2', '另一项目有自己的业务链。', '另一项目')
        second['refs'] = ['s2']
        job = self.job([first, second], summary=self.summary())
        rows = list_cases(self.store)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['job_id'] == job['id'] for r in rows))
        self.assertTrue(all('text' not in r and 'case' not in r and 'evidence' not in r for r in rows))
        row = next(r for r in rows if r['case_index'] == 0)
        detail = get_case(self.store, row['id'])
        self.assertEqual(detail['case']['text'], first['text'])
        self.assertEqual(len(detail['script']['cases']), 1)
        self.assertEqual(detail['summary_scope'], 'case')
        self.assertEqual([i['heading'] for i in detail['summary']['items']], ['咨询流程'])
        self.assertEqual([e['id'] for e in detail['summary']['evidence']], ['s1'])
        self.assertEqual(len(self.store.job(job['id'])['result']['summary']['items']), 2)

    def test_latest_case_keeps_stable_id_and_older_non_replaced_case(self):
        old = self.job([self.case(), self.case('p2', '较早但仍有价值的另一案例。')])
        before = {r['case_index']: r for r in list_cases(self.store)}
        newest = self.job([self.case(text='新版保留业务链，增加当前交付进展。')], state='running', tids=['t1', 't2'])
        rows = list_cases(self.store)
        self.assertEqual(len(rows), 2)
        updated = next(r for r in rows if r['job_id'] == newest['id'])
        self.assertEqual(updated['id'], before[0]['id'])
        self.assertNotEqual(updated['version'], before[0]['version'])
        self.assertTrue(updated['partial'])
        self.assertEqual(next(r for r in rows if r['case_index'] == 1)['job_id'], old['id'])
        with self.store.db(True) as db:
            db.execute('UPDATE jobs SET updated=updated+100 WHERE id=?', (newest['id'],))
        self.assertEqual(next(r for r in list_cases(self.store) if r['id'] == updated['id'])['version'], updated['version'])

    def test_locked_or_adopted_case_wins_and_can_be_released(self):
        old = self.job([self.case()])
        newer = self.job([self.case(text='同项目更新版本。')])
        self.store.lock_output(old['id'], True)
        row = list_cases(self.store)[0]
        self.assertEqual(row['job_id'], old['id'])
        self.assertTrue(row['locked'])
        self.assertTrue(get_case(self.store, row['id'])['locked'])
        self.store.lock_output(old['id'], False)
        self.assertEqual(list_cases(self.store)[0]['job_id'], newer['id'])
        adopted = self.case(text='已经采用的稿件。'); adopted['adopted'] = True
        adopted_job = self.job([adopted])
        self.job([self.case(text='晚生成的另一个版本。')])
        self.assertEqual(list_cases(self.store)[0]['job_id'], adopted_job['id'])

    def test_same_title_different_project_or_collection_never_merges(self):
        self.job([self.case(), self.case('p2')], collection='customer-a')
        self.job([self.case()], collection='customer-b')
        rows = list_cases(self.store)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r['id'] for r in rows}), 3)

    def test_archived_filtered_and_drafts_are_not_published(self):
        archived = self.job([self.case()], collection='old')
        with self.store.db(True) as db:
            db.execute('INSERT INTO job_archives VALUES(?,?,?)', (archived['id'], 'low quality', 100))
        self.job([self.case()], collection='bad', content_filtered=True)
        self.job([dict(self.case(), draft=True), dict(self.case('p2'), state='reviewing'), dict(self.case('p3'), text='   ')], collection='drafts')
        partial = self.job([self.case()], collection='usable', state='failed')
        rows = list_cases(self.store)
        self.assertEqual([r['job_id'] for r in rows], [partial['id']])
        self.assertIsNone(get_case(self.store, 'case_not_found'))

    def test_unmatched_summary_is_explicitly_batch_and_missing_is_unavailable(self):
        summary = self.summary()
        summary['items'] = summary['items'][1:]
        first = self.job([self.case()], collection='c1', summary=summary)
        self.job([self.case()], collection='c2', tids=['other'])
        rows = list_cases(self.store)
        detail = get_case(self.store, next(r['id'] for r in rows if r['job_id'] == first['id']))
        self.assertEqual(detail['summary_scope'], 'batch')
        self.assertEqual(len(detail['summary']['items']), 1)
        missing = get_case(self.store, next(r['id'] for r in rows if r['job_id'] != first['id']))
        self.assertIsNone(missing['summary'])
        self.assertFalse(missing['summary_available'])
        self.assertEqual(missing['summary_scope'], 'unavailable')

    def test_legacy_exact_copies_dedupe_but_different_sources_survive(self):
        case = self.case(); case.pop('case_id')
        self.job([case], collection=None, mode='script')
        latest = self.job([case], collection=None, mode='script')
        self.job([case], collection=None, mode='script', tids=['another-source'])
        self.job([dict(case, text='同名但不同案例的正文。')], collection=None, mode='script')
        rows = list_cases(self.store)
        self.assertEqual(len(rows), 3)
        self.assertIn(latest['id'], {r['job_id'] for r in rows})

    def test_legacy_summary_links_only_identical_sources_and_not_local_ref_names(self):
        case = self.case(); case.pop('case_id')
        self.job([case], collection=None, mode='script')
        summary = self.summary()
        # E/S IDs are local to each old job: match original audio positions.
        summary['items'][0]['refs'] = ['other-reference']
        summary['evidence'][0]['id'] = 'other-reference'
        summary_job = self.job(None, collection=None, mode='summary', summary=summary)
        unrelated = self.summary()
        unrelated['items'][0]['text'] = '不能串到不同的原文版本。'
        self.job(None, collection=None, mode='summary', summary=unrelated, tids=['wrong'])
        rows = list_cases(self.store)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['summary_available'])
        detail = get_case(self.store, rows[0]['id'])
        self.assertEqual(detail['summary_job_id'], summary_job['id'])
        self.assertEqual(detail['summary_scope'], 'case')
        self.assertEqual(detail['summary']['items'][0]['refs'], ['other-reference'])

    def test_library_is_not_truncated_at_legacy_history_limit(self):
        for n in range(205):
            self.job([self.case(project='p'+str(n))])
        rows = list_cases(self.store)
        self.assertEqual(len(rows), 205)
        self.assertIsNotNone(get_case(self.store, rows[-1]['id']))


if __name__ == '__main__':
    unittest.main()
