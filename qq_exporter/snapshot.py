"""Protected Windows offline copies; supplied snapshots remain caller-owned."""
from __future__ import annotations
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

class SnapshotError(RuntimeError): pass
class SourceBusy(SnapshotError): pass


def _fingerprint(path):
    try:
        with Path(path).open('rb') as f:
            before=os.fstat(f.fileno())
            h=hashlib.sha256()
            for data in iter(lambda:f.read(1024*1024),b''): h.update(data)
            after=os.fstat(f.fileno())
        stamp=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if stamp(before)!=stamp(after) or stamp(after)!=stamp(Path(path).stat()):
            raise SnapshotError('文件在校验期间变化')
        return (stamp(after),h.hexdigest())
    except FileNotFoundError:
        return None


def _paths(source):
    return [source,Path(str(source)+'-wal'),Path(str(source)+'-journal')]


def _qq_running():
    # .NET process enumeration sees elevated QQ processes that Toolhelp may omit.
    try:
        shell = shutil.which('pwsh') or str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        result=subprocess.run([shell,'-NoProfile','-NonInteractive','-Command',
            "if (@(Get-Process -Name QQ -ErrorAction SilentlyContinue).Count) { exit 3 }; exit 0"],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise SourceBusy('无法可靠确认QQ已退出') from exc
    if result.returncode not in (0,3): raise SourceBusy('QQ进程检查失败')
    return result.returncode==3


@contextmanager
def _protected_file(path):
    import ctypes
    from ctypes import wintypes as wt
    import msvcrt
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wt.LPCWSTR,wt.DWORD,wt.DWORD,wt.LPVOID,wt.DWORD,wt.DWORD,wt.HANDLE]
    kernel.CreateFileW.restype=wt.HANDLE
    kernel.CloseHandle.argtypes=[wt.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:
        code=ctypes.get_last_error()
        raise SourceBusy(f'无法取得拒写入/删除的只读句柄（Windows错误{code}）')
    try:
        fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd,'rb') as stream:
        yield stream


@dataclass
class Snapshot:
    path: Path
    source_path: Path
    metadata: dict
    _expected: dict

    def verify(self):
        current={p:_fingerprint(p) for p in self._expected}
        if current!=self._expected:
            raise SnapshotError('快照或sidecar在消费期间变化')


@contextmanager
def open_snapshot(source: Path, work: Path, *, mode='offline-copy', required_free_bytes=0):
    requested=Path(source).absolute()
    source=requested.resolve(); work=Path(work).resolve()
    if requested != source:
        raise SnapshotError('请使用源数据库的实际绝对路径，不能使用链接或含..的别名')
    if mode not in ('offline-copy','provided'): raise ValueError('未知snapshot mode')
    if not isinstance(required_free_bytes,int) or required_free_bytes<0: raise ValueError('无效空间预算')
    if not source.is_file(): raise SnapshotError('源主库不存在')
    work.mkdir(parents=True,exist_ok=True)
    if mode=='offline-copy' and os.name!='nt': raise SnapshotError('offline-copy仅支持Windows；请提供稳定副本')
    if mode=='offline-copy' and _qq_running(): raise SourceBusy('QQ仍运行；请正常退出后重试')
    paths=_paths(source)
    if mode=='offline-copy':
        for p in paths[1:]:
            if p.exists() and p.stat().st_size: raise SourceBusy('存在非空WAL/journal；请正常退出QQ完成检查点')
    budget=required_free_bytes+(source.stat().st_size if mode=='offline-copy' else 0)
    if shutil.disk_usage(work).free<budget: raise SnapshotError('快照、明文及恢复暂存空间不足')
    if mode=='provided':
        expected={p:_fingerprint(p) for p in paths}
        metadata={'mode':mode,'source_size':source.stat().st_size,'source_sha256':expected[source][1],
                  'immutable_source_required':True,'wal_ignored':bool(expected[paths[1]] and expected[paths[1]][0][2]),
                  'warnings':['provided要求调用方提供不可变快照；变化检测不能证明在线一致性']}
        snap=Snapshot(source,source,metadata,expected)
        try: yield snap
        except BaseException: raise
        else: snap.verify()
        return
    with tempfile.TemporaryDirectory(prefix='snapshot-',dir=work) as temporary:
        destination=Path(temporary)/'source.db'
        with ExitStack() as stack:
            streams={p:stack.enter_context(_protected_file(p)) for p in paths if p.exists()}
            if source not in streams: raise SnapshotError('复制前源消失')
            before={p:_fingerprint(p) for p in paths}
            for p in paths[1:]:
                if before[p] is not None and before[p][0][2]: raise SourceBusy('保护前sidecar出现未提交内容')
            with destination.open('xb') as out:
                shutil.copyfileobj(streams[source],out,1024*1024)
                out.flush(); os.fsync(out.fileno())
            after={p:_fingerprint(p) for p in paths}
            copied=_fingerprint(destination)
            if before!=after or copied[1]!=before[source][1] or _qq_running():
                raise SourceBusy('复制期间QQ或源文件状态变化')
        # Source locks end here; a later QQ restart cannot invalidate this copy.
        expected={p:_fingerprint(p) for p in _paths(destination)}
        metadata={'mode':mode,'source_size':before[source][0][2],'source_sha256':before[source][1],
                  'immutable_source_required':False,'wal_ignored':False,'protected_copy':True,'warnings':[]}
        snap=Snapshot(destination,source,metadata,expected)
        try: yield snap
        except BaseException: raise
        else: snap.verify()
