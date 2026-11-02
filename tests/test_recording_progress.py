import unittest
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
from recorder import FFmpegProgress, resolve_stream, Offline


class ProgressTests(unittest.TestCase):
    def test_streamlink_offline_message_on_stdout(self):
        result=subprocess.CompletedProcess([],1,stdout='error: No playable streams found on this URL',stderr='')
        with patch('recorder.run_command',return_value=result):
            with self.assertRaises(Offline):
                resolve_stream(SimpleNamespace(room_url='https://live.douyin.com/example'))

    def test_buffered_silence_keeps_media_heartbeat(self):
        progress=FFmpegProgress()
        for sec in range(0,65,2):
            with patch('recorder.time.monotonic',return_value=sec+100):
                progress.feed(f'out_time_us={sec*1000000}\n')
            self.assertEqual(progress.tick(),sec+100)

    def test_frozen_or_invalid_progress_does_not_mask_stall(self):
        progress=FFmpegProgress()
        with patch('recorder.time.monotonic',return_value=100):
            progress.feed('out_time_us=1000000')
        with patch('recorder.time.monotonic',return_value=200):
            for value in ['out_time_us=1000000','out_time_us=N/A','out_time_us=-1','progress=continue']:
                progress.feed(value)
        self.assertEqual(progress.tick(),100)

    def test_timestamp_reset_at_segment_boundary_is_progress(self):
        progress=FFmpegProgress()
        progress.feed('out_time_us=1800000000')
        with patch('recorder.time.monotonic',return_value=300):
            progress.feed('out_time_us=1000000')
        self.assertEqual(progress.tick(),300)
