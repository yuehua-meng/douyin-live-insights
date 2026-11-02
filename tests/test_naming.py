import copy
import json
import tempfile
import unittest
from pathlib import Path

from analysis.naming import backfill, enrich_result
from analysis.store import Store, packed


class NamingTests(unittest.TestCase):
    def test_later_legacy_summary_does_not_hide_existing_named_case(self):
        with tempfile.TemporaryDirectory(prefix='.test-analysis-', dir=Path(__file__).resolve().parents[1]) as tmp:
            store=Store(Path(tmp))
            script=store.create_job('script',{'transcript_ids':['t1']})
            summary=store.create_job('summary',{'transcript_ids':['t1']})
            with store.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',created=1,result=? WHERE id=?",
                    (packed(enrich_result({'transcript_ids':['t1'],'script':{'cases':[{'title':'客户咨询分派项目','text':'正文'}]}})),script['id']))
                db.execute("UPDATE jobs SET state='completed',created=2,result=? WHERE id=?",
                    (packed(enrich_result({'transcript_ids':['t1'],'summary':{'items':[{'heading':'客户需求','text':'客户需要分派咨询'}]}})),summary['id']))
            self.assertEqual(store.feed()[0]['job_id'],script['id'])
            self.assertEqual(store.feed()[0]['title'],'客户咨询分派项目')
            store.lock_output(summary['id'],True)
            self.assertEqual(store.feed()[0]['job_id'],summary['id'])

    def test_existing_case_name_and_prose_are_preserved(self):
        body = {'script': {'cases': [{'title': '资质申报报告生成 Agent', 'text': '客户需要整理申报材料。',
                                    'refs': ['E1'], 'stage': '方案讨论'}]},
                'summary': {'items': [{'heading': '概览', 'text': '客户需要通过资料生成申报报告。'}]}}
        original = copy.deepcopy(body)
        result = enrich_result(body)
        self.assertEqual(body, original)
        self.assertEqual(result['script'], body['script'])
        self.assertEqual(result['display_title'], '资质申报报告生成 Agent')
        self.assertEqual(result, enrich_result(result))

    def test_generic_case_uses_only_its_own_project_or_prose(self):
        body = {'script': {'cases': [
                    {'title': '项目案例口播 · abc12345', 'case_id': 'p2', 'text': '我们整理客户咨询。', 'refs': ['S2']},
                    {'title': '', 'text': '今天我们来聊一个项目。客户需要生成申报材料，先整理企业资料。', 'refs': ['S3']}]},
                'dossier': {'projects': [{'id': 'p1', 'title': '微信消息处理'}, {'id': 'p2', 'title': '客户咨询记录整理'}]}}
        result = enrich_result(body)
        self.assertEqual(result['script']['cases'][0]['title'], '客户咨询记录整理')
        self.assertIn('客户需要生成申报材料', result['script']['cases'][1]['title'])
        self.assertNotIn('微信', result['display_title'])
        self.assertIn('等 2 个案例', result['display_title'])
        for old, new in zip(body['script']['cases'], result['script']['cases']):
            self.assertEqual(old['text'], new['text']); self.assertEqual(old['refs'], new['refs'])

    def test_summary_without_case_is_named_as_summary(self):
        body = {'script': {'cases': [], 'reason': '当前无法确认项目的来源归属。'},
                'summary': {'items': [{'heading': '概览', 'text': '本段讨论客户的医疗咨询需求。'},
                                      {'heading': '话题：数字资产与融资方案', 'text': '这只是讨论。'}]}}
        result = enrich_result(body)
        self.assertTrue(result['display_title'].startswith('商业总结｜'))
        self.assertNotIn('概览', result['display_title'])
        self.assertIn('数字资产与融资方案', result['display_title'])
        self.assertNotIn('来源归属', result['display_title'])
        self.assertEqual(result['script'], body['script'])
        self.assertEqual(result, enrich_result(result))

    def test_empty_script_is_not_advertised_as_case(self):
        result = enrich_result({'script': {'cases': [], 'reason': '材料不足'},
                                'dossier': {'projects': [{'title': '家具制造销售流程讨论'}]}})
        self.assertEqual(result['display_title'], '口播待补充｜家具制造销售流程讨论')

    def test_long_summary_name_stays_idempotent(self):
        body = {'summary': {'items': [
            {'heading': '青海中小企业AI智能体试点：先低价验证再考虑中大型企业', 'text': '保留正文一'},
            {'heading': '青海目标行业：锂电和盐湖、原农牧产品', 'text': '保留正文二'}]}}
        result = enrich_result(body)
        self.assertEqual(result, enrich_result(result))
        self.assertEqual(result['summary']['items'], body['summary']['items'])
        self.assertLessEqual(len(result['display_title']), 45)

    def test_specific_headings_win_over_overview_and_old_auto_title(self):
        body = {'naming_version': 'content-names-v1', 'summary': {
            'title': '整场直播分两段：前半段围绕AI产品落地、第三方软件接入合规、远程控制… / 还有很多内容',
            'items': [
                {'heading': '概览', 'text': '整场直播分两段：前半段围绕AI产品落地、第三方软件接入合规、远程控制。'},
                {'heading': '事实', 'text': '原文提到商业项目。'},
                {'heading': '话题：医疗AI与集群协作', 'text': '保留一'},
                {'heading': '话题：数字资产与融资方案', 'text': '保留二'}]}}
        result = enrich_result(body)
        self.assertEqual(result['display_title'], '商业总结｜医疗AI与集群协作 / 数字资产与融资方案')
        self.assertEqual(result['naming_version'], 'content-names-v2')
        self.assertEqual(result, enrich_result(result))
        self.assertEqual(result['summary']['items'], body['summary']['items'])

    def test_overview_falls_back_to_actual_short_business_phrases(self):
        body = {'summary': {'items': [{'heading': '概览', 'text':
            '整场直播分两段：前半段围绕AI产品落地、第三方软件接入合规、远程控制，后半段讨论其他事情。'}]}}
        result = enrich_result(body)
        self.assertEqual(result['display_title'], '商业总结｜AI产品落地 / 第三方软件接入合规')
        self.assertLessEqual(len(result['display_title']), 45)

    def test_empty_or_filler_material_has_no_invented_topic(self):
        for text in ['嗯，嗯。', '原文无实质内容，只有“嗯”。', '未能识别有效业务信息。', '只有语气词，没有实质内容。']:
            result = enrich_result({'summary': {'items': [{'heading': '概览', 'text': text}]}})
            self.assertEqual(result['display_title'], '商业总结｜素材信息不足')
            self.assertEqual(result, enrich_result(result))

    def test_long_authored_case_title_is_preserved(self):
        title = '企业资质申报报告生成项目：从企业资料收集到申报材料整理与人工复核的完整实施过程'
        result = enrich_result({'script': {'cases': [{'title': title, 'text': '正文'}]}})
        self.assertEqual(result['display_title'], title)

    def test_generic_disagreement_heading_uses_actual_subject(self):
        result=enrich_result({'summary':{'items':[{'heading':'分歧与需核实','text':'讨论销售信任、选品定价与获客流程。'}]}})
        self.assertIn('销售信任',result['display_title'])
        self.assertNotIn('分歧与需核实',result['display_title'])

    def test_store_backfill_keeps_locks_archives_state_and_usage(self):
        with tempfile.TemporaryDirectory(prefix='.test-analysis-', dir=Path(__file__).resolve().parents[1]) as tmp:
            store = Store(Path(tmp))
            j = store.create_job('script', {'transcript_ids': ['t1']})
            body = {'transcript_ids': ['t1'], 'script': {'cases': [
                {'title': '项目案例口播', 'text': '客户需要一个销售线索整理方案。', 'refs': ['E1']}]}}
            with store.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',result=?,updated=123 WHERE id=?", (packed(body), j['id']))
            store.lock_output(j['id'], True)
            store.archive_script_jobs([j['id']], '先前重复稿保持隐藏')
            queued = store.create_job('script', {'transcript_ids': ['t2']})
            with store.db(True) as db:
                db.execute('UPDATE jobs SET result=? WHERE id=?', (packed(body), queued['id']))
            preview = backfill(store)
            self.assertEqual(preview['updated'], 1)
            self.assertEqual(store.job(j['id'])['result'], body)
            applied = backfill(store, apply=True)
            self.assertEqual(applied['renamed_cases'], 1)
            self.assertEqual(backfill(store, apply=True)['updated'], 0)
            current = store.job(j['id'])
            self.assertEqual(current['updated'], 123)
            self.assertEqual(current['state'], 'completed')
            self.assertEqual(current['payload'], j['payload'])
            self.assertTrue(store.locked(j['id']))
            self.assertNotIn(j['id'], [entry['job_id'] for entry in store.feed()])
            self.assertEqual(store.job(queued['id'])['result'], body)
            with store.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT count(*) FROM job_archives').fetchone()[0], 1)

    def test_new_worker_results_get_names_without_model_request(self):
        with tempfile.TemporaryDirectory(prefix='.test-analysis-', dir=Path(__file__).resolve().parents[1]) as tmp:
            store = Store(Path(tmp))
            j = store.create_job('business', {'transcript_ids': ['t1']})
            job_id, owner = store.claim('summarizing')
            result = {'transcript_ids': ['t1'], 'summary': {'items': [
                {'heading': '医疗咨询业务需求', 'text': '正文仍然保留。'}]}, 'script': {'cases': [], 'processing': {'processed': 2}}}
            store.update(job_id, owner, state='completed', result=result)
            self.assertEqual(store.feed()[0]['title'], '商业总结｜医疗咨询业务需求')
            self.assertEqual(store.feed()[0]['case_count'], 0)
            self.assertEqual(store.feed()[0]['case_processed'], 2)
            self.assertEqual(store.result_catalog()[0]['title'], '商业总结｜医疗咨询业务需求')
            self.assertEqual(store.job(j['id'])['result']['summary']['items'], result['summary']['items'])


if __name__ == '__main__':
    unittest.main()
