import unittest
from unittest.mock import patch
from tests.test_analysis import AnalysisTests
from analysis.worker import process
from analysis.store import BudgetExceeded,packed
from analysis.narration import NARRATION_VERSION


class BusinessIntegration(unittest.TestCase):
    setUp=AnalysisTests.setUp
    tearDown=AnalysisTests.tearDown
    login=AnalysisTests.login

    def test_automatic_api_is_authenticated_and_readonly_until_tick(self):
        self.assertEqual(self.client.get('/api/feed').status_code,401)
        h=self.login()
        self.assertEqual(self.client.get('/api/automation').status_code,200)
        self.assertIsNone(self.client.get('/api/automation').json()['activated_at'])
        self.assertEqual(self.store.jobs(),[])
        response=self.client.post('/api/automation',json={'enabled':False},headers=h)
        self.assertFalse(response.json()['enabled'])
        self.assertEqual(self.store.jobs(),[])

    def test_business_manual_input_and_steps_preserve_partial_output(self):
        h=self.login();recording=self.client.get('/api/recordings').json()[0]
        j=self.client.post('/api/jobs',json={'mode':'both','recording_ids':[recording['id']]},headers=h).json()
        self.assertEqual(j['mode'],'business')
        body={'id':'t1','recording':recording,'status':'ready','segments':[{'text':'客户需要处理业务咨询'}]}
        self.store.save_transcript(body)
        with patch('analysis.worker.transcribe',return_value=body):
            process('transcribing',self.settings,self.store,self.store.claim('transcribing'),lambda:None)
        def fail_after_summary(*args,**kwargs):
            kwargs['on_result']({'summary':{'items':[{'text':'客户需求'}]},'dossier':{'version':'business-cases-v1'}})
            raise RuntimeError('口播暂时失败')
        with patch('analysis.business.run',side_effect=fail_after_summary):
            process('summarizing',self.settings,self.store,self.store.claim('summarizing'),lambda:None)
        self.assertEqual(self.store.job(j['id'])['state'],'failed')
        self.assertIn('summary',self.store.job(j['id'])['result'])
        feed=self.client.get('/api/feed').json()['entries']
        self.assertEqual(feed[0]['job_id'],j['id']);self.assertTrue(feed[0]['partial'])
        self.store.retry(j['id'])
        with patch('analysis.business.run',return_value={'script':{'cases':[]}}):
            process('summarizing',self.settings,self.store,self.store.claim('summarizing'),lambda:None)
        result=self.store.job(j['id'])['result']
        self.assertIn('summary',result);self.assertIn('script',result)
        self.assertEqual(result['transcript_ids'],['t1'])

    def test_edit_jobs_share_original_budget(self):
        self.settings.task_budget=100
        j=self.store.create_job('business',{'transcript_ids':['t1']})
        usage=self.store.reserve(j['id'],60,self.settings);self.store.settle(usage,60)
        edit=self.store.create_job('business',{'transcript_ids':['t2'],'billing_group_id':j['id']})
        with self.assertRaisesRegex(BudgetExceeded,'本批'):
            self.store.reserve(edit['id'],60,self.settings)

    def test_manual_style_upgrade_creates_new_job_but_repeat_is_idempotent(self):
        headers=self.login()
        recording=self.client.get('/api/recordings').json()[0]
        self.store.save_transcript({'id':'style-t1','recording':recording,'revision':1,
                                   'segments':[{'text':'客户希望整理咨询记录，由业务员工确认内容。'}]})
        variants=[
            ('script',{'transcript_ids':['style-t1'],'script_version':'product-story-v6','script_topic':''},
             {'mode':'script','transcript_ids':['style-t1']}),
            ('business',{'transcript_ids':['style-t1'],'business_version':'business-cases-v1','script_topic':''},
             {'mode':'business','transcript_ids':['style-t1']}),
            ('business',{'recordings':[recording],'business_version':'business-cases-v1','script_topic':''},
             {'mode':'both','recording_ids':[recording['id']]}),
        ]
        for mode,legacy_payload,request in variants:
            with self.subTest(request=request):
                old=self.store.create_job(mode,legacy_payload)
                with self.store.db(True) as db:
                    db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?",
                               (packed({'script':{'cases':[{'title':'旧口播稿','text':'旧稿正文'}]}}),old['id']))
                response=self.client.post('/api/jobs',json=request,headers=headers)
                self.assertEqual(response.status_code,200,response.text)
                new=response.json()
                self.assertNotEqual(new['id'],old['id'])
                self.assertEqual(new['payload']['narration_version'],NARRATION_VERSION)
                self.assertEqual(self.client.post('/api/jobs',json=request,headers=headers).json()['id'],new['id'])
                self.assertEqual(self.store.job(old['id'])['result']['script']['cases'][0]['text'],'旧稿正文')

    def test_lock_retains_original_collection_without_losing_versions(self):
        h=self.login()
        old=self.store.create_job('business',{'transcript_ids':['t1'],'collection_id':'c1'})
        newer=self.store.create_job('business',{'transcript_ids':['t1','t2'],'collection_id':'c1'})
        for i,j in enumerate([old,newer]):
            with self.store.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',created=?,result=? WHERE id=?",(10+i,packed({'script':{'cases':[{'title':'客户咨询项目'}]},'transcript_ids':['t1']}),j['id']))
        self.assertEqual(self.client.get('/api/feed').json()['entries'][0]['job_id'],newer['id'])
        r=self.client.post('/api/jobs/'+old['id']+'/lock',json={'locked':True},headers=h)
        self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['locked'])
        self.assertEqual(self.client.get('/api/feed').json()['entries'][0]['job_id'],old['id'])
        self.assertEqual(len(self.store.jobs()),2)

    def test_unknown_paid_request_is_not_automatically_repeated_on_recovery(self):
        j=self.store.create_job('business',{'transcript_ids':['t1'],'automatic':True})
        self.store.claim('summarizing');self.store.reserve(j['id'],10,self.settings)
        with self.store.db(True) as db:db.execute('UPDATE jobs SET lease=0 WHERE id=?',(j['id'],))
        self.assertIsNone(self.store.claim('summarizing'))
        self.assertEqual(self.store.job(j['id'])['state'],'failed')

    def test_manually_generated_version_can_also_be_retained(self):
        jobs=[self.store.create_job('business',{'transcript_ids':['t1'],'script_topic':topic}) for topic in ['原稿','新版']]
        for i,j in enumerate(jobs):
            with self.store.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',created=?,result=? WHERE id=?",(20+i,packed({'transcript_ids':['t1'],'script':{'cases':[{'title':'同一素材'}]}}),j['id']))
        self.store.lock_output(jobs[0]['id'],True)
        self.assertEqual(self.store.feed()[0]['job_id'],jobs[0]['id'])
        self.store.lock_output(jobs[0]['id'],False)
        self.assertEqual(self.store.feed()[0]['job_id'],jobs[1]['id'])


if __name__=='__main__':unittest.main()
