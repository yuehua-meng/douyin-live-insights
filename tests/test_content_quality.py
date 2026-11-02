import copy
import unittest

from analysis.content_quality import assess_transcript


class ContentQualityTests(unittest.TestCase):
    def body(self, *texts, status='ready', **extra):
        return dict(status=status, segments=[dict(text=t) for t in texts], **extra)

    def test_empty_completed_transcript_is_excluded(self):
        for body in [self.body(), self.body('', '……'), self.body(status='no_speech')]:
            with self.subTest(body=body):
                result=assess_transcript(body)
                self.assertEqual(result['decision'],'exclude')
                self.assertEqual(result['reason'],'no_recognized_text')

    def test_only_strict_fillers_are_excluded(self):
        result=assess_transcript(self.body('嗯，嗯。', '啊……', '呃，哦！'))
        self.assertEqual(result['decision'],'exclude')
        self.assertEqual(result['reason'],'filler_only')
        self.assertEqual(result['counts']['meaningful_segments'],0)

    def test_short_meaningful_answers_and_technical_terms_are_kept(self):
        for text in ['不', '是', '好', '对', '不是', '好啊', '15万元', 'API', 'Transformer', '额', 'AI']:
            with self.subTest(text=text):
                self.assertEqual(assess_transcript(self.body(text))['decision'],'keep')

    def test_exact_unintelligible_placeholders_are_excluded(self):
        for text in ['[听不清]', '【噪音】', '(inaudible)', '（背景噪声）', '[静音]', '[无法识别]']:
            with self.subTest(text=text):
                result=assess_transcript(self.body(text))
                self.assertEqual(result['decision'],'exclude')
                self.assertEqual(result['reason'],'unintelligible_only')

    def test_unbracketed_marker_words_are_real_short_statements(self):
        for text in ['听不清', '静音', '无法识别', '噪音，听不清。', 'noise', 'silence', 'inaudible']:
            with self.subTest(text=text):
                self.assertEqual(assess_transcript(self.body(text))['decision'],'keep')

    def test_raw_literal_marker_words_override_bracketed_punctuation_output(self):
        body=dict(status='ready',segments=[dict(text='[听不清]',raw_text='听不清')])
        self.assertEqual(assess_transcript(body)['decision'],'keep')

    def test_placeholders_mixed_with_fillers_are_excluded(self):
        result=assess_transcript(self.body('嗯，[听不清]。', '【噪音】啊'))
        self.assertEqual(result['decision'],'exclude')
        self.assertEqual(result['reason'],'filler_and_unintelligible_only')

    def test_any_real_context_prevents_exclusion(self):
        for texts in [('[听不清]', '客户需要整理订单'), ('嗯', '不是'),
                      ('客户说听不清这个需求',), ('听不清啊',), ('[客户需求]',),
                      ('语音不清会影响客户体验',), ('noise reduction',)]:
            with self.subTest(texts=texts):
                self.assertEqual(assess_transcript(self.body(*texts))['decision'],'keep')

    def test_duration_and_word_count_do_not_delete_real_content(self):
        self.assertEqual(assess_transcript(self.body('客户要退款', duration_ms=300))['decision'],'keep')
        self.assertEqual(assess_transcript(self.body('对', duration_ms=3600000))['decision'],'keep')

    def test_incomplete_or_invalid_transcripts_are_review_only(self):
        bodies=[None, {}, self.body('嗯',status='failed'), self.body('嗯',status='running'),
                dict(status='ready',segments=None), dict(status='ready',segments=[None]),
                dict(status='ready',segments=[dict(text=123)])]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(assess_transcript(body)['decision'],'review')

    def test_raw_asr_words_preserve_content_even_if_punctuation_lost_it(self):
        body=dict(status='ready',segments=[dict(text='嗯',raw_text='客户希望生成申报报告')])
        self.assertEqual(assess_transcript(body)['decision'],'keep')
        body['segments'][0]['text']=''
        self.assertEqual(assess_transcript(body)['decision'],'keep')

    def test_repetitions_are_reviewed_never_deleted(self):
        result=assess_transcript(self.body(*(['客户需要处理订单']*10)))
        self.assertEqual(result['decision'],'review')
        self.assertEqual(result['reason'],'repetitive_text')

    def test_assessment_is_deterministic_and_does_not_mutate(self):
        body=self.body('嗯', '[噪声]')
        original=copy.deepcopy(body)
        result=assess_transcript(body)
        self.assertEqual(result,assess_transcript(body))
        self.assertEqual(body,original)
        self.assertEqual(result['policy'],'transcript-content-v1')


if __name__=='__main__':
    unittest.main()
