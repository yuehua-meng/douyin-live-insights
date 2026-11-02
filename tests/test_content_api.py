import unittest
from tests import test_analysis as fixtures


class ContentApiTests(unittest.TestCase):
    setUp=fixtures.AnalysisTests.setUp
    tearDown=fixtures.AnalysisTests.tearDown
    login=fixtures.AnalysisTests.login

    def test_hidden_recording_cannot_be_resubmitted_from_stale_list(self):
        headers=self.login();recording=self.client.get('/api/recordings').json()[0]
        body=dict(id='filler',recording=recording,status='ready',revision=1,
            segments=[dict(id='filler:0',text='嗯。',start_ms=0,end_ms=1000)])
        self.store.save_transcript(body);self.store.assess_content(body)
        self.assertEqual(self.client.get('/api/recordings').json(),[])
        response=self.client.post('/api/jobs',json={'mode':'both','recording_ids':[recording['id']]},headers=headers)
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.store.jobs(),[])
        self.assertEqual(self.client.get('/api/status').json()['filtering']['content']['excluded'],1)

    def test_corrected_revision_restores_recording_without_old_cache_override(self):
        headers=self.login();recording=self.client.get('/api/recordings').json()[0]
        body=dict(id='filler',recording=recording,status='ready',revision=1,
            segments=[dict(id='filler:0',text='嗯。',start_ms=0,end_ms=1000)])
        self.store.save_transcript(body);self.store.assess_content(body)
        response=self.client.post('/api/transcripts/filler/revision',json={'texts':['客户需要自动整理订单。']},headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        self.store.assess_content(body)
        self.assertEqual(len(self.client.get('/api/recordings').json()),1)


if __name__=='__main__':unittest.main()
