import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import daily_private_sync as daily

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='daily-sync-test-'); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config={k:str(self.root/k) for k in ('live_db','key_file','work','private_index','target_index','backup_root','build_config')}
        self.config.update(self_qq='11',conversation='c2c:22')
        self.path=self.root/'config.json'; self.path.write_text(json.dumps(self.config))
    def call(self,**kwargs):
        with contextlib.redirect_stdout(io.StringIO()),patch.dict(os.environ,{},clear=False):
            return daily.run(self.path,**kwargs)
    def report(self):
        p=next(Path(self.config['work']).glob('daily-report-*.json'))
        return json.loads(p.read_text(encoding='utf-8'))
    def test_busy_does_not_publish(self):
        with patch.object(daily,'sync',side_effect=daily.SourceBusy('busy')),patch('tools.publish_conversation.publish') as publish:
            self.assertEqual(self.call(),3); publish.assert_not_called()
        self.assertEqual(self.report()['status'],'source_busy')
    def test_sync_failure_keeps_publication_unattempted(self):
        with patch.object(daily,'sync',side_effect=RuntimeError('private secret')),patch('tools.publish_conversation.publish') as publish:
            self.assertEqual(self.call(),1); publish.assert_not_called()
        self.assertNotIn('private secret',json.dumps(self.report()))
    def test_local_hash_forced_and_scope_preserved(self):
        def sync(**kwargs):
            self.assertEqual(kwargs['conversation'],'c2c:22')
            self.assertEqual(kwargs['snapshot_mode'],'offline-copy')
            self.assertEqual(os.environ['QQ_CHATBOT_EMBEDDING_PROVIDER'],'hash')
            self.assertEqual(os.environ['QQ_CHATBOT_EMBEDDING_DIM'],'256')
            self.assertNotIn('host',kwargs)
            return {'last_msg_time':1}
        with patch.object(daily,'sync',side_effect=sync),patch('tools.publish_conversation.publish',return_value={'status':'published'}) as publish:
            self.assertEqual(self.call(),0); publish.assert_called_once()
    def test_publish_retry_does_not_resync(self):
        with patch.object(daily,'sync') as sync,patch('tools.publish_conversation.publish',return_value={'status':'skipped'}):
            self.assertEqual(self.call(publish_only=True),0); sync.assert_not_called()
    def test_no_target_does_not_publish(self):
        with patch.object(daily,'sync',return_value={'status':'no_target'}),patch('tools.publish_conversation.publish') as publish:
            self.assertEqual(self.call(),1); publish.assert_not_called()

if __name__=='__main__':unittest.main()
