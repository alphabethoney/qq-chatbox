"""Run isolated private sync, then preserve other conversations on publication."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.sync_index import sync, _atomic_write, _sync_lock
from qq_exporter.snapshot import SourceBusy


def run(config_path: Path, *, publish_only=False):
    config=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    required=('live_db','key_file','self_qq','conversation','work','private_index','target_index','backup_root','build_config')
    if any(not config.get(k) for k in required): raise ValueError('调度配置缺少必填字段')
    conversation=config['conversation']
    if not isinstance(conversation,str) or not conversation.startswith('c2c:'): raise ValueError('调度只支持私聊')
    # Never inherit an online embedding override from the interactive environment.
    for name in list(os.environ):
        if name.startswith(('QQ_CHATBOT_EMBEDDING_', 'QQ_CHATBOT_LLM_')): os.environ.pop(name)
    os.environ['QQ_CHATBOT_EMBEDDING_PROVIDER']='hash'
    os.environ['QQ_CHATBOT_EMBEDDING_DIM']='256'
    os.environ['QQ_CHATBOT_LLM_PROVIDER']='mock'
    work=Path(config['work']).resolve(); work.mkdir(parents=True,exist_ok=True)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'conversation':conversation,'status':'running'}
    report_path=work/('daily-report-'+uuid.uuid4().hex+'.json')
    code=1
    try:
        # A separate process-wide coordinator prevents two runs from interleaving
        # private-sync and publication. Each stage also owns its own index lock.
        with _sync_lock(work/'coordinator'):
            if not publish_only:
                report['stage']='sync'
                state=sync(live_db=Path(config['live_db']),key_file=Path(config['key_file']),
                    self_qq=str(config['self_qq']),conversation=conversation,
                    base_json=Path(config.get('base_json',str(work/'no-base'))),
                    work=work/'sync',out=Path(config['private_index']),
                    build_config=Path(config['build_config']),snapshot_mode='offline-copy',
                    integrity_mode='selected')
                if state.get('status')=='no_target':
                    raise ValueError('没有目标记录，拒绝发布空索引')
            report['stage']='publish'
            from tools.publish_conversation import publish
            report['publication']=publish(Path(config['private_index']),Path(config['target_index']),
                                           conversation,Path(config['backup_root']))
            report['status']='ok'; code=0
    except SourceBusy:
        report['status']='source_busy'; code=3
    except Exception as exc:
        report['status']='failed'; report['error_type']=type(exc).__name__
        # No raw exception arguments: subprocess commands/config may contain secrets.
        code=1
    finally:
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        _atomic_write(report_path,json.dumps(report,ensure_ascii=False,indent=2).encode())
    print(json.dumps({'status':report['status'],'report':str(report_path)},ensure_ascii=False))
    return code


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--publish-only',action='store_true',help='重新发布已验证私聊索引，不读取QQ库')
    args=parser.parse_args()
    return run(args.config,publish_only=args.publish_only)

if __name__=='__main__': raise SystemExit(main())
