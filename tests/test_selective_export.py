"""Offline integration tests for ordinary selective export and discovery."""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_exporter import cli, collect, db, export_state
from qq_exporter.selection import Selection


class SelectiveExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "fixture.db"
        self.out = self.root / "out"
        self.cols = ['40001', '40050', '40013', '40033', '40030', '40020', '40021', '40800']
        with contextlib.closing(sqlite3.connect(self.source)) as conn, conn:
            for table in ('group_msg_table', 'c2c_msg_table'):
                conn.execute(f'CREATE TABLE {table} (' + ','.join('"'+c+'" '+('BLOB' if c == '40800' else 'INTEGER' if c not in ('40020','40021') else 'TEXT') for c in self.cols) + ')')
            conn.executemany('INSERT INTO group_msg_table VALUES (?,?,?,?,?,?,?,?)', [
                (1,100,0,21,900,'u21','g900',b'a'),
                (2,200,0,22,901,'u22','g901',b'b'),
                (3,300,0,21,900,'u21','g900',b'c')])
            conn.executemany('INSERT INTO c2c_msg_table VALUES (?,?,?,?,?,?,?,?)', [
                (1,100,1,11,21,'u11','u21',b'd'),
                (2,200,0,21,11,'u21','u11',b'e'),
                (3,300,1,11,22,'u11','u22',b'f')])
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def args(self, *extra):
        return cli.build_parser().parse_args(['export','--db',str(self.source),'--out',str(self.out),'--format','json',*extra])

    def run_export(self, *extra):
        return cli.cmd_export(self.args(*extra))

    def index(self):
        return json.loads((self.out/'json'/'index.json').read_text(encoding='utf-8'))

    def test_single_and_multiple_selection_parse_only_matches(self):
        with patch.object(db, 'parse_body', wraps=db.parse_body) as parse, patch.object(db, 'extract_media', return_value=[]) as media:
            meta, convs = collect.build_conversations(self.source, selection=Selection(('group:900',)))
        self.assertEqual(meta['message_count'], 2)
        self.assertEqual([c.key for c in convs], ['group:900'])
        self.assertEqual([c.args[0] for c in parse.call_args_list], [b'a', b'c'])
        self.assertEqual(media.call_count, 2)
        meta, convs = collect.build_conversations(self.source, selection=Selection(('group:900','group:901'), after=200, before=301))
        self.assertEqual(meta['message_count'],2)
        self.assertEqual({c.key for c in convs}, {'group:900','group:901'})

    def test_c2c_both_directions_and_limit_after_matching(self):
        meta, convs = collect.build_conversations(self.source, selection=Selection(('c2c:21',)), limit=2)
        self.assertEqual([m['msg_id'] for m in convs[0].messages], [1,2])
        self.assertEqual(convs[0].key,'c2c:21')
        meta, convs = collect.build_conversations(self.source, selection=Selection(('group:900',), after=200), limit=1)
        self.assertEqual(convs[0].messages[0]['msg_id'],3)

    def test_group_skips_identity_and_private_ambiguous_requires_explicit(self):
        with patch('qq_exporter.selection.infer_self', side_effect=AssertionError('group must not infer')):
            collect.build_conversations(self.source, selection=Selection(('group:900',)))
        with contextlib.closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('UPDATE c2c_msg_table SET "40013"=0')
        with self.assertRaisesRegex(ValueError,'self-qq'):
            collect.build_conversations(self.source, selection=Selection(('c2c:21',)))
        meta, convs = collect.build_conversations(self.source, selection=Selection(('c2c:21',),self_qq='11'))
        self.assertEqual(meta['message_count'],2)

    def test_unknown_time_rejected_before_parse(self):
        with contextlib.closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('UPDATE group_msg_table SET "40050"=NULL WHERE "40001"=1')
        with patch.object(db,'parse_body', side_effect=AssertionError('no body parse')):
            with self.assertRaisesRegex(ValueError,'未知时间'):
                collect.build_conversations(self.source, selection=Selection(('group:900',),after=0))

    def test_scope_mismatch_and_unmarked_outputs(self):
        self.run_export('--conversation','group:900')
        before = (self.out/'json'/'index.json').read_bytes()
        for opts in (('--conversation','group:901'), ()):
            with self.assertRaisesRegex(ValueError,'新的 --out'):
                self.run_export(*opts)
        self.assertEqual((self.out/'json'/'index.json').read_bytes(), before)
        other = self.root/'old'
        other.mkdir()
        (other/'history.txt').write_text('history')
        args = self.args('--conversation','group:900')
        args.out = str(other)
        with self.assertRaisesRegex(ValueError,'标记'):
            cli.cmd_export(args)
        self.assertEqual((other/'history.txt').read_text(),'history')

    def test_incremental_zero_replaces_index_without_advancing_state(self):
        self.run_export('--conversation','group:900','--incremental')
        state = self.out/'.export_state.json'
        before = state.read_bytes()
        self.run_export('--conversation','group:900','--incremental')
        self.assertEqual(self.index()['conversations'],[])
        self.assertEqual(self.index()['meta']['message_count'],0)
        self.assertEqual(state.read_bytes(),before)

    def test_failed_export_and_invalid_format_do_not_advance(self):
        self.run_export('--conversation','group:900','--incremental')
        state = self.out/'.export_state.json'
        before = state.read_bytes()
        with patch.object(cli,'export_json', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                self.run_export('--conversation','group:900','--incremental','--since','0')
        self.assertEqual(state.read_bytes(),before)
        args = self.args('--format','yaml','--incremental')
        args.out = str(self.root/'invalid')
        with self.assertRaises(ValueError):
            cli.cmd_export(args)
        self.assertFalse(Path(args.out).exists())

    def test_legacy_and_explicit_minid_all_tables(self):
        self.out.mkdir()
        state = self.out/'.export_state.json'
        state.write_text(json.dumps({'last_ids': {'c2c_msg_table': 100}}))
        self.run_export('--incremental','--since','1')
        self.assertEqual(self.index()['meta']['message_count'],4)
        self.assertEqual(json.loads(state.read_text())['version'],2)
        meta, convs = collect.build_conversations(self.source,min_ids={'*':2})
        self.assertEqual(meta['message_count'],2)
        with contextlib.closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('ALTER TABLE group_msg_table RENAME COLUMN "40001" TO "unrecognized"')
        with self.assertRaisesRegex(ValueError,'ID'):
            collect.build_conversations(self.source,min_ids={'*':0})

    def test_explain_and_all_explain_have_no_writes_or_body_parsing(self):
        with patch.object(db,'parse_body',side_effect=AssertionError('parse')), patch.object(cli.keyfinder,'find_main_db',side_effect=AssertionError('keys')):
            self.assertEqual(self.run_export('--conversation','group:900','--explain-query'),0)
            self.assertFalse(self.out.exists())
            args = cli.build_parser().parse_args(['all','--explain-query'])
            with self.assertRaisesRegex(ValueError,'export --db'):
                cli.cmd_all(args)
            watch = cli.build_parser().parse_args(['watch','--db',str(self.source),'--out',str(self.out),'--conversation','group:900','--explain-query'])
            self.assertEqual(cli.cmd_watch(watch),0)
            self.assertFalse(self.out.exists())

    def test_conversations_metadata_only_json_and_names(self):
        with contextlib.closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('UPDATE group_msg_table SET "40050"=NULL WHERE "40001"=1')
        with patch.object(db,'parse_body',side_effect=AssertionError('parse')), patch.object(db,'extract_media',side_effect=AssertionError('media')):
            entries = collect.list_conversations(self.source,kind='group',names={'group:900':'name'})
            entry = entries[0]
            self.assertEqual((entry['key'],entry['title'],entry['message_count']),('group:900','name',2))
            self.assertEqual(entry['unknown_time_count'],1)
            self.assertEqual(entry['first_time'],300)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(['conversations','--db',str(self.source),'--json','--kind','group'])
            self.assertEqual(len(json.loads(out.getvalue())),2)

    def test_single_json_media_downloads_only_selected_and_zero_txt(self):
        def media(blob):
            return [{'type':'image', 'url':blob.decode()}]
        with patch.object(db,'extract_media',side_effect=media), patch.object(cli,'download_media',return_value={'copied':0,'downloaded':1,'skipped':0,'failed':0}) as download:
            self.run_export('--conversation','group:901','--single-json','--media','--incremental','--format','json,txt')
        self.assertEqual(download.call_count,1)
        self.assertEqual(download.call_args.args[0],[{'type':'image','url':'b'}])
        data = json.loads((self.out/'json'/'conversations.json').read_text(encoding='utf-8'))
        self.assertEqual([c['id'] for c in data['conversations']],['group:901'])
        self.run_export('--conversation','group:901','--single-json','--incremental','--format','json,txt')
        data = json.loads((self.out/'json'/'conversations.json').read_text(encoding='utf-8'))
        self.assertEqual(data['conversations'],[])
        self.assertIn('会话数量: 0',(self.out/'txt'/'_index.txt').read_text(encoding='utf-8'))

    def test_source_scope_and_mixed_seconds_milliseconds(self):
        self.run_export('--conversation','group:900')
        other = self.root/'other.db'
        other.write_bytes(self.source.read_bytes())
        args = self.args('--conversation','group:900')
        args.db = str(other)
        with self.assertRaisesRegex(ValueError,'不匹配'):
            cli.cmd_export(args)
        with contextlib.closing(sqlite3.connect(self.source)) as conn, conn:
            conn.execute('UPDATE group_msg_table SET "40050"=1700000000000 WHERE "40001"=1')
            conn.execute('UPDATE group_msg_table SET "40050"=1800000000 WHERE "40001"=3')
        entry = collect.list_conversations(self.source,kind='group')[0]
        self.assertEqual(entry['first_time'],1700000000)
        self.assertEqual(entry['last_time'],1800000000)

    def test_artifact_scope_switches_rejected(self):
        self.run_export('--conversation','group:900','--incremental')
        for opts in (('--format','json,txt'), ('--single-json',)):
            with self.assertRaisesRegex(ValueError,'范围'):
                self.run_export('--conversation','group:900','--incremental',*opts)
        other = self.root/'legacy-new'
        args = self.args('--incremental')
        args.out = str(other)
        cli.cmd_export(args)
        args.format = 'json,txt'
        with self.assertRaisesRegex(ValueError,'范围'):
            cli.cmd_export(args)

    def test_incremental_limit_rejected_before_actions(self):
        with self.assertRaisesRegex(ValueError,'limit'):
            self.run_export('--incremental','--limit','1')
        self.assertFalse(self.out.exists())
        with patch.object(cli.keyfinder,'find_main_db',side_effect=AssertionError('keys')):
            args = cli.build_parser().parse_args(['all','--incremental','--limit','1'])
            with self.assertRaisesRegex(ValueError,'limit'):
                cli.cmd_all(args)

    def test_failed_media_retains_watermark(self):
        self.run_export('--conversation','group:900','--incremental')
        before = (self.out/'.export_state.json').read_bytes()
        with patch.object(db,'extract_media',return_value=[{'type':'image','url':'offline'}]), patch.object(cli,'download_media',return_value={'copied':0,'downloaded':0,'skipped':0,'failed':1}):
            self.assertEqual(self.run_export('--conversation','group:900','--incremental','--since','0','--media'),1)
        self.assertEqual((self.out/'.export_state.json').read_bytes(),before)
        self.assertFalse(self.index()['meta']['media_complete'])

    def test_bad_checkpoint_types_rejected_and_atomic_failure_retains_previous(self):
        payload = export_state.scope_payload(self.source,Selection())
        path = self.root/'state.json'
        for bad in ({'version':3,'last_ids':{}},{'last_ids':{'x':1.5}}, {'last_ids':{'x':True}}, []):
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                export_state.load_checkpoint(path,payload,active=False)
        path.write_text('{"old":true}')
        with patch.object(export_state.os,'replace',side_effect=OSError('atomic failure')):
            with self.assertRaises(OSError):
                export_state.atomic_json(path,{'new':True})
        self.assertEqual(json.loads(path.read_text()),{'old':True})


if __name__ == '__main__':
    unittest.main(verbosity=2)
