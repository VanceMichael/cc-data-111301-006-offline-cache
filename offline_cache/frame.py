"""导出分块的帧格式与流式重放。

一次导出的所有记录都装在编号连续的 chunk 文件里（chunk-000000.frame …），
每个 chunk 由若干帧组成。帧头固定 56 字节，全部小端：

    魔数 4B "TBEF" | 版本 u8=1 | 帧类型 u8 | 保留 u16=0
    负载长度 u32 | 存储序号 u32 | 帧序号 u64 | 负载 SHA-256 32B
    随后是「负载长度」字节的 TBE 负载（记录帧才有，控制帧负载为空）。

帧类型：
    1 RECORD  一条 IndexedDB 记录，负载是 TBE 编码的 {key, value}
    2 END     某个 store 读取结束，负载为空

帧序号在整个导出内全局递增、连续；存储序号对应 manifest 中 stores 的下标，
让独立校验端不必依赖文件名顺序也能定位记录归属。整个 chunk 文件另有 SHA-256，
由接收端落盘时流式计算并回 ACK，避免大块内容进入 Python 常驻内存。
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

MAGIC = b'TBEF'
VERSION = 1
HEADER = struct.Struct('<4sBBHIIQ32s')
# 4s 魔数, B 版本, B 类型, H 保留, I 负载长度, I 存储序号, Q 帧序号, 32s 负载摘要
HEADER_SIZE = HEADER.size

FRAME_RECORD = 1
FRAME_END = 2


class FrameError(ValueError):
    """帧头、魔数、摘要或序号不合法时抛出。"""


@dataclass
class Frame:
    kind: int
    sequence: int
    store_index: int
    payload: bytes
    digest: bytes

    @property
    def digest_hex(self) -> str:
        return self.digest.hex()


def build_frame(kind: int, sequence: int, store_index: int, payload: bytes) -> bytes:
    """供测试与 Python 侧自检构造单帧；浏览器端在 frame.js 中对等实现。"""
    digest = hashlib.sha256(payload).digest()
    head = HEADER.pack(MAGIC, VERSION, kind, 0, len(payload),
                       store_index, sequence, digest)
    return head + payload


def iter_frames(data: bytes):
    """从一整块 chunk 字节中惰性取出帧，校验魔数、版本与负载摘要。"""
    offset = 0
    total = len(data)
    while offset < total:
        if total - offset < HEADER_SIZE:
            raise FrameError(f'帧头不完整，剩余 {total - offset} 字节')
        magic, version, kind, _reserved, length, store_index, sequence, digest = \
            HEADER.unpack_from(data, offset)
        if magic != MAGIC:
            raise FrameError(f'帧魔数错误: {magic!r}')
        if version != VERSION:
            raise FrameError(f'不支持的帧版本: {version}')
        if kind not in (FRAME_RECORD, FRAME_END):
            raise FrameError(f'未知帧类型: {kind}')
        start = offset + HEADER_SIZE
        end = start + length
        if end > total:
            raise FrameError('帧负载超出 chunk 边界')
        payload = data[start:end]
        if hashlib.sha256(payload).digest() != digest:
            raise FrameError(f'帧 {sequence} 负载摘要不一致')
        yield Frame(kind, sequence, store_index, payload, digest)
        offset = end
    if offset != total:
        raise FrameError('chunk 存在无法对齐的尾字节')
