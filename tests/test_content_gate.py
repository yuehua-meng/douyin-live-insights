import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis.settings import Settings
from analysis.store import Store, packed
from analysis.worker import process


class ContentGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='.test-analysis-',dir=Path(__file__).resolve().parents[1])
        self.settings=Settings(Path(self.temp.name)/'recordings',Path(self.temp.name)/'analysis')
        self.store=Store(self.settings.data)

    def tearDown(self):
        self.temp.cleanup()

    def transcript(self,ident,text):
        body=dict(id=ident,status='ready',duration_ms=3000,revision=1,
            recording=dict(id='r-'+ident,path='owner/day/session/'+ident+'.m4a',size=123,mtime_ns=456),
            segments=[dict(id=ident+':0',start_ms=0,end_ms=3000,text=text)] if text else [])
        self.store.save_transcript(body)
        return body

    def test_all_filler_jobs_finish_without_any_model_or_stale_outputs(self):
        self.transcript('filler','嗯，啊。')
        for mode in ['business','summary','script']:
            job=self.store.create_job(mode,{'transcript_ids':['filler']})
            old={'transcript_ids':['filler'],'summary':{'prompt_version':'business-cases-v1','items':[{'text':'旧空总结'}]},
                 'script':{'cases':[]},'dossier':{'version':'business-cases-v1'}}
            with self.store.db(True) as db:db.execute('UPDATE jobs SET result=? WHERE id=?',(packed(old),job['id']))
            with patch('analysis.business.run') as business,patch('analysis.script.generate') as script,patch('analysis.summarize.Summarizer.run') as summary:
                process('summarizing',self.settings,self.store,self.store.claim('summarizing'),lambda:None)
                business.assert_not_called();script.assert_not_called();summary.assert_not_called()
            done=self.store.job(job['id'])
            self.assertEqual(done['state'],'completed',done['error'])
            self.assertTrue(done['result']['content_filtered'])
            self.assertEqual(done['result']['transcript_ids'],[])
            self.assertEqual(done['result']['excluded_transcripts'][0]['reason'],'filler_only')
            for field in ('summary','script','dossier'):self.assertNotIn(field,done['result'])

    def test_mixed_input_retains_useful_text_and_discards_old_derived_sources(self):
        self.transcript('filler','嗯。');good=self.transcript('useful','客户需要整理订单，我们先收集订单再交给负责人复核。')
        job=self.store.create_job('business',{'transcript_ids':['filler','useful']})
        with self.store.db(True) as db:
            db.execute('UPDATE jobs SET result=? WHERE id=?',(packed({'transcript_ids':['filler','useful'],
                'summary':{'items':[{'text':'旧总结'}],'prompt_version':'business-cases-v1'},'dossier':{'old':True},'script':{'cases':[]}}),job['id']))
        seen=[]
        def run(settings,store,jid,transcripts,*args,**kwargs):
            seen.extend(t['id'] for t in transcripts)
            current=store.job(jid)['result']
            self.assertNotIn('summary',current);self.assertNotIn('dossier',current);self.assertNotIn('script',current)
            return {'summary':{'items':[{'heading':'订单复核','text':good['segments'][0]['text']}],'transcript_ids':['useful']}}
        with patch('analysis.business.run',side_effect=run):
            process('summarizing',self.settings,self.store,self.store.claim('summarizing'),lambda:None)
        result=self.store.job(job['id'])['result']
        self.assertEqual(seen,['useful'])
        self.assertEqual(result['transcript_ids'],['useful'])
        self.assertEqual(result['source_transcript_ids'],['filler','useful'])
        self.assertNotIn('content_filtered',result)

    def test_cached_asr_is_assessed_and_only_asr_job_keeps_audit_id(self):
        body=self.transcript('cached','嗯。')
        job=self.store.create_job('transcribe',{'recordings':[body['recording']]})
        with patch('analysis.worker.transcribe',return_value=copy.deepcopy(body)):
            process('transcribing',self.settings,self.store,self.store.claim('transcribing'),lambda:None)
        result=self.store.job(job['id'])['result']
        self.assertEqual(result['transcript_ids'],['cached'])
        self.assertEqual(result['usable_transcript_ids'],[])
        self.assertTrue(result['content_filtered'])
        self.assertTrue(self.store.content_filtered(body['recording']))


if __name__=='__main__':unittest.main()
