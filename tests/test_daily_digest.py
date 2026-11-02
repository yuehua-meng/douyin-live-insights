import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import httpx

from analysis.daily_digest import DailyDigest, build_messages, valid_webhook
from analysis.automation import Automation
from analysis.catalog import scan
from analysis.settings import Settings
from analysis.store import Store, packed
from tests import test_automation as fixtures


def clock(day=1, hour=8, minute=0):
    return datetime(2026,11,day,hour,minute,tzinfo=timezone(timedelta(hours=8))).timestamp()


def case(index=1, version='v1'):
    return dict(id=f'case{index}',version=version,title='业务场景与产品价值'+str(index),stage='方案讨论',excerpt='先确定业务需求，再检验方案和交付的价值。')


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='.test-digest-',dir=Path(__file__).resolve().parents[1])
        root=Path(self.temp.name)
        self.settings=Settings(root/'recordings',root/'analysis',daily_batch_mode=True,
                               wecom_digest_enabled=True,wecom_webhook_url='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret-key')
        self.store=Store(self.settings.data)
        self.sent=[]
        def respond(request):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200,json={'errcode':0})
        self.transport=httpx.MockTransport(respond)
        self.daily=DailyDigest(self.settings,self.store,self.transport)

    def tearDown(self):self.temp.cleanup()

    def test_webhook_exact_allowlist_and_secret_not_in_repr(self):
        self.assertTrue(valid_webhook(self.settings.wecom_webhook_url))
        for url in ['http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret-key',
                    'https://qyapi.weixin.qq.com.evil.com/cgi-bin/webhook/send?key=test-secret-key',
                    'https://user@qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret-key',
                    self.settings.wecom_webhook_url+'&key=second-secret-key',self.settings.wecom_webhook_url+'#fragment']:
            self.assertFalse(valid_webhook(url))
        self.assertNotIn('test-secret-key',repr(self.settings))

    def test_first_enable_after_hours_starts_tomorrow_baseline_not_sent(self):
        self.daily.tick(clock(hour=23),[case(1)])
        self.assertEqual(self.sent,[])
        self.assertIsNone(self.daily.snapshot_due(clock(hour=23)))
        self.daily=DailyDigest(self.settings,self.store,self.transport)
        self.daily.tick(clock(day=2,hour=11),[case(1),case(2)])
        self.assertEqual(len(self.sent),1)
        content=self.sent[0]['markdown']['content']
        self.assertIn('#case=case2',content)
        self.assertNotIn('#case=case1',content)
        self.daily.tick(clock(day=2,hour=12),[case(1),case(2),case(3)])
        self.assertEqual(len(self.sent),1,'late arrivals wait for the next digest')
        self.daily.tick(clock(day=3,hour=11),[case(1),case(2),case(3)])
        self.assertIn('#case=case3',self.sent[-1]['markdown']['content'])

    def test_webhook_missing_does_not_consume_new_versions(self):
        self.daily.initialize(clock(),[case(1)])
        self.settings.wecom_webhook_url=''
        self.daily.tick(clock(hour=11),[case(1),case(2)])
        self.assertEqual(self.daily.status()['last_delivery']['state'],'missing_webhook')
        self.assertEqual([c['id'] for c in self.daily.unseen([case(2)])],['case2'])
        self.settings.wecom_webhook_url='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-secret-key'
        self.daily.tick(clock(hour=12),[case(1),case(2)])
        self.assertEqual(len(self.sent),1)

    def test_pagination_preserves_all_case_versions_and_byte_limit(self):
        cases=[dict(case(i),title='中'*55,excerpt='文'*75) for i in range(30)]
        pages=build_messages('2026-11-01',cases,self.settings.origin,pending=5)
        self.assertGreater(len(pages),1)
        self.assertTrue(all(len(p['content'].encode())<=4096 for p in pages))
        self.assertEqual([p for page in pages for p in page['versions']],[[c['id'],c['version']] for c in cases])
        self.daily.initialize(clock(),[])
        for index in range(len(pages)):
            self.daily.tick(clock(hour=11,minute=index),cases)
            self.daily.tick(clock(hour=11,minute=index)+5,cases)
            self.assertEqual(len(self.sent),index+1)
        self.assertEqual(self.daily.unseen(cases),[])

    def test_unknown_result_not_resent_after_restart_or_next_day(self):
        calls=[]
        def timeout(request):calls.append(1);raise httpx.ReadTimeout('do not log '+str(request.url))
        self.daily=DailyDigest(self.settings,self.store,httpx.MockTransport(timeout))
        self.daily.initialize(clock(),[])
        self.daily.tick(clock(hour=11),[case()])
        self.assertEqual(self.daily.status()['last_delivery']['state'],'uncertain')
        self.assertNotIn('secret',self.daily.status()['last_delivery']['error'])
        self.daily.tick(clock(hour=12),[case()])
        self.assertEqual(len(calls),1)
        recovered=DailyDigest(self.settings,self.store,self.transport)
        recovered.tick(clock(day=2,hour=11),[case()])
        self.assertNotIn('#case=case1',self.sent[0]['markdown']['content'])
        self.assertEqual(recovered.status()['uncertain_messages'],1)

    def test_explicit_failure_retries_at_most_three_times(self):
        calls=[]
        def reject(request):calls.append(1);return httpx.Response(200,json={'errcode':45009,'errmsg':'secret text must not be logged'})
        self.daily=DailyDigest(self.settings,self.store,httpx.MockTransport(reject))
        self.daily.initialize(clock(),[])
        for minute in [0,1,5,10,20]:self.daily.tick(clock(hour=11,minute=minute),[case()])
        self.assertEqual(len(calls),3)
        self.assertEqual(self.daily.status()['last_delivery']['state'],'failed')
        self.assertEqual(self.daily.unseen([case()]),[case()])

    def test_concurrent_claim_and_expired_lease_are_not_duplicate_sends(self):
        def reenter(request):
            self.sent.append(1)
            DailyDigest(self.settings,self.store,self.transport).tick(clock(hour=11),[case()])
            return httpx.Response(200,json={'errcode':0})
        self.daily=DailyDigest(self.settings,self.store,httpx.MockTransport(reenter))
        self.daily.initialize(clock(),[])
        self.daily.tick(clock(hour=11),[case()])
        self.assertEqual(self.sent,[1])
        with self.store.db(True) as db:
            db.execute("UPDATE digest_messages SET state='sending',lease=?",(clock(hour=11),))
        self.daily.tick(clock(hour=12),[case()])
        self.assertEqual(self.daily.status()['last_delivery']['state'],'uncertain')

    def test_invalid_success_body_is_uncertain_not_crash(self):
        self.daily=DailyDigest(self.settings,self.store,httpx.MockTransport(lambda _:httpx.Response(200,json=[])))
        self.daily.initialize(clock(),[])
        self.daily.tick(clock(hour=11),[case()])
        self.assertEqual(self.daily.status()['last_delivery']['state'],'uncertain')

    def test_markdown_injection_is_plain_text_and_no_secret_status(self):
        page=build_messages('2026-11-01',[dict(case(),title='[伪造](https://bad.test) @all <script>')],self.settings.origin)[0]
        self.assertNotIn('](https://bad.test)',page['content'])
        self.assertNotIn('@all',page['content'])
        self.assertNotIn('<script>',page['content'])
        self.assertNotIn('test-secret-key',json.dumps(self.daily.status()))


class DailyBatchTests(unittest.TestCase):
    setUp=fixtures.AutomationTests.setUp
    tearDown=fixtures.AutomationTests.tearDown
    session=fixtures.AutomationTests.session
    add_part=fixtures.AutomationTests.add_part
    complete_asr=fixtures.AutomationTests.complete_asr
    business=fixtures.AutomationTests.business
    history_transcript=fixtures.AutomationTests.history_transcript

    def enable(self):
        self.settings.daily_batch_mode=True
        self.auto=Automation(self.settings,self.store)
        self.auto.tick(clock())

    def touch(self,folder,when):
        for p in folder.glob('*.m4a'):os.utime(p,(when,when))

    def test_daily_snapshot_once_waits_asr_and_ignores_late_audio(self):
        self.enable()
        folder,body=self.session(files=2)
        self.touch(folder,clock(hour=8,minute=30))
        self.auto.tick(clock(hour=8,minute=45))
        self.assertEqual(self.store.jobs(),[])
        self.auto.tick(clock(hour=9))
        self.assertEqual(len(self.store.jobs()),2)
        self.add_part(folder,body,2)
        os.utime(folder/'part_0002.m4a',(clock(hour=9,minute=5),)*2)
        self.auto.tick(clock(hour=9,minute=10))
        self.assertEqual(len(self.store.jobs()),2)
        self.complete_asr(duration=1000)
        self.auto.tick(clock(hour=9,minute=11))
        jobs=self.business()
        self.assertEqual(len(jobs),1)
        self.assertEqual(len(jobs[0]['payload']['transcript_ids']),2)
        self.assertTrue(jobs[0]['payload']['all_cases'])
        self.assertEqual(jobs[0]['payload']['daily_batch_day'],'2026-11-01')
        Automation(self.settings,self.store).tick(clock(hour=10))
        self.assertEqual(len(self.business()),1)
        self.auto.tick(clock(day=2,hour=9))
        self.assertEqual(len([j for j in self.store.jobs() if j['mode']=='transcribe']),3)

    def test_already_transcribed_audio_reused_without_new_asr(self):
        self.enable()
        folder,_=self.session();self.touch(folder,clock())
        body=self.history_transcript(scan(self.settings.recordings)[0],'saved-text')
        self.auto.tick(clock(hour=9))
        self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(self.business()[0]['payload']['transcript_ids'],[body['id']])

    def test_blocked_legacy_generation_does_not_block_new_daily_audio(self):
        folder,body=self.session();self.touch(folder,clock())
        self.auto.backfill_history(clock())
        self.auto.tick(clock());self.complete_asr()
        self.auto.tick(clock()+60)
        legacy=self.business()[0]
        with self.store.db(True) as db:db.execute("UPDATE jobs SET state='awaiting_budget',error='本批预算不足' WHERE id=?",(legacy['id'],))
        self.enable()
        self.add_part(folder,body,1)
        os.utime(folder/'part_0001.m4a',(clock(hour=8,minute=30),)*2)
        self.auto.tick(clock(hour=9));self.complete_asr()
        self.auto.tick(clock(hour=9,minute=1))
        daily=[j for j in self.business() if j['id']!=legacy['id']]
        self.assertEqual(len(daily),1)
        self.assertEqual(len(daily[0]['payload']['transcript_ids']),1)
        self.assertNotIn('previous_job_id',daily[0]['payload'])
        self.assertEqual(self.store.job(legacy['id'])['state'],'awaiting_budget')

    def test_late_repaired_asr_is_included_in_later_daily_batch(self):
        self.enable();folder,_=self.session(files=2);self.touch(folder,clock())
        self.auto.tick(clock(hour=9));self.complete_asr()
        with self.store.db(True) as db:
            asset=db.execute('SELECT * FROM automation_assets ORDER BY asset_key LIMIT 1').fetchone()
            db.execute("UPDATE automation_assets SET state='failed',retry_count=3 WHERE asset_key=?",(asset['asset_key'],))
            db.execute("UPDATE jobs SET state='failed' WHERE id=?",(asset['asr_job'],))
        self.auto.tick(clock(hour=9,minute=1))
        first=self.business()[0]
        self.assertEqual(len(first['payload']['transcript_ids']),1)
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?",(packed({'transcript_ids':first['payload']['transcript_ids']}),first['id']))
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(asset['asr_job'],))
        self.auto.tick(clock(day=2,hour=9))
        self.auto.tick(clock(day=2,hour=9,minute=1))
        later=[j for j in self.business() if j['id']!=first['id']]
        self.assertEqual(len(later),1)
        self.assertEqual(len(later[0]['payload']['transcript_ids']),2)
        self.auto.tick(clock(day=2,hour=10))
        self.assertEqual(len(self.business()),2)

    def test_future_day_asset_cannot_dispatch_before_nine(self):
        self.enable();folder,_=self.session();self.touch(folder,clock())
        self.auto.tick(clock(hour=9));self.complete_asr()
        with self.store.db(True) as db:db.execute("UPDATE automation_daily_assets SET day='2026-11-02'")
        self.auto.tick(clock(day=2,hour=0,minute=1))
        self.assertEqual(self.business(),[])
        self.auto.tick(clock(day=2,hour=9))
        self.assertEqual(len(self.business()),1)

    def test_daily_revision_next_day_replaces_old_transcript(self):
        self.enable();folder,_=self.session();self.touch(folder,clock())
        self.auto.tick(clock(hour=9));self.complete_asr();self.auto.tick(clock(hour=9,minute=1))
        first=self.business()[0];old_tid=first['payload']['transcript_ids'][0]
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?",(packed({'transcript_ids':[old_tid]}),first['id']))
        original=self.store.transcript(old_tid)
        revised=dict(original,id='daily-revised',revision=2,segments=[dict(original['segments'][0],text='客户希望先审核订单，再确认业务结果。')])
        self.store.save_transcript(revised,parent=old_tid)
        self.auto.record_revision(old_tid,revised['id'],clock(hour=12))
        self.auto.tick(clock(hour=13))
        self.assertEqual(len(self.business()),1)
        self.auto.tick(clock(day=2,hour=9));self.auto.tick(clock(day=2,hour=9,minute=1))
        new=next(j for j in self.business() if j['id']!=first['id'])
        self.assertEqual(new['payload']['transcript_ids'],['daily-revised'])

    def test_legacy_budget_recovery_waits_for_daily_work_and_eleven(self):
        self.enable()
        old=self.store.create_job('business',dict(transcript_ids=['history-text'],automatic=True,history_backfill=True))
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='awaiting_budget',error='每日预算不足',updated=? WHERE id=?",(clock()-86400,old['id']))
        self.auto._resume_daily_budget(clock(hour=0))
        self.assertEqual(self.store.job(old['id'])['state'],'awaiting_budget')
        self.auto._resume_daily_budget(clock(hour=10))
        self.assertEqual(self.store.job(old['id'])['state'],'awaiting_budget')
        daily=self.store.create_job('business',dict(transcript_ids=['daily-text'],automatic=True,daily_batch_day='2026-11-01'))
        self.auto._resume_daily_budget(clock(hour=11))
        self.assertEqual(self.store.job(old['id'])['state'],'awaiting_budget')
        with self.store.db(True) as db:db.execute("UPDATE jobs SET state='completed' WHERE id=?",(daily['id'],))
        self.auto._resume_daily_budget(clock(hour=11,minute=1))
        self.assertEqual(self.store.job(old['id'])['state'],'queued')

    def test_pausing_processing_keeps_scheduled_delivery(self):
        from unittest.mock import patch
        self.enable();self.auto.set_enabled(False)
        with patch.object(self.auto.daily,'tick') as tick:
            self.auto.tick(clock(hour=11))
        tick.assert_called_once()
        self.assertEqual(self.store.jobs(),[])


if __name__=='__main__':unittest.main()
