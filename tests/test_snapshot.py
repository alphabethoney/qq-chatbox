"""Synthetic snapshot tests; never stop or touch a running QQ."""
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from qq_exporter import snapshot as s

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='snapshot-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.source=self.root/'source.db'; self.source.write_bytes(b'synthetic data')
        self.work=self.root/'work'
    def test_provided_changes_rejected_before_commit(self):
        with self.assertRaises(s.SnapshotError):
            with s.open_snapshot(self.source,self.work,mode='provided') as snap:
                self.source.write_bytes(b'changed')
                snap.verify()
    def test_provided_sidecar_appearance_rejected(self):
        with self.assertRaises(s.SnapshotError):
            with s.open_snapshot(self.source,self.work,mode='provided'):
                Path(str(self.source)+'-wal').write_bytes(b'new')
    def test_space_shortage(self):
        with patch.object(s.shutil,'disk_usage',return_value=type('Disk',(),{'free':0})()):
            with self.assertRaises(s.SnapshotError):
                with s.open_snapshot(self.source,self.work,mode='provided',required_free_bytes=1): pass
        self.assertEqual(self.source.read_bytes(),b'synthetic data')
    @unittest.skipUnless(os.name=='nt','Windows locking')
    def test_running_qq_skips_without_shutdown(self):
        with patch.object(s,'_qq_running',return_value=True):
            with self.assertRaises(s.SourceBusy):
                with s.open_snapshot(self.source,self.work): pass
        self.assertFalse(any(self.work.glob('snapshot-*')))
    @unittest.skipUnless(os.name=='nt','Windows locking')
    def test_write_handle_conflict(self):
        with self.source.open('r+b'),patch.object(s,'_qq_running',return_value=False):
            with self.assertRaises(s.SourceBusy):
                with s.open_snapshot(self.source,self.work): pass
    @unittest.skipUnless(os.name=='nt','Windows locking')
    def test_locked_source_disallows_writer(self):
        with s._protected_file(self.source):
            with self.assertRaises(OSError): self.source.open('r+b')
    @unittest.skipUnless(os.name=='nt','Windows locking')
    def test_snapshot_survives_later_live_changes_cleanup_owned_only(self):
        self.work.mkdir(); unrelated=self.work/'unrelated'; unrelated.write_text('keep')
        with patch.object(s,'_qq_running',return_value=False):
            with s.open_snapshot(self.source,self.work) as snap:
                saved=snap.path
                self.source.write_bytes(b'new after copy')
                snap.verify()
                self.assertEqual(saved.read_bytes(),b'synthetic data')
        self.assertFalse(saved.exists()); self.assertEqual(unrelated.read_text(),'keep')
    @unittest.skipUnless(os.name=='nt','Windows locking')
    def test_nonempty_wal_rejected(self):
        Path(str(self.source)+'-wal').write_bytes(b'pending')
        with patch.object(s,'_qq_running',return_value=False):
            with self.assertRaises(s.SourceBusy):
                with s.open_snapshot(self.source,self.work): pass
    @unittest.skipUnless(os.name=='nt','Windows locking')
    def test_copy_failure_cleans_own_directory(self):
        with patch.object(s,'_qq_running',return_value=False),patch.object(s.shutil,'copyfileobj',side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                with s.open_snapshot(self.source,self.work): pass
        self.assertFalse(any(self.work.glob('snapshot-*')))
    @unittest.skipUnless(os.name=='nt','Windows locking')
    def test_sidecar_created_during_copy_rejected(self):
        original=s.shutil.copyfileobj
        def copying(src,dst,*args):
            original(src,dst,*args)
            Path(str(self.source)+'-journal').write_bytes(b'race')
        with patch.object(s,'_qq_running',return_value=False),patch.object(s.shutil,'copyfileobj',side_effect=copying):
            with self.assertRaises(s.SourceBusy):
                with s.open_snapshot(self.source,self.work): pass
    def test_process_probe_errors_fail_closed(self):
        with patch.object(s.subprocess,'run',side_effect=OSError('probe unavailable')):
            with self.assertRaises(s.SourceBusy): s._qq_running()

if __name__=='__main__': unittest.main()
