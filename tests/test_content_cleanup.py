import copy
from pathlib import Path
import tempfile
import unittest

from analysis.content_cleanup import cleanup,clean_result
from analysis.store import Store,packed


class ContentCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='.test-analysis-',dir=Path(__file__).resolve().parents[1])
        self.store=Store(Path(self.temp.name))
    def tearDown(self):self.temp.cleanup()
    def transcript(self,tid='bad',text='嗯。',rid='r1',mtime=123,parent=None):
        body=dict(id=tid,recording=dict(id=rid,path=rid+'.m4a',size=12,mtime_ns=mtime),status='ready',
                  segments=[dict(id=tid+':0',text=text,start_ms=0,end_ms=1000)])
        self.store.save_transcript(body,parent=parent);return body
    def test_latest_revision_controls_exact_physical_version(self):
        old=self.transcript();self.store.assess_content(old)
        self.assertTrue(self.store.content_filtered(old['recording']))
        fixed=self.transcript('fixed','客户需要整理订单。',parent='bad');self.store.assess_content(fixed)
        self.assertFalse(self.store.content_filtered(old['recording']))
        self.assertEqual(self.store.assess_content(old)['decision'],'exclude')
        self.assertFalse(self.store.content_filtered(old['recording']))
        changed=dict(old['recording'],mtime_ns=124)
        self.assertFalse(self.store.content_filtered(changed))
    def test_cleanup_archives_garbage_preserves_prose_and_is_idempotent(self):
        bad=self.transcript();job=self.store.create_job('both',{'transcript_ids':['bad']})
        result={'transcript_ids':['bad'],'summary':{'items':[{'heading':'概览','text':'只有嗯'}]}}
        with self.store.db(True) as db:db.execute("UPDATE jobs SET state='completed',result=? WHERE id=?",(packed(result),job['id']))
        preview=cleanup(self.store)
        self.assertEqual(preview['archived_jobs'],[job['id']]);self.assertTrue(self.store.feed())
        cleanup(self.store,True)
        self.assertEqual(self.store.feed(),[])
        self.assertEqual(self.store.job(job['id'])['result'],result)
        self.assertEqual(cleanup(self.store,True)['archived_jobs'],[])
        self.assertTrue(self.store.content_filtered(bad['recording']))
    def test_mixed_result_removes_only_entries_grounded_in_bad_recording(self):
        bad=self.transcript();good=self.transcript('good','客户希望自动整理订单。',rid='r2')
        result={'transcript_ids':['bad','good'],'summary':{
            'items':[{'heading':'无内容','text':'只有嗯','refs':['E1']},
                     {'heading':'订单整理','text':'需要自动整理订单','refs':['E2']}],
            'evidence':[{'id':'E1','recording_id':'r1'},{'id':'E2','recording_id':'r2'}]},
            'script':{'cases':[{'title':'订单整理项目','text':'完整稿件','refs':['S1']}],
                      'evidence':[{'id':'S1','recording_id':'r2'}]}}
        original=copy.deepcopy(result);filtered,archive=clean_result(result,[bad,good])
        self.assertFalse(archive);self.assertEqual(len(filtered['summary']['items']),1)
        self.assertEqual(filtered['script'],result['script']);self.assertEqual(result,original)
        self.assertEqual(filtered['summary']['items'][0]['text'],'需要自动整理订单')


if __name__=='__main__':unittest.main()
