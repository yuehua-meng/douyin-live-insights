import json
import os
from unittest.mock import patch

import httpx

import unittest
from tests import test_analysis as fixtures
from analysis.script import generate, validate_script, char_count
from analysis.narration import NARRATION_VERSION, NARRATION_STYLE
from analysis.worker import process


class ScriptTests(unittest.TestCase):
    setUp = fixtures.AnalysisTests.setUp
    tearDown = fixtures.AnalysisTests.tearDown
    login = fixtures.AnalysisTests.login
    def transcript(self):
        return {'id': 'product-t1', 'revision': 1, 'recording': {'id': 'r1'}, 'segments': [
            {'id': 's1', 'start_ms': 0, 'end_ms': 20000,
             'text': '客户需要快速整理销售咨询记录。我们计划做一个上传录音、转写、整理需求并由销售确认的工具，还没上线。'}]}

    def run_model(self, outputs, topic=''):
        calls = []
        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'usage': {'prompt_tokens': 20, 'completion_tokens': 30},
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(outputs[len(calls)-1])}}]})
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-only'}):
            result = generate(self.settings, self.store, 'script-test', [self.transcript()], lambda: None,
                              lambda *_: None, httpx.MockTransport(respond), topic=topic)
        return result, calls

    def case(self, text='我们讨论的方案是，帮客户把销售咨询录音转成文字，整理需求后由销售确认。这还是计划，尚未上线。'):
        return {'cases': [{'title': '销售咨询整理', 'stage': '方案讨论', 'text': text, 'refs': ['E1']}], 'reason': ''}

    def extracted(self):
        return {'items': [{'heading': '销售咨询整理', 'text': '需求是整理咨询记录，计划转写并整理需求，由销售确认，未上线。', 'refs': ['E1']}]}

    def test_script_generation_cache_and_estimate(self):
        outputs = [self.extracted(), self.case(), self.case()]
        result, calls = self.run_model(outputs)
        self.assertEqual(len(calls), 3)
        self.assertGreater(result['estimated_seconds'], 0)
        self.assertEqual(result['prompt_version'], 'product-story-v6')
        self.assertEqual(result['transcript_ids'], ['product-t1'])
        self.assertEqual(result['cases'][0]['stage'], '方案讨论')
        self.assertEqual(result['evidence'][0]['text'], self.transcript()['segments'][0]['text'])
        again, cached_calls = self.run_model([])
        self.assertEqual(again, result)
        self.assertEqual(cached_calls, [])

    def test_detailed_story_is_not_shortened(self):
        text = '我们先梳理客户流程，再逐步验证并调整方案。' * 100
        result, calls = self.run_model([self.extracted(), self.case(text), self.case(text)])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['cases'][0]['text'], text)
        self.assertGreater(char_count(result), 300)
        self.assertGreater(result['estimated_seconds'], 120)
        self.assertEqual(calls[-1]['max_tokens'], 8000)

    def test_legacy_script_route_uses_current_style_in_write_and_review(self):
        outputs=[self.extracted(),self.case(),self.case()]
        with patch('analysis.script.NARRATION_VERSION','legacy-test-style'):
            self.run_model(outputs)
        result,calls=self.run_model([self.case(),self.case()])
        self.assertEqual(len(calls),2,'reuse extraction but invalidate both previous writing caches')
        self.assertEqual(result['narration_version'],NARRATION_VERSION)
        for call in calls:
            self.assertIn(NARRATION_STYLE,call['messages'][0]['content'])
            payload=json.loads(call['messages'][1]['content'])
            self.assertEqual(payload['evidence'][0]['text'],self.transcript()['segments'][0]['text'])
        self.assertIn('draft',json.loads(calls[-1]['messages'][1]['content']))
        again,cached_calls=self.run_model([])
        self.assertEqual(again,result)
        self.assertEqual(cached_calls,[])

    def test_review_can_remove_unsupported_draft_details(self):
        result, calls = self.run_model([self.extracted(), self.case('客户已经上线。'), self.case('我们讨论的方案尚未上线。')])
        self.assertEqual(result['cases'][0]['text'], '我们讨论的方案尚未上线。')
        self.assertEqual(calls[-1]['thinking'], {'type': 'enabled'})
        self.assertEqual(calls[-1]['reasoning_effort'], 'high')
        self.assertEqual(calls[-2]['thinking'], {'type': 'disabled'})
        review_payload = json.loads(calls[-1]['messages'][1]['content'])
        self.assertEqual(review_payload['draft']['cases'][0]['text'], '客户已经上线。')
        self.assertEqual(review_payload['evidence'][0]['text'], self.transcript()['segments'][0]['text'])

    def test_no_case_and_invalid_citation(self):
        result, calls = self.run_model([{'items': []}])
        self.assertEqual(result['cases'], [])
        self.assertEqual(result['estimated_seconds'], 0)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            validate_script(self.case(), {'different'})

    def test_topic_and_original_context_reach_writer(self):
        transcript = self.transcript()
        transcript['segments'] += [dict(id='s2', start_ms=20000, end_ms=40000, text='中途发现问题，调整录音上传方式。'),
                                  dict(id='s3', start_ms=40000, end_ms=60000, text='目前只完成试用，未全面上线。')]
        candidate = self.extracted()
        candidate['items'][0]['refs'] = ['E2']
        output = self.case()
        output['cases'][0]['refs'] = ['E2', 'E3']
        output['cases'][0]['missing_details'] = ['正式交付后的量化效果']
        with patch.object(self, 'transcript', return_value=transcript):
            result, calls = self.run_model([candidate, output, output], topic='销售咨询整理')
        payload = json.loads(calls[-1]['messages'][1]['content'])
        self.assertEqual(payload['requested_project'], '销售咨询整理')
        self.assertEqual([e['id'] for e in payload['evidence']], ['E1', 'E2', 'E3'])
        self.assertEqual(result['cases'][0]['missing_details'], ['正式交付后的量化效果'])
        self.assertEqual(result['requested_project'], '销售咨询整理')
        with self.assertRaises(ValueError):
            validate_script({'cases': output['cases']*2}, {'E2', 'E3'})

    def test_script_api_worker_export_and_revision_identity(self):
        self.store.save_transcript(self.transcript())
        h = self.login()
        response = self.client.post('/api/jobs', json={'mode': 'script', 'transcript_ids': ['product-t1']}, headers=h)
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        self.assertEqual(job['payload']['script_version'], 'product-story-v6')
        old = self.store.create_job('script', {'transcript_ids': ['product-t1']})
        self.assertNotEqual(old['id'], job['id'])
        self.store.cancel(old['id'])
        focused = self.client.post('/api/jobs', json={'mode': 'script', 'transcript_ids': ['product-t1'], 'script_topic': '销售咨询整理'}, headers=h).json()
        self.assertNotEqual(focused['id'], job['id'])
        self.assertEqual(focused['payload']['script_topic'], '销售咨询整理')
        self.store.cancel(focused['id'])
        self.assertEqual(job['stage'], 'summarizing')
        self.assertEqual(self.client.get('/api/jobs/'+job['id']+'/script.txt').status_code, 404)
        with patch('analysis.script.generate', return_value=self.case()) as generator:
            process('summarizing', self.settings, self.store, self.store.claim('summarizing'), lambda: self.fail('must not run ASR'))
        self.assertEqual(generator.call_args.args[3][0]['id'], 'product-t1')
        self.assertEqual(self.store.job(job['id'])['state'], 'completed')
        export = self.client.get('/api/jobs/'+job['id']+'/script.txt')
        self.assertEqual(export.status_code, 200)
        self.assertIn('我们讨论', export.text)
        revision = self.client.post('/api/transcripts/product-t1/revision', json={'texts': ['更新方案']}, headers=h).json()
        newjob = self.client.post('/api/jobs', json={'mode': 'script', 'transcript_ids': [revision['id']]}, headers=h).json()
        self.assertNotEqual(newjob['id'], job['id'])
