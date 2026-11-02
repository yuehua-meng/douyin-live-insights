import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis.automation import Automation
from analysis.catalog import scan
from analysis.settings import Settings
from analysis.store import Store, packed


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-auto-', dir=Path(__file__).resolve().parents[1] if os.name == 'nt' else None)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root/'recordings', self.root/'analysis')
        self.settings.recordings.mkdir()
        self.store = Store(self.settings.data)
        self.auto = Automation(self.settings, self.store)
        self.now = 1_800_000_000

    def tearDown(self):
        self.temp.cleanup()

    def session(self, name='120000_s1', start='2027-01-15T12:00:00+08:00', ended=None, state='recording', files=1):
        folder = self.settings.recordings/'owner'/'2027-01-15'/name
        folder.mkdir(parents=True, exist_ok=True)
        body = dict(douyin_id='owner', started_at=start, ended_at=ended, status=state,
                    files=[{'name': f'part_{i:04d}.m4a'} for i in range(files)], gaps=[])
        for item in body['files']:
            (folder/item['name']).write_bytes(b'audio')
        (folder/'session.json').write_text(json.dumps(body), encoding='utf-8')
        return folder, body

    def add_part(self, folder, body, i):
        name=f'part_{i:04d}.m4a'
        (folder/name).write_bytes(b'audio')
        body['files'].append({'name':name})
        (folder/'session.json').write_text(json.dumps(body),encoding='utf-8')

    def complete_asr(self, duration=1_799_000, speech=True):
        jobs=[j for j in self.store.jobs() if j['mode']=='transcribe' and j['state']=='queued']
        for job in jobs:
            recording=job['payload']['recordings'][0]
            tid='transcript-'+recording['id']
            self.store.save_transcript(dict(id=tid,recording=recording,status='ready' if speech else 'no_speech',
                duration_ms=duration,revision=1,segments=[dict(id=tid+':0',text='业务需求',start_ms=0,end_ms=1000)] if speech else []))
            with self.store.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?", (packed({'transcript_ids':[tid]}),job['id']))

    def business(self):
        return [j for j in self.store.jobs() if j['mode']=='business']

    def monitor(self, now, offline_since, state='offline'):
        (self.settings.recordings/'monitor.json').write_text(json.dumps(dict(observed_at=now,offline_since=offline_since,live_state=state,douyin_id='owner')),encoding='utf-8')

    def test_baseline_excludes_history_restart_discovers_once(self):
        folder,body=self.session(files=1)
        self.auto.tick(self.now)
        self.assertEqual(self.store.jobs(),[])
        self.add_part(folder,body,1)
        self.auto.tick(self.now+60)
        self.assertEqual(len(self.store.jobs()),1)
        self.assertTrue(self.store.jobs()[0]['payload']['automatic'])
        Automation(self.settings,self.store).tick(self.now+120)
        self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(self.auto.status()['pending'],0)

    def test_ready_historical_filler_is_filtered_without_asr_or_model_restart(self):
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        recording=scan(self.settings.recordings)[0]
        body=self.history_transcript(recording,'old-filler')
        body['segments'][0]['text']='嗯。'
        with self.store.db(True) as db:db.execute('UPDATE transcripts SET body=? WHERE id=?',(packed(body),body['id']))
        self.auto.backfill_history(self.now)
        self.auto.tick(self.now+60)
        self.assertEqual(self.store.jobs(),[])
        with self.store.db() as db:
            asset=dict(db.execute('SELECT * FROM automation_assets').fetchone())
        self.assertEqual(asset['state'],'no_speech')
        self.assertEqual(asset['transcript_id'],'old-filler')
        self.assertTrue(self.store.content_filtered(recording))
        self.assertEqual(self.auto.status()['history']['pending'],0)

    def test_old_ready_asset_rechecked_then_corrected_revision_can_restore(self):
        self.auto.tick(self.now);self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        self.auto.tick(self.now+60);self.complete_asr(duration=1000)
        # Simulate a ready registration written by the old scheduler.
        with self.store.db() as db:asset=dict(db.execute('SELECT * FROM automation_assets').fetchone())
        tid='transcript-'+asset['recording_id'];body=self.store.transcript(tid)
        body['segments'][0]['text']='嗯。'
        with self.store.db(True) as db:db.execute('UPDATE transcripts SET body=? WHERE id=?',(packed(body),body['id']))
        with self.store.db(True) as db:db.execute("UPDATE automation_assets SET state='ready',transcript_id=?",(tid,))
        self.auto.tick(self.now+120)
        with self.store.db() as db:self.assertEqual(db.execute('SELECT state FROM automation_assets').fetchone()[0],'no_speech')
        self.assertEqual(self.business(),[])
        revised=dict(body,id='corrected',revision=2,segments=[dict(body['segments'][0],text='客户要复核订单。')])
        self.store.save_transcript(revised,parent=tid)
        self.assertTrue(self.auto.record_revision(tid,'corrected',self.now+180))
        with self.store.db() as db:self.assertEqual(db.execute('SELECT state FROM automation_assets').fetchone()[0],'ready')
        self.assertFalse(self.store.content_filtered(revised['recording']))

    def test_filtered_last_revision_does_not_queue_empty_paid_job(self):
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        recording=scan(self.settings.recordings)[0]
        original=self.history_transcript(recording,'original')
        self.auto.backfill_history(self.now)
        edited=dict(original,id='empty-edit',revision=2,segments=[dict(original['segments'][0],text='嗯')])
        self.store.save_transcript(edited,parent='original')
        self.auto.record_revision('original','empty-edit',self.now+1)
        self.auto.tick(self.now+120)
        self.assertEqual(self.business(),[])
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM automation_revisions').fetchone()[0],0)

    def test_queue_full_keeps_registration_for_later(self):
        self.auto.tick(self.now)
        self.session()
        with patch.object(self.store,'create_job',side_effect=ValueError('任务队列已满')):
            self.auto.tick(self.now+60)
        self.assertEqual(self.auto.status()['pending'],1)
        self.auto.tick(self.now+120)
        self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(self.auto.status()['pending'],0)

    def test_nominal_30min_dispatches_once_and_ongoing_is_not_final(self):
        self.auto.tick(self.now);self.session()
        self.auto.tick(self.now+60);self.complete_asr()
        self.auto.tick(self.now+120)
        self.assertEqual(len(self.business()),1)
        job=self.business()[0]
        self.assertEqual(job['payload']['business_version'],'business-cases-v1')
        self.assertFalse(job['payload']['coverage']['final'])
        self.auto.tick(self.now+2000)
        self.assertEqual(len(self.business()),1)
        self.assertEqual(self.auto.status()['collections'][0]['state'],'open')

    def test_reconnects_share_collection_and_short_tail_waits_fresh_offline(self):
        self.auto.tick(self.now)
        from datetime import datetime, timezone
        def iso(sec):return datetime.fromtimestamp(sec,timezone.utc).isoformat()
        self.session('s1',iso(self.now),iso(self.now+100),'stalled')
        self.session('s2',iso(self.now+200),iso(self.now+300),'stream_ended')
        self.auto.tick(self.now+360);self.complete_asr(duration=200000)
        self.auto.tick(self.now+1000)
        self.assertEqual(self.business(),[],'No monitor must not invent confirmed offline')
        self.monitor(self.now+1000,self.now+300)
        self.auto.tick(self.now+1000)
        self.assertEqual(len(self.business()),1)
        self.assertEqual(len(self.business()[0]['payload']['transcript_ids']),2)
        self.assertTrue(self.business()[0]['payload']['coverage']['final'])
        self.assertEqual(len(self.auto.status()['collections']),1)

    def test_30min_no_file_gap_never_flushes_still_recording(self):
        self.auto.tick(self.now);self.session()
        self.auto.tick(self.now+60);self.complete_asr(duration=120000)
        self.monitor(self.now+2000,self.now,'offline')
        self.auto.tick(self.now+2000)
        self.assertEqual(self.business(),[])
        self.assertEqual(self.auto.status()['collections'][0]['state'],'open')

    def test_stale_monitor_and_unfinished_segment_block_finalization(self):
        self.auto.tick(self.now)
        from datetime import datetime, timezone
        iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
        folder,body=self.session(ended=iso(self.now),state='stream_ended')
        self.auto.tick(self.now+60);self.complete_asr(duration=120000)
        self.monitor(self.now,self.now-600)
        self.auto.tick(self.now+1000)
        self.assertEqual(self.business(),[])
        self.monitor(self.now+1000,self.now-600)
        body['gaps']=[{'reason':'unfinished_segment'}]
        (folder/'session.json').write_text(json.dumps(body),encoding='utf-8')
        self.auto.tick(self.now+1000)
        self.assertEqual(self.business(),[])

    def test_non_speech_and_disabled_mode_do_not_submit_model(self):
        self.auto.tick(self.now)
        self.auto.set_enabled(False);self.session()
        self.auto.tick(self.now+60);self.assertEqual(self.store.jobs(),[])
        self.auto.set_enabled(True);self.auto.tick(self.now+120)
        self.complete_asr(speech=False);self.auto.tick(self.now+180)
        self.assertEqual(self.business(),[])
        self.assertEqual(self.auto.status()['transcribing'],0)

    def test_generation_crash_window_uses_same_identity(self):
        self.auto.tick(self.now);self.session();self.auto.tick(self.now+60);self.complete_asr()
        self.auto._sync_assets(self.now+120)
        original=self.store.create_job
        created=[]
        def crash_after_create(mode,payload):
            value=original(mode,payload);created.append(value)
            if mode=='business':raise RuntimeError('process stopped after job committed')
            return value
        with patch.object(self.store,'create_job',side_effect=crash_after_create):
            with self.assertRaises(RuntimeError):self.auto.tick(self.now+120)
        self.auto.tick(self.now+180)
        self.assertEqual(len(self.business()),1)

    def test_saved_revision_debounces_and_keeps_billing_group(self):
        self.auto.tick(self.now);self.session();self.auto.tick(self.now+60);self.complete_asr();self.auto.tick(self.now+120)
        job=self.business()[0]
        with self.store.db(True) as db:db.execute("UPDATE jobs SET state='completed' WHERE id=?",(job['id'],))
        old=job['payload']['transcript_ids'][0]
        body=self.store.transcript(old);new=dict(body,id='revision',revision=2)
        self.store.save_transcript(new,parent=old)
        self.assertTrue(self.auto.record_revision(old,'revision',self.now+200))
        self.auto.tick(self.now+250);self.assertEqual(len(self.business()),1)
        self.auto.tick(self.now+270);self.assertEqual(len(self.business()),2)
        latest=self.business()[0]
        self.assertEqual(latest['payload']['transcript_ids'],['revision'])
        self.assertEqual(latest['payload']['billing_group_id'],job['id'])

    def test_model_failure_blocks_paid_replacement_for_same_collection(self):
        self.auto.tick(self.now);folder,body=self.session();self.auto.tick(self.now+60);self.complete_asr();self.auto.tick(self.now+120)
        with self.store.db(True) as db:db.execute("UPDATE jobs SET state='failed',error='请求结果未知' WHERE mode='business'")
        self.add_part(folder,body,1);self.auto.tick(self.now+2000);self.complete_asr();self.auto.tick(self.now+2060)
        self.assertEqual(len(self.business()),1)

    def test_asr_retry_backoff_is_bounded_and_cancel_is_respected(self):
        self.auto.tick(self.now);self.session();self.auto.tick(self.now+60)
        job=self.store.jobs()[0]
        with self.store.db(True) as db:db.execute("UPDATE jobs SET state='failed' WHERE id=?",(job['id'],))
        self.auto.tick(self.now+100)
        self.assertEqual(self.store.job(job['id'])['state'],'failed')
        self.auto.tick(self.now+159)
        self.assertEqual(self.store.job(job['id'])['state'],'failed')
        self.auto.tick(self.now+161)
        self.assertEqual(self.store.job(job['id'])['state'],'queued')
        self.store.cancel(job['id']);self.auto.tick(self.now+200)
        self.assertEqual(self.store.job(job['id'])['state'],'cancelled')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT state FROM automation_assets').fetchone()[0],'cancelled')

    def test_only_known_daily_budget_wait_retries_next_day(self):
        jobs=[]
        for i,error in enumerate(['每日预算不足，等待恢复','本批预算不足，需处理','每日预算不足，存在未决请求']):
            job=self.store.create_job('business',{'transcript_ids':[str(i)],'automatic':True})
            with self.store.db(True) as db:
                db.execute("UPDATE jobs SET state='awaiting_budget',error=?,updated=? WHERE id=?",(error,self.now-86400,job['id']))
                if i==2:
                    db.execute("INSERT INTO usage(id,job_id,day,state,reserved,created) VALUES(?,?,?,'uncertain',10,?)",('uncertain',job['id'],'test',self.now))
            jobs.append(job)
        with patch.object(self.store,'spending',return_value={'committed':0}):
            self.auto._resume_daily_budget(self.now)
        self.assertEqual(self.store.job(jobs[0]['id'])['state'],'queued')
        self.assertEqual(self.store.job(jobs[1]['id'])['state'],'awaiting_budget')
        self.assertEqual(self.store.job(jobs[2]['id'])['state'],'awaiting_budget')

    def history_transcript(self, recording, tid, duration=120000, parent=None):
        body = dict(id=tid, recording=recording, status='ready', duration_ms=duration,
                    revision=2 if parent else 1,
                    segments=[dict(id=tid+':0', text='客户的业务需求和交付方案', start_ms=0, end_ms=duration)])
        self.store.save_transcript(body, parent=parent)
        return body

    def test_history_reuses_latest_revision_and_flushes_short_ended_snapshot(self):
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        recording=scan(self.settings.recordings)[0]
        self.history_transcript(recording,'old')
        self.history_transcript(recording,'corrected',parent='old')
        self.auto.tick(self.now)
        result=self.auto.backfill_history(self.now+60)
        self.assertEqual(result['reused_transcripts'],1)
        self.assertEqual(self.store.jobs(),[],'registration never performs paid work')
        self.auto.tick(self.now+120)
        job=self.business()[0]
        self.assertEqual(job['payload']['transcript_ids'],['corrected'])
        self.assertTrue(job['payload']['all_cases'])
        self.assertTrue(job['payload']['coverage']['final'])
        self.assertEqual(self.auto.backfill_history(self.now+180)['newly_registered'],0)
        self.auto.tick(self.now+240)
        self.assertEqual(len(self.store.jobs()),1)

    def test_history_does_not_reuse_transcript_after_audio_changed(self):
        folder,_=self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        old=scan(self.settings.recordings)[0]
        self.history_transcript(old,'stale')
        (folder/'part_0000.m4a').write_bytes(b'new audio version')
        result=self.auto.backfill_history(self.now)
        self.assertEqual(result['reused_transcripts'],0)
        self.auto.tick(self.now+60)
        self.assertEqual([j['mode'] for j in self.store.jobs()],['transcribe'])

    def test_history_reuses_multifile_job_and_maps_each_result(self):
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended',files=2)
        recordings=scan(self.settings.recordings)
        job=self.store.create_job('transcribe',{'recordings':recordings})
        result=self.auto.backfill_history(self.now)
        self.assertEqual(result['reused_asr'],2)
        self.auto.tick(self.now+60)
        self.assertEqual(len(self.store.jobs()),1)
        ids=[]
        for i,recording in enumerate(recordings):
            tid='recording-'+str(i)
            ids.append(tid)
            self.history_transcript(recording,tid)
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?",(packed({'transcript_ids':ids}),job['id']))
        self.auto.tick(self.now+120)
        self.assertEqual(set(self.business()[0]['payload']['transcript_ids']),set(ids))
        with self.store.db() as db:
            linked={r['recording_id']:r['transcript_id'] for r in db.execute('SELECT * FROM automation_assets')}
        self.assertEqual(linked,{r['id']:tid for r,tid in zip(recordings,ids)})

    def test_history_keeps_cancelled_retries_old_failed_asr_and_reuses_completed_text(self):
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended',files=3)
        recordings=scan(self.settings.recordings)
        jobs=[]
        for i,recording in enumerate(recordings):
            job=self.store.create_job('both',{'recordings':[recording]})
            with self.store.db(True) as db:
                db.execute("UPDATE jobs SET state=?,stage=? WHERE id=?",
                           ('cancelled' if i==0 else 'failed','summarizing' if i==2 else 'transcribing',job['id']))
            jobs.append(job)
        self.history_transcript(recordings[2],'already-transcribed')
        result=self.auto.backfill_history(self.now)
        self.assertEqual(result['kept_cancelled'],1)
        self.assertEqual(result['restarted_failed'],1)
        self.assertEqual(result['reused_transcripts'],1)
        self.auto.tick(self.now+60)
        self.assertEqual(self.store.job(jobs[0]['id'])['state'],'cancelled')
        self.assertEqual(self.store.job(jobs[1]['id'])['state'],'failed')
        fresh=[j for j in self.store.jobs() if j['mode']=='transcribe']
        self.assertEqual(len(fresh),1)
        self.assertEqual(fresh[0]['payload']['recordings'][0]['id'],recordings[1]['id'])
        self.auto.backfill_history(self.now+100)
        self.complete_asr()
        self.auto.tick(self.now+3600)
        self.assertIn('already-transcribed',self.business()[0]['payload']['transcript_ids'])
        self.assertFalse(self.business()[0]['payload']['coverage']['final'])

    def test_history_summary_budget_wait_without_text_starts_only_missing_asr(self):
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        recording=scan(self.settings.recordings)[0]
        old=self.store.create_job('both',{'recordings':[recording]})
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='awaiting_budget',stage='summarizing' WHERE id=?",(old['id'],))
        self.auto.backfill_history(self.now)
        self.auto.tick(self.now+60)
        fresh=[j for j in self.store.jobs() if j['mode']=='transcribe']
        self.assertEqual(len(fresh),1)
        self.assertEqual(self.store.job(old['id'])['state'],'awaiting_budget')
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='failed' WHERE id=?",(fresh[0]['id'],))
            db.execute("UPDATE automation_assets SET state='failed',retry_count=3")
        self.auto.backfill_history(self.now+120)
        self.auto.tick(self.now+180)
        self.assertEqual(self.store.job(fresh[0]['id'])['state'],'failed')
        self.assertEqual(len(self.store.jobs()),2)

    def test_history_completed_all_case_review_without_cases_is_not_regenerated(self):
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        recording=scan(self.settings.recordings)[0]
        self.history_transcript(recording,'text')
        job=self.store.create_job('business',dict(transcript_ids=['text'],business_version='business-cases-v1',all_cases=True))
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?",(packed({'script':{'cases':[]}}),job['id']))
        self.auto.backfill_history(self.now)
        self.auto.tick(self.now+60);self.auto.tick(self.now+120)
        self.assertEqual(len(self.business()),1)
        self.assertEqual(self.auto.status()['history']['analyzed'],1)

    def test_history_batches_new_material_and_does_not_reset_failed_batch_budget(self):
        self.session(ended='2027-01-15T14:30:00+08:00',state='stream_ended',files=5)
        for i,recording in enumerate(scan(self.settings.recordings)):
            self.history_transcript(recording,'text-'+str(i),duration=1800000)
        self.auto.backfill_history(self.now)
        self.auto.tick(self.now+60)
        first=self.business()[0]
        self.assertEqual(len(first['payload']['transcript_ids']),2)
        self.assertFalse(first['payload']['coverage']['final'])
        self.assertEqual(self.auto.status()['collections'][0]['state'],'partial')
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(first['id'],))
        self.auto.tick(self.now+120)
        second=self.business()[0]
        self.assertEqual(len(second['payload']['transcript_ids']),4)
        self.assertEqual(second['payload']['previous_job_id'],first['id'])
        self.assertNotIn('billing_group_id',second['payload'])
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='awaiting_budget',error='本批预算不足' WHERE id=?",(second['id'],))
        self.auto.backfill_history(self.now+180)
        self.auto.tick(self.now+240)
        self.assertEqual(len(self.business()),2)
        self.assertEqual(self.auto.status()['collections'][0]['state'],'partial')
        self.assertEqual(self.auto.status()['history']['generated'],2)
        self.assertEqual(self.auto.status()['history']['pending'],3)

    def test_history_respects_global_pause_and_active_session(self):
        self.session()
        recording=scan(self.settings.recordings)[0]
        self.history_transcript(recording,'text')
        self.auto.set_enabled(False)
        self.auto.backfill_history(self.now)
        self.auto.tick(self.now+60)
        self.assertEqual(self.store.jobs(),[])
        self.auto.set_enabled(True)
        self.auto.tick(self.now+120)
        self.assertFalse(self.business()[0]['payload']['coverage']['final'])

    def test_history_upgrades_previous_automatic_generation_to_all_cases_once(self):
        self.auto.tick(self.now)
        self.session(ended='2027-01-15T12:03:00+08:00',state='stream_ended')
        self.auto.tick(self.now+60);self.complete_asr();self.auto.tick(self.now+120)
        first=self.business()[0]
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(first['id'],))
        self.auto.backfill_history(self.now+180)
        self.auto.tick(self.now+240)
        second=self.business()[0]
        self.assertEqual(len(self.business()),2)
        self.assertEqual(second['payload']['previous_job_id'],first['id'])
        self.assertTrue(second['payload']['all_cases'])
        self.assertNotEqual(second['payload']['generation_key'],first['payload']['generation_key'])
        with self.store.db(True) as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?",(second['id'],))
        self.auto.backfill_history(self.now+300);self.auto.tick(self.now+360)
        self.assertEqual(len(self.business()),2)
        self.assertEqual(self.auto.status()['collections'][0]['state'],'completed')


if __name__ == '__main__':
    unittest.main()
