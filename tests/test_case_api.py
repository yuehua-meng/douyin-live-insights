import unittest
from unittest.mock import patch

from tests.test_analysis import AnalysisTests


class CaseApiTests(unittest.TestCase):
    setUp = AnalysisTests.setUp
    tearDown = AnalysisTests.tearDown
    login = AnalysisTests.login

    def test_library_and_detail_require_auth(self):
        self.assertEqual(self.client.get('/api/cases').status_code, 401)
        self.assertEqual(self.client.get('/api/cases/example').status_code, 401)

    def test_reading_and_deep_link_do_not_create_model_jobs(self):
        self.login()
        before = self.store.jobs()
        item = {'id':'case-one','job_id':'job-one','case_index':0,'title':'客户咨询整理'}
        detail = dict(item,case={'title':item['title'],'text':'业务流程'},summary=None,summary_scope='unavailable')
        with patch('analysis.case_library.list_cases', return_value=[item]), patch('analysis.case_library.get_case', return_value=detail):
            self.assertEqual(self.client.get('/api/cases').json(), [item])
            self.assertEqual(self.client.get('/api/cases/case-one').json(), detail)
        self.assertEqual(self.store.jobs(), before)
        with patch('analysis.case_library.get_case', return_value=None):
            self.assertEqual(self.client.get('/api/cases/removed').status_code, 404)


if __name__ == '__main__':
    unittest.main()
