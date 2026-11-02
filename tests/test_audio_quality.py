import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from audio_quality import inspect_audio, check_entry, filtered, counts, quarantine_root
from analysis.catalog import scan


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='.test-quality-',dir=Path(__file__).resolve().parents[1] if os.name=='nt' else None)
        self.root=Path(self.tmp.name)/'recordings';self.folder=self.root/'owner'/'day'/'session';self.folder.mkdir(parents=True)
        self.path=self.folder/'part_0000.m4a';self.path.write_bytes(b'test audio')
        self.manifest=self.folder/'session.json'
        self.entry={'name':self.path.name,'bytes':10}
        self.session={'status':'stalled','files':[self.entry]}
        self.manifest.write_text(json.dumps(self.session))

    def tearDown(self):self.tmp.cleanup()

    def measurement(self,peak):
        return patch('audio_quality.subprocess.run',return_value=subprocess.CompletedProcess([],0,stderr=f'n_samples: 10000\nmax_volume: {peak} dB'))

    def test_quiet_but_audible_reconnection_retained(self):
        with self.measurement(-65):
            check_entry(self.manifest,self.session,self.entry,self.root)
        self.assertEqual(self.entry['quality']['decision'],'keep')
        self.assertTrue(self.path.is_file())

    def test_digital_silence_is_reversibly_isolated(self):
        with self.measurement(-91):
            check_entry(self.manifest,self.session,self.entry,self.root)
        self.assertTrue(filtered(self.entry))
        self.assertFalse(self.path.exists())
        saved=quarantine_root(self.root)/self.path.relative_to(self.root)
        self.assertEqual(saved.read_bytes(),b'test audio')
        self.assertTrue(saved.with_name(saved.name+'.audit.json').is_file())
        self.manifest.write_text(json.dumps(self.session))
        self.assertEqual(scan(self.root),[])
        self.assertEqual(counts(self.root)['quarantined'],1)
        self.assertFalse(check_entry(self.manifest,self.session,self.entry,self.root))

    def test_failed_inspection_keeps_original(self):
        with patch('audio_quality.subprocess.run',side_effect=subprocess.TimeoutExpired('ffmpeg',180)):
            self.assertEqual(inspect_audio(self.path)['decision'],'review')
        self.assertTrue(self.path.exists())

    def test_decoder_rejects_corrupt_file(self):
        with patch('audio_quality.subprocess.run',return_value=subprocess.CompletedProcess([],1,stderr='invalid data')):
            self.assertEqual(inspect_audio(self.path)['reason'],'decode_error_or_no_audio')

    def test_manifest_recovers_after_move_before_save(self):
        with self.measurement(-91):check_entry(self.manifest,self.session,self.entry,self.root)
        original=json.loads(self.manifest.read_text())
        self.assertTrue(check_entry(self.manifest,original,original['files'][0],self.root))
        self.assertTrue(filtered(original['files'][0]))

    def test_manual_restore_does_not_get_filtered_again(self):
        self.entry['quality']={'decision':'keep','reason':'manual_restore'}
        with patch('audio_quality.inspect_audio',side_effect=AssertionError('Should not inspect')):
            self.assertFalse(check_entry(self.manifest,self.session,self.entry,self.root))
