import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from analysis.asr import Engine, transcribe


class ShortTailTests(unittest.TestCase):
    def engine(self, intervals):
        engine = Engine.__new__(Engine)
        engine.vad = Mock(return_value=[intervals])
        engine.asr = Mock(return_value=[{'preds': '测试声音'}])
        engine.punc = Mock(return_value=['测试声音。'])
        return engine

    def test_19ms_tail_padded_without_recognizing_padding(self):
        engine = self.engine([[0, 1000]])
        samples = np.ones(312, dtype=np.float32)
        self.assertEqual(engine.recognize(samples), [])
        padded = engine.vad.call_args.args[0]
        self.assertEqual(len(padded), 16000)
        np.testing.assert_array_equal(padded[:312], samples)
        self.assertTrue(np.all(padded[312:] == 0))
        engine.asr.assert_not_called()

    def test_short_real_speech_is_retained_and_clipped(self):
        engine = self.engine([[0, 1000], [900, 1000]])
        samples = np.ones(4800, dtype=np.float32)
        result = engine.recognize(samples)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['end_ms'], 300)
        self.assertEqual(len(engine.asr.call_args.args[0]), len(samples))
        self.assertEqual(result[0]['text'], '测试声音。')

    def test_empty_and_full_windows(self):
        engine = self.engine([])
        self.assertEqual(engine.recognize(np.array([], dtype=np.float32)), [])
        engine.vad.assert_not_called()
        samples = np.zeros(960000, dtype=np.float32)
        engine.recognize(samples)
        self.assertIs(engine.vad.call_args.args[0], samples)

    def test_resume_30_cached_windows_then_tiny_tail(self):
        settings = SimpleNamespace(recordings='unused', data='unused', min_free_bytes=0, ffprobe='probe', ffmpeg='decode')
        store = Mock()
        prior = [{'start_ms': 0, 'end_ms': 1000, 'text': '已完成'}]
        store.cached.side_effect = [None] + [prior] * 30 + [None]
        probe = SimpleNamespace(returncode=0, stdout=json.dumps({'format': {'duration': '1800.0195'}, 'streams': [{}]}).encode())
        tail = SimpleNamespace(returncode=0, stdout=np.ones(312, dtype='<f4').tobytes())
        engine = self.engine([])
        progress = Mock()
        with patch('analysis.asr.safe_file', return_value='audio'), patch('analysis.asr.file_hash', return_value='hash'), \
             patch('analysis.asr.model_fingerprint', return_value='same-model'), \
             patch('analysis.asr.shutil.disk_usage', return_value=SimpleNamespace(free=100)), \
             patch('analysis.asr.subprocess.run', side_effect=[probe, tail]) as run:
            result = transcribe({'id': 'r1'}, settings, store, lambda: engine, lambda: None, progress)
        self.assertEqual(run.call_count, 2)  # Only probe and last decode; reuse first 30.
        self.assertEqual(len(result['segments']), 30)
        self.assertEqual(result['duration_ms'], 1800020)
        progress.assert_called_with(31, 31)
        store.cache.assert_called_once()
        store.save_transcript.assert_called_once()

    def empty_decode(self, duration, cached_windows, returncode=0, tail_checkpoint=None):
        settings = SimpleNamespace(recordings='unused', data='unused', min_free_bytes=0, ffprobe='probe', ffmpeg='decode')
        store = Mock()
        prior = [{'start_ms': 0, 'end_ms': 1000, 'text': '已完成'}]
        store.cached.side_effect = [None] + [prior] * cached_windows + [tail_checkpoint]
        probe = SimpleNamespace(returncode=0, stdout=json.dumps({'format': {'duration': str(duration)}, 'streams': [{}]}).encode())
        empty = SimpleNamespace(returncode=returncode, stdout=b'')
        loader = Mock()
        progress = Mock()
        with patch('analysis.asr.safe_file', return_value='audio'), patch('analysis.asr.file_hash', return_value='hash'), \
             patch('analysis.asr.model_fingerprint', return_value='same-model'), \
             patch('analysis.asr.shutil.disk_usage', return_value=SimpleNamespace(free=100)), \
             patch('analysis.asr.subprocess.run', side_effect=[probe, empty]) as run:
            result = transcribe({'id': 'r1'}, settings, store, loader, lambda: None, progress)
        return result, store, loader, progress, run

    def test_successful_empty_1ms_container_tail_keeps_30_cached_windows(self):
        result, store, loader, progress, run = self.empty_decode(1800.001,30)
        self.assertEqual(len(result['segments']),30)
        self.assertEqual(result['duration_ms'],1800001)
        self.assertEqual(result['decode_notes'][0]['duration_ms'],1)
        self.assertEqual(result['decode_notes'][0]['window'],31)
        self.assertEqual(result['decode_notes'][0]['kind'],'container_rounding_tail')
        loader.assert_not_called()
        progress.assert_called_with(31,31)
        self.assertEqual(run.call_count,2)
        store.cache.assert_called_once()
        store.save_transcript.assert_called_once()
        checkpoint=store.cache.call_args.args[2]
        self.assertEqual(checkpoint['segments'],[])
        resumed, cached_store, cached_loader, _, cached_run = self.empty_decode(1800.001,30,tail_checkpoint=checkpoint)
        self.assertEqual(resumed['decode_notes'],result['decode_notes'])
        self.assertEqual(len(resumed['segments']),30)
        self.assertEqual(cached_run.call_count,1)
        cached_store.cache.assert_not_called()
        cached_loader.assert_not_called()

    def test_100ms_boundary_accepted_but_longer_missing_tail_fails(self):
        result, _, loader, _, _ = self.empty_decode(1800.1,30)
        self.assertEqual(result['decode_notes'][0]['duration_ms'],100)
        loader.assert_not_called()
        for duration in (1800.101,1801,1805):
            with self.subTest(duration=duration), self.assertRaisesRegex(ValueError,'音频片段解码失败'):
                self.empty_decode(duration,30)

    def test_failed_ffmpeg_middle_empty_and_first_empty_are_not_ignored(self):
        for duration, cached, code in ((1800.001,30,1),(1800.001,29,0),(0.1,0,0),(60.001,0,0)):
            with self.subTest(duration=duration,cached=cached,code=code), self.assertRaisesRegex(ValueError,'音频片段解码失败'):
                self.empty_decode(duration,cached,code)


if __name__ == '__main__':
    unittest.main()
