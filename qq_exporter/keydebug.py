"""通过调试器从 ``nt_sqlite3_key_v2`` 的调用参数中取出数据库口令。

这是 QQBackup / qq-win-db-key 项目验证过的官方思路，这里用纯 ctypes 复刻：

1. 静态分析 ``wrapper.node``，得到 ``nt_sqlite3_key_v2`` 所在函数的 RVA
   （见 :mod:`qq_exporter.peinfo`）；
2. 以 ``DEBUG_ONLY_THIS_PROCESS`` 启动 ``QQ.exe``；
3. 等 ``wrapper.node`` 加载后，在 ``模块基址 + 函数RVA`` 处写入 ``int3`` 断点；
4. 断点命中时，第三个参数（x64 下是 ``R8``）就是口令指针，读出来即可。

注意
----
* 必须以调试方式**新启动**一个 QQ 进程，所以请先退出正在运行的 QQ；
* 提取完毕后会结束这个被调试的 QQ 进程，你需要重新登录一次；
* 口令可缓存，之后无需再走这条路。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import time
from ctypes import byref, sizeof
from pathlib import Path

from . import peinfo

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DEBUG_ONLY_THIS_PROCESS = 0x00000002
DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001

EXCEPTION_DEBUG_EVENT = 1
CREATE_THREAD_DEBUG_EVENT = 2
CREATE_PROCESS_DEBUG_EVENT = 3
EXIT_THREAD_DEBUG_EVENT = 4
EXIT_PROCESS_DEBUG_EVENT = 5
LOAD_DLL_DEBUG_EVENT = 6
UNLOAD_DLL_DEBUG_EVENT = 7
OUTPUT_DEBUG_STRING_EVENT = 8

EXCEPTION_BREAKPOINT = 0x80000003
EXCEPTION_SINGLE_STEP = 0x80000004

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
THREAD_ALL_ACCESS = 0x1FFFFF

CONTEXT_AMD64 = 0x00100000
CONTEXT_ALL = CONTEXT_AMD64 | 0x1F
CONTEXT_SIZE = 1232

# x64 CONTEXT 里我们关心的字段偏移
OFF_CONTEXT_FLAGS = 0x30
OFF_EFLAGS = 0x44
OFF_R8 = 0xB8
OFF_RIP = 0xF8

TRAP_FLAG = 0x100


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [
        ("dwDebugEventCode", wt.DWORD),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
        ("_pad", wt.DWORD),
        ("u", ctypes.c_ubyte * 160),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", wt.WCHAR * 256),
        ("szExePath", wt.WCHAR * 260),
    ]


kernel32.CreateProcessW.argtypes = [
    wt.LPCWSTR,
    wt.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.BOOL,
    wt.DWORD,
    ctypes.c_void_p,
    wt.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wt.BOOL
kernel32.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT), wt.DWORD]
kernel32.WaitForDebugEvent.restype = wt.BOOL
kernel32.ContinueDebugEvent.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD]
kernel32.ContinueDebugEvent.restype = wt.BOOL
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.WriteProcessMemory.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.FlushInstructionCache.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
kernel32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenThread.restype = wt.HANDLE
kernel32.GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.SetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.IsWow64Process.argtypes = [wt.HANDLE, ctypes.POINTER(wt.BOOL)]


class DebuggerError(RuntimeError):
    pass


def _context_buffer():
    """CONTEXT 要求 16 字节对齐，手工对齐一块缓冲区。"""
    raw = ctypes.create_string_buffer(CONTEXT_SIZE + 16)
    addr = ctypes.addressof(raw)
    aligned = (addr + 15) & ~15
    return raw, aligned


def _read_u32(base: int, off: int) -> int:
    return struct.unpack("<I", ctypes.string_at(base + off, 4))[0]


def _write_u32(base: int, off: int, value: int) -> None:
    ctypes.memmove(base + off, struct.pack("<I", value), 4)


def _read_u64(base: int, off: int) -> int:
    return struct.unpack("<Q", ctypes.string_at(base + off, 8))[0]


def module_base(pid: int, name: str) -> int:
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return 0
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(snapshot, byref(entry)):
            return 0
        target = name.lower()
        while True:
            if entry.szModule.lower() == target:
                return ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
            if not kernel32.Module32NextW(snapshot, byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return 0


def find_qq_exe(install_dir: Path | None = None) -> tuple[Path, Path]:
    """返回 ``(QQ.exe, wrapper.node)``。"""
    wrapper = peinfo.find_wrapper_node(install_dir)
    # wrapper.node 位于 <安装目录>/versions/<版本>/resources/app/
    install = wrapper.parents[3]
    exe = install / "QQ.exe"
    if not exe.is_file():
        raise FileNotFoundError(f"未找到 {exe}")
    return exe, wrapper


class KeyDebugger:
    def __init__(self, qq_exe: Path, function_rva: int, log=print):
        self.qq_exe = Path(qq_exe)
        self.function_rva = function_rva
        self.log = log
        self.process = PROCESS_INFORMATION()
        self.process_handle = None
        self.breakpoint_address = 0
        self.original_byte = None
        self.passphrase: str | None = None

    # -- 断点 ---------------------------------------------------------------

    def _set_breakpoint(self) -> bool:
        addr = self.breakpoint_address
        buf = ctypes.create_string_buffer(1)
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.process_handle, ctypes.c_void_p(addr), buf, 1, byref(read)
        )
        if not ok or read.value != 1:
            self.log("  读取原始字节失败")
            return False
        self.original_byte = buf.raw[0]
        patch = ctypes.create_string_buffer(b"\xcc", 1)
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            self.process_handle, ctypes.c_void_p(addr), patch, 1, byref(written)
        )
        kernel32.FlushInstructionCache(self.process_handle, ctypes.c_void_p(addr), 1)
        return bool(ok and written.value == 1)

    def _restore_byte(self) -> None:
        if self.original_byte is None:
            return
        patch = ctypes.create_string_buffer(bytes([self.original_byte]), 1)
        written = ctypes.c_size_t(0)
        kernel32.WriteProcessMemory(
            self.process_handle,
            ctypes.c_void_p(self.breakpoint_address),
            patch,
            1,
            byref(written),
        )
        kernel32.FlushInstructionCache(
            self.process_handle, ctypes.c_void_p(self.breakpoint_address), 1
        )

    # -- 断点处理 -----------------------------------------------------------

    def _handle_breakpoint(self, thread_id: int) -> str | None:
        handle = kernel32.OpenThread(THREAD_ALL_ACCESS, False, thread_id)
        if not handle:
            return None
        raw, ctx = _context_buffer()
        try:
            _write_u32(ctx, OFF_CONTEXT_FLAGS, CONTEXT_ALL)
            if not kernel32.GetThreadContext(handle, ctypes.c_void_p(ctx)):
                return None
            r8 = _read_u64(ctx, OFF_R8)
            rip = _read_u64(ctx, OFF_RIP)
            # int3 之后 RIP 已 +1，回退到原指令地址
            ctypes.memmove(ctx + OFF_RIP, struct.pack("<Q", rip - 1), 8)
            kernel32.SetThreadContext(handle, ctypes.c_void_p(ctx))

            buf = ctypes.create_string_buffer(256)
            read = ctypes.c_size_t(0)
            if not kernel32.ReadProcessMemory(
                self.process_handle, ctypes.c_void_p(r8), buf, 256, byref(read)
            ):
                return None
            raw_bytes = buf.raw
            end = raw_bytes.find(b"\x00")
            if end < 0:
                end = read.value
            candidate = raw_bytes[:end]
            if len(candidate) == 16 and all(32 <= c < 127 for c in candidate):
                return candidate.decode("ascii")
            return None
        finally:
            kernel32.CloseHandle(handle)

    def _single_step(self, thread_id: int) -> None:
        """让线程单步执行一次原指令，再装回断点。"""
        handle = kernel32.OpenThread(THREAD_ALL_ACCESS, False, thread_id)
        if not handle:
            return
        raw, ctx = _context_buffer()
        try:
            _write_u32(ctx, OFF_CONTEXT_FLAGS, CONTEXT_ALL)
            if kernel32.GetThreadContext(handle, ctypes.c_void_p(ctx)):
                flags = _read_u32(ctx, OFF_EFLAGS) | TRAP_FLAG
                ctypes.memmove(ctx + OFF_EFLAGS, struct.pack("<I", flags), 4)
                kernel32.SetThreadContext(handle, ctypes.c_void_p(ctx))
        finally:
            kernel32.CloseHandle(handle)

    def _clear_trap(self, thread_id: int) -> None:
        handle = kernel32.OpenThread(THREAD_ALL_ACCESS, False, thread_id)
        if not handle:
            return
        raw, ctx = _context_buffer()
        try:
            _write_u32(ctx, OFF_CONTEXT_FLAGS, CONTEXT_ALL)
            if kernel32.GetThreadContext(handle, ctypes.c_void_p(ctx)):
                flags = _read_u32(ctx, OFF_EFLAGS) & ~TRAP_FLAG
                ctypes.memmove(ctx + OFF_EFLAGS, struct.pack("<I", flags), 4)
                kernel32.SetThreadContext(handle, ctypes.c_void_p(ctx))
        finally:
            kernel32.CloseHandle(handle)

    # -- 主流程 -------------------------------------------------------------

    def run(self, timeout: float = 600.0) -> str | None:
        info = peinfo.analyse(peinfo.find_wrapper_node(self.qq_exe.parents[0]))
        self.function_rva = info.function_rva

        startup = STARTUPINFOW()
        startup.cb = sizeof(STARTUPINFOW)
        cmdline = ctypes.create_unicode_buffer(f'"{self.qq_exe}"')
        created = kernel32.CreateProcessW(
            str(self.qq_exe),
            cmdline,
            None,
            None,
            False,
            DEBUG_ONLY_THIS_PROCESS,
            None,
            str(self.qq_exe.parent),
            byref(startup),
            byref(self.process),
        )
        if not created:
            raise DebuggerError(
                f"以调试方式启动 QQ 失败（错误码 {ctypes.get_last_error()}）。"
                "若 QQ 已在运行，请先完全退出。"
            )
        self.process_handle = self.process.hProcess
        self.log(f"已启动 QQ（PID={self.process.dwProcessId}），请在窗口中登录…")

        deadline = time.time() + timeout
        event = DEBUG_EVENT()
        stepping: set[int] = set()
        try:
            while time.time() < deadline:
                if not kernel32.WaitForDebugEvent(byref(event), 5000):
                    continue
                code = event.dwDebugEventCode
                pid, tid = event.dwProcessId, event.dwThreadId
                status = DBG_CONTINUE

                if code == LOAD_DLL_DEBUG_EVENT:
                    hfile = struct.unpack_from("<Q", bytes(event.u), 0)[0]
                    if hfile:
                        kernel32.CloseHandle(wt.HANDLE(hfile))
                    if not self.breakpoint_address:
                        base = module_base(pid, "wrapper.node")
                        if base:
                            self.breakpoint_address = base + info.function_rva
                            self.log(
                                f"wrapper.node @0x{base:X}，断点 @0x{self.breakpoint_address:X}"
                            )
                            if not self._set_breakpoint():
                                self.log("设置断点失败")
                elif code == EXCEPTION_DEBUG_EVENT:
                    exc_code = struct.unpack_from("<I", bytes(event.u), 0)[0]
                    exc_addr = struct.unpack_from("<Q", bytes(event.u), 16)[0]
                    if exc_code == EXCEPTION_BREAKPOINT:
                        if self.breakpoint_address and exc_addr == self.breakpoint_address:
                            self._restore_byte()
                            found = self._handle_breakpoint(tid)
                            if found:
                                self.passphrase = found
                                self.log(f"找到口令: {found}")
                                kernel32.TerminateProcess(self.process_handle, 0)
                            else:
                                stepping.add(tid)
                        # 系统初始断点等一律放行
                    elif exc_code == EXCEPTION_SINGLE_STEP:
                        if tid in stepping:
                            self._clear_trap(tid)
                            self._set_breakpoint()
                            stepping.discard(tid)
                    else:
                        status = DBG_EXCEPTION_NOT_HANDLED
                elif code == CREATE_PROCESS_DEBUG_EVENT:
                    hfile = struct.unpack_from("<Q", bytes(event.u), 0)[0]
                    if hfile:
                        kernel32.CloseHandle(wt.HANDLE(hfile))
                elif code == EXIT_PROCESS_DEBUG_EVENT:
                    break

                kernel32.ContinueDebugEvent(pid, tid, status)
                if self.passphrase:
                    break
        finally:
            try:
                kernel32.TerminateProcess(self.process_handle, 0)
            except Exception:  # noqa: BLE001
                pass
            kernel32.CloseHandle(self.process)
            if self.process.hThread:
                kernel32.CloseHandle(self.process.hThread)

        return self.passphrase


def extract_passphrase(
    *, wrapper: Path | None = None, log=print, timeout: float = 600.0
) -> str | None:
    """以调试方式启动 QQ 并取出数据库口令。"""
    if wrapper:
        exe = Path(wrapper).parents[3] / "QQ.exe"
        info = peinfo.analyse(Path(wrapper))
    else:
        exe, wrapper_path = find_qq_exe()
        info = peinfo.analyse(wrapper_path)
    dbg = KeyDebugger(exe, info.function_rva, log=log)
    return dbg.run(timeout=timeout)
