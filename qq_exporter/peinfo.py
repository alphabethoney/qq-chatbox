"""wrapper.node 静态分析：定位 ``nt_sqlite3_key_v2`` 所在的函数。

NTQQ 的数据库密钥是 16 字节 ASCII 口令，它在调用 ``sqlite3_key_v2`` 时作为
第 3 个参数传入。要动态取出它，需要先知道这个函数在模块里的 RVA。

这里做的是纯文件解析，不启动、不附加、不修改任何进程：

1. 解析 PE32+ 头与节表；
2. 在 ``.rdata`` 里定位调试字符串 ``nt_sqlite3_key_v2: db=%p zDb=%s``；
3. 在 ``.text`` 里找到引用该字符串的 RIP 相对 LEA 指令；
4. 借助异常目录（``.pdata``）二分查出该指令所属函数的起始 RVA。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

TARGET_STRING = b"nt_sqlite3_key_v2: db=%p zDb=%s"

# RIP 相对寻址的 LEA：REX.W + 8D + ModRM(/r=5) + disp32
_REX_W = 0x48
_LEA = 0x8D
_MODRM_MASK = 0xC7
_MODRM_RIP = 0x05


class PEError(ValueError):
    """PE 文件结构不符合预期。"""


@dataclass
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_pointer: int
    raw_size: int

    @property
    def raw_end(self) -> int:
        return self.raw_pointer + self.raw_size

    def contains_rva(self, rva: int) -> bool:
        return self.virtual_address <= rva < self.virtual_address + max(
            self.virtual_size, self.raw_size
        )


@dataclass
class KeyFunctionInfo:
    path: Path
    image_base: int
    string_rva: int
    lea_rva: int
    function_rva: int


def parse_sections(blob: bytes) -> tuple[int, list[Section]]:
    if len(blob) < 0x40 or blob[:2] != b"MZ":
        raise PEError("不是有效的 PE 文件（缺少 MZ 头）")
    e_lfanew = struct.unpack_from("<I", blob, 0x3C)[0]
    if blob[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        raise PEError("不是有效的 PE 文件（缺少 PE 签名）")

    coff = e_lfanew + 4
    number_of_sections = struct.unpack_from("<H", blob, coff + 2)[0]
    size_of_optional = struct.unpack_from("<H", blob, coff + 16)[0]
    optional = coff + 20
    magic = struct.unpack_from("<H", blob, optional)[0]
    if magic != 0x20B:
        raise PEError(f"仅支持 64 位 PE（magic=0x{magic:X}）")

    image_base = struct.unpack_from("<Q", blob, optional + 24)[0]

    sections: list[Section] = []
    base = optional + size_of_optional
    for i in range(number_of_sections):
        off = base + i * 40
        name = blob[off : off + 8].rstrip(b"\x00").decode("ascii", "replace")
        virtual_size = struct.unpack_from("<I", blob, off + 8)[0]
        virtual_address = struct.unpack_from("<I", blob, off + 12)[0]
        raw_size = struct.unpack_from("<I", blob, off + 16)[0]
        raw_pointer = struct.unpack_from("<I", blob, off + 20)[0]
        sections.append(
            Section(name, virtual_address, virtual_size, raw_pointer, raw_size)
        )
    return image_base, sections


def find_target_string(blob: bytes, sections: list[Section]) -> tuple[Section, int]:
    """在 .rdata 中定位目标字符串，返回 (节, RVA)。"""
    rdata = next((s for s in sections if s.name == ".rdata"), None)
    if rdata is None:
        raise PEError(".rdata 节不存在")
    idx = blob.find(TARGET_STRING, rdata.raw_pointer, rdata.raw_end)
    if idx < 0:
        raise PEError(f"未在 .rdata 中找到字符串 {TARGET_STRING!r}")
    return rdata, rdata.virtual_address + (idx - rdata.raw_pointer)


def find_lea(blob: bytes, sections: list[Section], string_rva: int) -> int:
    """在 .text 中找到引用目标字符串的 LEA 指令，返回指令 RVA。"""
    text = next((s for s in sections if s.name == ".text"), None)
    if text is None:
        raise PEError(".text 节不存在")

    start = text.raw_pointer
    end = min(text.raw_end, len(blob))
    data = blob
    for i in range(start + 1, end - 6):
        if data[i] != _LEA:
            continue
        if (data[i - 1] & 0xF8) != _REX_W:
            continue
        if (data[i + 1] & _MODRM_MASK) != _MODRM_RIP:
            continue
        disp = struct.unpack_from("<i", data, i + 2)[0]
        instr_rva = text.virtual_address + (i - 1 - text.raw_pointer)
        if instr_rva + 7 + disp == string_rva:
            return instr_rva
    raise PEError("未在 .text 中找到引用该字符串的 LEA 指令")


def find_function(blob: bytes, sections: list[Section], lea_rva: int) -> int:
    """借助异常目录（.pdata / RUNTIME_FUNCTION）二分查找包含该 RVA 的函数起始 RVA。"""
    e_lfanew = struct.unpack_from("<I", blob, 0x3C)[0]
    optional = e_lfanew + 4 + 20
    size_of_optional = struct.unpack_from("<H", blob, e_lfanew + 4 + 16)[0]
    data_dirs = optional + 112
    exception_rva, exception_size = struct.unpack_from("<II", blob, data_dirs + 3 * 8)
    if exception_rva == 0 or exception_size == 0:
        raise PEError("异常目录为空，无法定位函数边界")

    section = next((s for s in sections if s.contains_rva(exception_rva)), None)
    if section is None:
        raise PEError("异常目录不在任何已知节内")

    file_off = section.raw_pointer + (exception_rva - section.virtual_address)
    count = exception_size // 12

    left, right = 0, count - 1
    while left <= right:
        mid = (left + right) // 2
        begin, end = struct.unpack_from("<II", blob, file_off + mid * 12)
        if lea_rva < begin:
            right = mid - 1
        elif lea_rva >= end:
            left = mid + 1
        else:
            return begin
    raise PEError("异常目录中找不到包含该指令的函数")


def analyse(wrapper_path: Path) -> KeyFunctionInfo:
    wrapper_path = Path(wrapper_path)
    blob = wrapper_path.read_bytes()
    image_base, sections = parse_sections(blob)
    _rdata, string_rva = find_target_string(blob, sections)
    lea_rva = find_lea(blob, sections, string_rva)
    function_rva = find_function(blob, sections, lea_rva)
    return KeyFunctionInfo(wrapper_path, image_base, string_rva, lea_rva, function_rva)


def find_wrapper_node(install_dir: Path | None = None) -> Path:
    """自动定位已安装 QQ 的 wrapper.node。"""
    candidates: list[Path] = []
    if install_dir:
        candidates.extend(Path(install_dir).glob("versions/*/resources/app/wrapper.node"))
    else:
        import os

        roots = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tencent",
            Path("C:/Program Files/Tencent"),
            Path("C:/Program Files (x86)/Tencent"),
            Path("D:/QQ"),
        ]
        for root in roots:
            if root.is_dir():
                candidates.extend(root.glob("versions/*/resources/app/wrapper.node"))
                candidates.extend(root.glob("*/versions/*/resources/app/wrapper.node"))
    if not candidates:
        raise FileNotFoundError(
            "未找到 wrapper.node，请用 --wrapper 手动指定路径"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)
