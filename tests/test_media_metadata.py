import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from media_metadata import cache_duration, cached_duration_ms
from analysis.catalog import scan


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='.test-media-',dir=Path(__file__).resolve().parents[1] if os.name=='nt' else None)
        self.root=Path(self.temp.name)/'recordings'
        folder=self.root/'owner'/'date'/'session';folder.mkdir(parents=True)
        self.path=folder/'part_0000.m4a';self.path.write_bytes(b'test audio')
        (folder/'session.json').write_text(json.dumps({'files':[{'name':self.path.name}]}))
    def tearDown(self):self.temp.cleanup()

    def test_probe_once_and_catalog_only_reads_cache(self):
        output=subprocess.CompletedProcess([],0,stdout=json.dumps({'format':{'duration':'1800.125'},'streams':[{}]}))
        with patch('media_metadata.subprocess.run',return_value=output) as probe:
            self.assertEqual(cache_duration(self.path),1800125)
            self.assertEqual(cache_duration(self.path),1800125)
            self.assertEqual(probe.call_count,1)
        with patch('media_metadata.subprocess.run',side_effect=AssertionError('HTTP scan must not probe')):
            self.assertEqual(scan(self.root,include_duration=True)[0]['duration_ms'],1800125)
            self.assertNotIn('duration_ms',scan(self.root)[0])

    def test_replaced_file_invalidates_duration(self):
        output=subprocess.CompletedProcess([],0,stdout='{"format":{"duration":"15"}}')
        with patch('media_metadata.subprocess.run',return_value=output):cache_duration(self.path)
        self.path.write_bytes(b'replacement has a different size')
        self.assertIsNone(cached_duration_ms(self.path))

    def test_probe_failure_is_unknown_not_zero(self):
        with patch('media_metadata.subprocess.run',side_effect=subprocess.TimeoutExpired('ffprobe',10)):
            self.assertIsNone(cache_duration(self.path))
        self.assertIsNone(scan(self.root,include_duration=True)[0]['duration_ms'])
