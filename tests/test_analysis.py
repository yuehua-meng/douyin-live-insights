import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from analysis.api import create_app, password_hash
from analysis.settings import Settings
from analysis.store import Store, BudgetExceeded, Cancelled, digest
from analysis.catalog import scan, safe_file
from analysis.summarize import Summarizer, validate_summary
from analysis.worker import process


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-analysis-', dir=Path(__file__).resolve().parents[1] if os.name=='nt' else None)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root/'recordings', self.root/'analysis', origin='https://testserver',
            cookie_path='/', password_hash=password_hash('test-password'))
        self.folder=self.settings.recordings/'owner'/'day'/'session'
        self.folder.mkdir(parents=True)
        (self.folder/'part_0000.m4a').write_bytes(b'audio')
        (self.folder/'session.json').write_text(json.dumps({'files':[{'name':'part_0000.m4a'},{'name':'../../escape.m4a'}]}))
        self.store=Store(self.settings.data)
        self.app=create_app(self.settings)
        self.client=TestClient(self.app,base_url='https://testserver')

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self):
        r=self.client.post('/api/login',json={'password':'test-password'},headers={'Origin':self.settings.origin})
        self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers['set-cookie'])
        self.assertIn('Secure',r.headers['set-cookie'])
        return {'Origin':self.settings.origin,'X-CSRF-Token':r.json()['csrf']}

    def test_auth_csrf_and_selection(self):
        self.assertEqual(self.client.get('/api/recordings').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'password':'test-password'}).status_code,403)
        headers=self.login()
        rows=self.client.get('/api/recordings').json()
        self.assertEqual(len(rows),1)
        self.assertEqual(self.client.post('/api/jobs',json={'recording_ids':[rows[0]['id']]},headers={'Origin':self.settings.origin}).status_code,403)
        self.assertEqual(self.client.post('/api/jobs',json={'recording_ids':['../../etc/passwd']},headers=headers).status_code,400)
        result=self.client.post('/api/jobs',json={'recording_ids':[rows[0]['id']]},headers=headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['stage'],'transcribing')
        self.assertEqual(self.client.post('/api/jobs',content='x'*2_000_001,headers=headers).status_code,413)
        self.assertNotIn('password',self.client.get('/api/status').text)
        media=self.client.get('/api/media/'+rows[0]['id'],headers={'Range':'bytes=0-1'})
        self.assertEqual(media.status_code,206)
        self.assertEqual(media.content,b'au')
        download=self.client.get('/api/media/'+rows[0]['id']+'?download=true',headers={'Range':'bytes=0-1'})
        self.assertEqual(download.status_code,206)
        self.assertEqual(download.content,b'au')
        self.assertIn('attachment;',download.headers['content-disposition'])
        self.assertIn('part_0000.m4a',download.headers['content-disposition'])
        self.client.post('/api/logout',json={},headers=headers)
        self.assertEqual(self.client.get('/api/jobs').status_code,401)

    def test_catalog_changed_and_escape(self):
        recording=scan(self.settings.recordings)[0]
        (self.folder/'part_0000.m4a').write_bytes(b'changed')
        with self.assertRaises(ValueError): safe_file(self.settings.recordings,recording)
        with self.assertRaises(ValueError): safe_file(self.settings.recordings,dict(recording,path='../escape.m4a'))

    def test_archive_preserves_result_and_hides_duplicate_drafts(self):
        jobs=[]
        for version, topic in [('v1','项目A'), ('v2','项目A'), ('v2','项目B')]:
            job=self.store.create_job('script', {'transcript_ids':['t1'], 'script_version':version, 'script_topic':topic})
            result={'transcript_ids':['t1'],'script':{'cases':[{'title':topic,'text':'原稿'}],'transcript_ids':['t1']}}
            with self.store.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',created=?,result=? WHERE id=?", (len(jobs)+1,json.dumps(result),job['id']))
            jobs.append(job)
        self.assertEqual({r['job_id'] for r in self.store.result_catalog()}, {jobs[1]['id'],jobs[2]['id']})
        before=self.store.job(jobs[0]['id'])
        self.store.archive_script_jobs([jobs[0]['id']], '重复调试版本')
        self.assertEqual(self.store.job(jobs[0]['id']),before)
        self.assertNotIn(jobs[0]['id'], {j['id'] for j in self.store.jobs()})
        pending=self.store.create_job('script',{'transcript_ids':['other']})
        with self.assertRaises(ValueError):
            self.store.archive_script_jobs([pending['id']], '不允许归档处理中任务')

    def test_generated_catalog_is_readonly_and_keeps_versions_separate(self):
        self.assertEqual(self.client.get('/api/results').status_code, 401)
        self.login()
        jobs = []
        for mode, ids in [('summary', ['t1']), ('script', ['t1']), ('script', ['t2'])]:
            job = self.store.create_job(mode, {'transcript_ids': ids})
            payload = {'transcript_ids': ids, mode: {'transcript_ids': ids}}
            if mode == 'script':
                payload[mode]['cases'] = [{'title': '客户咨询整理项目', 'text': '正文不应出现在目录'}]
            with self.store.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?", (json.dumps(payload), job['id']))
            jobs.append(job)
        before = self.store.spending()
        rows = self.client.get('/api/results').json()
        self.assertEqual(len(rows), 3)
        self.assertEqual({tuple(r['transcript_ids']) for r in rows}, {('t1',), ('t2',)})
        self.assertEqual([r for r in rows if r['kind']=='script'][0]['title'], '客户咨询整理项目')
        self.assertNotIn('正文不应出现在目录', json.dumps(rows, ensure_ascii=False))
        self.assertEqual(self.store.spending(), before)
        self.assertEqual(len(self.store.jobs()), 3)

    def test_transcription_badge_tracks_saved_audio_version(self):
        self.assertEqual(self.client.get('/api/recordings/transcribed').status_code, 401)
        self.login()
        row = self.client.get('/api/recordings').json()[0]
        self.assertFalse(row['transcribed'])
        # A saved result is valid even without a completed whole-batch job.
        recording = scan(self.settings.recordings)[0]
        body = dict(id='complete-audio', recording=recording, status='ready', segments=[{'text': '已识别的文字'}])
        self.store.save_transcript(body)
        self.assertTrue(self.client.get('/api/recordings').json()[0]['transcribed'])
        records = self.client.get('/api/recordings/transcribed').json()
        self.assertEqual(records[0]['mtime_ns'], str(recording['mtime_ns']))
        (self.folder/'part_0000.m4a').write_bytes(b'replaced audio')
        self.assertFalse(self.client.get('/api/recordings').json()[0]['transcribed'])

    def test_dedup_claim_cancel_recovery(self):
        with ThreadPoolExecutor(4) as pool:
            jobs=list(pool.map(lambda _:self.store.create_job('both',{'recordings':[]}),range(8)))
        self.assertEqual(len({j['id'] for j in jobs}),1)
        claim=self.store.claim('transcribing')
        self.assertIsNone(self.store.claim('transcribing'))
        with self.store.db(True) as db: db.execute('UPDATE jobs SET lease=0')
        newer=self.store.claim('transcribing')
        self.assertNotEqual(claim[1],newer[1])
        with self.assertRaises(Cancelled): self.store.update(*claim,state='completed')
        self.store.cancel(newer[0])
        with self.assertRaises(Cancelled): self.store.heartbeat(*newer)

    def test_budget_atomic_uncertain_retained(self):
        self.settings.task_budget=100
        def reserve(_):
            try: return self.store.reserve('job',60,self.settings)
            except BudgetExceeded: return None
        with ThreadPoolExecutor(4) as pool: ids=list(pool.map(reserve,range(4)))
        self.assertEqual(sum(x is not None for x in ids),1)
        self.assertEqual(self.store.spending()['committed'],60)
        self.store.settle(next(x for x in ids if x),10)
        self.assertEqual(self.store.spending()['committed'],10)

    def test_summary_refs_cache_and_billing(self):
        calls=[]
        def mock(request):
            calls.append(request)
            return httpx.Response(200,json={'usage':{'prompt_tokens':100,'completion_tokens':50},'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'items':[{'text':'概要','refs':['s1']}]})}}]})
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-only'}):
            runner=Summarizer(self.settings,self.store,'job',lambda:None,httpx.MockTransport(mock))
            a=runner.call({'text':'测试'},{'s1'})
            self.assertEqual(a,runner.call({'text':'测试'},{'s1'}))
        self.assertEqual(len(calls),1)
        self.assertEqual(self.store.spending()['committed'],90)
        with self.assertRaises(ValueError): validate_summary({'items':[{'text':'幻觉','refs':['fake']}]},{'s1'})

    def test_revision_preserves_original(self):
        h=self.login()
        body={'id':'t1','recording':scan(self.settings.recordings)[0],'revision':1,'segments':[{'id':'s1','start_ms':0,'end_ms':1000,'text':'原文'}]}
        self.store.save_transcript(body,'k')
        r=self.client.post('/api/transcripts/t1/revision',json={'texts':['修改']},headers=h)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.store.transcript('t1')['segments'][0]['text'],'原文')
        self.assertEqual(len(self.client.get('/api/transcripts/t1/versions').json()),2)

    def test_truncated_summary_retry_is_budgeted(self):
        calls=[]
        def mock(request):
            calls.append(json.loads(request.content)['max_tokens'])
            return httpx.Response(200,json={'usage':{'prompt_tokens':10,'completion_tokens':10},
                'choices':[{'finish_reason':'length' if len(calls)==1 else 'stop',
                            'message':{'content':json.dumps({'items':[{'text':'完整结果','refs':['E1']}]})}}]})
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'test-only'}):
            runner=Summarizer(self.settings,self.store,'job',lambda:None,httpx.MockTransport(mock))
            result=runner.call({'text':'素材'},{'E1'})
            # A later pipeline stage can fail; re-entering this step must use
            # the successful expanded response instead of billing the short
            # request again.
            self.assertEqual(runner.call({'text':'素材'},{'E1'}),result)
        self.assertEqual(calls,[3000,6000])
        self.assertEqual(self.store.spending()['committed'],30)
        self.assertEqual(result['items'][0]['text'],'完整结果')

    def test_preexisting_expanded_summary_cache_is_revalidated_and_promoted(self):
        payload={'text':'历史素材'}
        from analysis.summarize import PROMPT_VERSION
        original_key=digest([PROMPT_VERSION,self.settings.deepseek_model,payload,3000,'thinking-high'])
        expanded_key=digest([PROMPT_VERSION,self.settings.deepseek_model,payload,6000,'thinking-high'])
        cached={'items':[{'text':'完整历史结果','refs':['E1']}]}
        self.store.cache('summaries',expanded_key,cached)
        runner=Summarizer(self.settings,self.store,'job',lambda:None,
                          httpx.MockTransport(lambda _:self.fail('must not resend paid request')))
        with self.assertRaises(ValueError):runner.call(payload,{'different'},thinking=True)
        self.assertIsNone(self.store.cached('summaries',original_key))
        clean=runner.call(payload,{'E1'},thinking=True)
        self.assertEqual(clean,validate_summary(cached,{'E1'}))
        self.assertEqual(self.store.cached('summaries',original_key),clean)
        self.assertEqual(self.store.spending()['committed'],0)

    def test_summary_failure_retains_transcription(self):
        self.store.save_transcript({'id':'t1','segments':[]})
        job=self.store.create_job('summary',{'transcript_ids':['t1']})
        process('summarizing',self.settings,self.store,self.store.claim('summarizing'),lambda:None)
        result=self.store.job(job['id'])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['result']['transcript_ids'],['t1'])

if __name__=='__main__': unittest.main()
