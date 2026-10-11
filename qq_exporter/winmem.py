"""Windows 进程枚举与内存读取。

本模块只使用 Win32 公开 API：

* ``CreateToolhelp32Snapshot``  枚举进程 / 模块
* ``OpenProcess``               以只读权限打开目标进程
* ``VirtualQueryEx``            枚举可读内存区域
* ``ReadProcessMemory``         读取内存

不注入 DLL、不 Hook 任何函数、不修改目标进程内存。等价于一个只读的调试器行为。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from ctypes import byref, create_string_buffer, sizeof
from dataclasses import dataclass
from typing import Iterator

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- 常量 -----------------------------------------------------------------

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

MAX_PATH = 260
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 内存保护标志 -> 是否可读
_READABLE_PROTECT = {
    0x02,  # PAGE_READONLY
    0x04,  # PAGE_READWRITE
    0x08,  # PAGE_WRITECOPY
    0x20,  # PAGE_EXECUTE_READ
    0x40,  # PAGE_EXECUTE_READWRITE
    0x80,  # PAGE_EXECUTE_WRITECOPY
}


# ---- 结构体 ---------------------------------------------------------------


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * MAX_PATH),
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
        ("szExePath", wt.WCHAR * MAX_PATH),
    ]


# ---- 函数原型（显式声明，避免 64 位句柄被截断） ---------------------------

kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE

kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL

kernel32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wt.BOOL
kernel32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
kernel32.Module32NextW.restype = wt.BOOL

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE

kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL

kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t

kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wt.BOOL

kernel32.IsWow64Process.argtypes = [wt.HANDLE, ctypes.POINTER(wt.BOOL)]
kernel32.IsWow64Process.restype = wt.BOOL


# ---- 进程枚举 -------------------------------------------------------------


def list_processes(exe_name: str) -> list[int]:
    """返回指定可执行文件名（不区分大小写）的所有 PID。"""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return []
    pids: list[int] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, byref(entry)):
            return []
        target = exe_name.lower()
        while True:
            if entry.szExeFile.lower() == target:
                pids.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(snapshot, byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def process_has_module(pid: int, module_name: str) -> bool:
    """判断某进程是否加载了指定模块（NTQQ 的主进程加载 ``wrapper.node``）。"""
    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return False
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(snapshot, byref(entry)):
            return False
        target = module_name.lower()
        while True:
            if entry.szModule.lower() == target:
                return True
            if not kernel32.Module32NextW(snapshot, byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return False


def is_wow64(handle: int) -> bool:
    flag = wt.BOOL(0)
    if kernel32.IsWow64Process(handle, byref(flag)):
        return bool(flag.value)
    return False


# ---- 内存读取 -------------------------------------------------------------


@dataclass
class Region:
    base: int
    size: int
    protect: int
    mtype: int = 0


# 内存类型
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000


class ProcessMemory:
    """以只读方式访问某个进程的虚拟内存。"""

    def __init__(self, pid: int):
        self.pid = pid
        self.handle = kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
        )
        if not self.handle:
            err = ctypes.get_last_error()
            raise OSError(
                err,
                f"OpenProcess(PID={pid}) 失败。"
                "若错误码为 5（拒绝访问），请以管理员身份运行终端。",
            )
        self.is_32bit = is_wow64(self.handle)

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "ProcessMemory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 区域枚举 ----------------------------------------------------------

    def iter_regions(self, max_region: int = 1024 * 1024 * 1024) -> Iterator[Region]:
        """枚举所有已提交且可读的内存区域。"""
        mbi = MEMORY_BASIC_INFORMATION()
        address = 0
        limit = 0x7FFFFFFF if self.is_32bit else 0x7FFFFFFFFFFF

        while address < limit:
            written = kernel32.VirtualQueryEx(
                self.handle, ctypes.c_void_p(address), byref(mbi), sizeof(mbi)
            )
            if not written:
                break
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize)
            if (
                mbi.State == MEM_COMMIT
                and (mbi.Protect & 0xFF) in _READABLE_PROTECT
                and not (mbi.Protect & PAGE_GUARD)
                and size > 0
                and size <= max_region
            ):
                yield Region(base, size, mbi.Protect, mbi.Type)
            nxt = base + size
            if nxt <= address:
                break
            address = nxt

    def iter_private_regions(self, max_region: int = 1024 * 1024 * 1024) -> Iterator[Region]:
        """只枚举堆（MEM_PRIVATE）区域，跳过 DLL 与文件映射，速度快很多。

        SQLCipher 的编解码上下文是 malloc 出来的，只会落在私有堆里。
        """
        for region in self.iter_regions(max_region=max_region):
            if region.mtype == MEM_PRIVATE:
                yield region

    # -- 读取 --------------------------------------------------------------

    def read(self, address: int, size: int) -> bytes | None:
        buf = create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(address),
            buf,
            size,
            byref(read),
        )
        if not ok or read.value == 0:
            return None
        return buf.raw[: read.value]

    def iter_chunks(
        self,
        chunk_size: int = 8 * 1024 * 1024,
        overlap: int = 512,
        private_only: bool = False,
    ) -> Iterator[tuple[int, bytes]]:
        """按块遍历所有可读内存，产出 ``(起始地址, 数据)``。

        相邻块之间保留 ``overlap`` 字节重叠，避免目标串跨块边界时漏检。
        ``private_only`` 为真时只扫私有堆（跳过 DLL 与文件映射）。
        """
        regions = self.iter_private_regions() if private_only else self.iter_regions()
        for region in regions:
            offset = 0
            while offset < region.size:
                want = min(chunk_size + overlap, region.size - offset)
                data = self.read(region.base + offset, want)
                if data:
                    yield region.base + offset, data
                step = chunk_size
                if offset + step >= region.size:
                    break
                offset += step
