"""TBE v1 的 Python 端实现。

与 fixtures/tbe.js 共享同一套标签约定（见 docs/export.md）。本模块刻意不依赖
浏览器：接收端用 :class:`TBEEncoder` 生成跨语言测试向量，用 :class:`TBEDecoder`
独立重放浏览器发来的记录，核对类型与二进制摘要，而不是信任页面自述。

支持的类型覆盖 IndexedDB 结构化克隆的常见取值：None/bool/int/float/str/bytes、
Date（datetime）、正则、ArrayBuffer 与 11 种 TypedArray、Blob、Map、Set、
BigInt、循环引用（DECL/REF）。普通 dict 的键按字符串处理，与 JS 对象一致。
"""
from __future__ import annotations

import datetime
import re
import struct
from dataclasses import dataclass


TAG_UNDEF = 0
TAG_NULL = 1
TAG_FALSE = 2
TAG_TRUE = 3
TAG_INT = 4
TAG_FLOAT = 5
TAG_STRING = 6
TAG_BYTES = 7
TAG_DATE = 8
TAG_REGEXP = 9
TAG_ARRAY = 10
TAG_OBJECT = 11
TAG_MAP = 12
TAG_SET = 13
TAG_ARRAYBUFFER = 14
TAG_TYPEDARRAY = 15
TAG_BLOB = 16
TAG_BIGINT = 17
TAG_DECL = 18
TAG_REF = 19
TAG_END = 20

# 下标即线缆上的视图编号，必须与 tbe.js 的 TYPED_VIEWS 完全一致。
_TYPED_NAMES = (
    'Int8Array', 'Uint8Array', 'Uint8ClampedArray', 'Int16Array',
    'Uint16Array', 'Int32Array', 'Uint32Array', 'Float32Array',
    'Float64Array', 'BigInt64Array', 'BigUint64Array',
)
_TYPED_FORMATS = {
    'Int8Array': ('b', 1), 'Uint8Array': ('B', 1),
    'Uint8ClampedArray': ('B', 1), 'Int16Array': ('h', 2),
    'Uint16Array': ('H', 2), 'Int32Array': ('i', 4),
    'Uint32Array': ('I', 4), 'Float32Array': ('f', 4),
    'Float64Array': ('d', 8), 'BigInt64Array': ('q', 8),
    'BigUint64Array': ('Q', 8),
}


class TBEError(ValueError):
    """线缆数据无法按 TBE v1 解析时抛出。"""


@dataclass
class BlobValue:
    """浏览器 Blob 的镜像：MIME 类型加原始字节。"""
    mime: str
    data: bytes


@dataclass
class ArrayBufferValue:
    """ArrayBuffer 与裸 bytes 在线缆上是不同标签，分别建模。"""
    data: bytes


@dataclass
class TypedArrayValue:
    kind: str
    data: bytes  # 原始小端字节，按 kind 的元素宽度解释


@dataclass
class RegexpValue:
    pattern: str
    flags: str

    def compile(self) -> re.Pattern:
        mapping = {'d': re.DOTALL, 'g': 0, 'i': re.IGNORECASE,
                   'm': re.MULTILINE, 's': re.DOTALL, 'u': re.UNICODE, 'y': 0}
        flags = 0
        for char in self.flags:
            flags |= mapping.get(char, 0)
        return re.compile(self.pattern, flags)


class MapValue:
    """JS Map 的镜像：保留插入顺序，键可以不是字符串。"""

    def __init__(self, items=()):
        self.items = list(items)


def _zigzag_encode(value: int) -> int:
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def _zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _regex_flags_to_js(flags: int) -> str:
    # Python 的 re 标志与 JS 字符标志做尽力映射，仅供测试向量使用。
    letters = []
    if flags & re.IGNORECASE:
        letters.append('i')
    if flags & re.MULTILINE:
        letters.append('m')
    if flags & re.DOTALL:
        letters.append('s')
    if flags & re.UNICODE:
        letters.append('u')
    return ''.join(letters)


class TBEEncoder:
    """生成 TBE 字节，供跨语言测试向量与 Python 侧自检使用。"""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._refs: dict[int, int] = {}
        self._next_ref = 0

    def _write(self, data: bytes) -> None:
        self._chunks.append(data)

    def _tag(self, tag: int) -> None:
        self._write(bytes((tag,)))

    def _u32(self, value: int) -> None:
        self._write(struct.pack('<I', value))

    def _f64(self, value: float) -> None:
        self._write(struct.pack('<d', value))

    def _varint(self, value: int) -> None:
        out = bytearray()
        while value > 0x7F:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value & 0x7F)
        self._write(bytes(out))

    def _string(self, value: str) -> None:
        raw = value.encode('utf-8')
        self._tag(TAG_STRING)
        self._u32(len(raw))
        self._write(raw)

    def _key_string(self, value: str) -> None:
        # 对象键不带 STRING 标签，直接是 u32 长度 + UTF-8 字节。
        raw = str(value).encode('utf-8')
        self._u32(len(raw))
        self._write(raw)

    def _declare(self, identity: int) -> None:
        ref = self._next_ref
        self._next_ref += 1
        self._refs[identity] = ref
        self._tag(TAG_DECL)
        self._u32(ref)

    def encode(self, value) -> bytes:
        self._encode(value, id(value) if self._compound(value) else None)
        return b''.join(self._chunks)

    @staticmethod
    def _compound(value) -> bool:
        return isinstance(value, (list, dict, set, frozenset, tuple,
                                  datetime.datetime, re.Pattern, RegexpValue,
                                  BlobValue, ArrayBufferValue, TypedArrayValue,
                                  MapValue))

    def _encode(self, value, identity) -> None:
        if value is None:
            return self._tag(TAG_NULL)
        if value is True:
            return self._tag(TAG_TRUE)
        if value is False:
            return self._tag(TAG_FALSE)
        if isinstance(value, bool):  # pragma: no cover - 已被上面覆盖
            return self._tag(TAG_TRUE if value else TAG_FALSE)
        if isinstance(value, int) and not isinstance(value, bool):
            if -(1 << 63) <= value < (1 << 63):
                self._tag(TAG_INT)
                return self._varint(_zigzag_encode(value))
            self._tag(TAG_BIGINT)
            return self._string(str(value))
        if isinstance(value, float):
            self._tag(TAG_FLOAT)
            return self._f64(value)
        if isinstance(value, str):
            return self._string(value)
        if isinstance(value, bytes):
            self._tag(TAG_BYTES)
            self._u32(len(value))
            return self._write(value)
        if identity is not None and identity in self._refs:
            self._tag(TAG_REF)
            return self._u32(self._refs[identity])
        if isinstance(value, datetime.datetime):
            self._declare(identity)
            self._tag(TAG_DATE)
            return self._f64(value.timestamp() * 1000.0)
        if isinstance(value, re.Pattern):
            value = RegexpValue(value.pattern, _regex_flags_to_js(value.flags))
            identity = id(value)
        if isinstance(value, RegexpValue):
            self._declare(identity)
            self._tag(TAG_REGEXP)
            self._string(value.pattern)
            return self._string(value.flags)
        if isinstance(value, ArrayBufferValue):
            self._declare(identity)
            self._tag(TAG_ARRAYBUFFER)
            self._u32(len(value.data))
            return self._write(value.data)
        if isinstance(value, TypedArrayValue):
            if value.kind not in _TYPED_FORMATS:
                raise TBEError(f'未知 TypedArray 类型: {value.kind}')
            self._declare(identity)
            self._tag(TAG_TYPEDARRAY)
            self._u32(_TYPED_NAMES.index(value.kind))
            self._u32(len(value.data))
            return self._write(value.data)
        if isinstance(value, BlobValue):
            self._declare(identity)
            self._tag(TAG_BLOB)
            self._string(value.mime)
            self._u32(len(value.data))
            return self._write(value.data)
        if isinstance(value, dict):
            self._declare(identity)
            self._tag(TAG_OBJECT)
            self._u32(len(value))
            for key, item in value.items():
                self._key_string(key)
                self._encode(item, id(item) if self._compound(item) else None)
            return self._tag(TAG_END)
        if isinstance(value, (list, tuple)):
            self._declare(identity)
            self._tag(TAG_ARRAY)
            self._u32(len(value))
            for item in value:
                self._encode(item, id(item) if self._compound(item) else None)
            return self._tag(TAG_END)
        if isinstance(value, (set, frozenset)):
            self._declare(identity)
            self._tag(TAG_SET)
            self._u32(len(value))
            for item in value:
                self._encode(item, id(item) if self._compound(item) else None)
            return self._tag(TAG_END)
        if isinstance(value, MapValue):
            self._declare(identity)
            self._tag(TAG_MAP)
            self._u32(len(value.items))
            for key, item in value.items:
                self._encode(key, id(key) if self._compound(key) else None)
                self._encode(item, id(item) if self._compound(item) else None)
            return self._tag(TAG_END)
        raise TBEError(f'Python 类型无法编码为 TBE: {type(value).__name__}')


class TBEDecoder:
    """流式重放 TBE 字节。构造后调用 :meth:`decode` 取根值。"""

    def __init__(self, data: bytes):
        self._data = data
        self._view = memoryview(data)
        self._offset = 0
        self._refs: list = []
        self._pending_ref = None

    def _need(self, size: int) -> None:
        if self._offset + size > len(self._data):
            raise TBEError(f'TBE 数据在第 {self._offset} 字节处截断')

    def _u8(self) -> int:
        self._need(1)
        value = self._data[self._offset]
        self._offset += 1
        return value

    def _u32(self) -> int:
        self._need(4)
        (value,) = struct.unpack_from('<I', self._data, self._offset)
        self._offset += 4
        return value

    def _f64(self) -> float:
        self._need(8)
        (value,) = struct.unpack_from('<d', self._data, self._offset)
        self._offset += 8
        return value

    def _varint(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self._u8()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise TBEError('varint 过长')

    def _raw(self, size: int) -> bytes:
        self._need(size)
        value = bytes(self._view[self._offset:self._offset + size])
        self._offset += size
        return value

    def _string(self) -> str:
        return self._raw(self._u32()).decode('utf-8')

    def _key_string(self) -> str:
        # 对应编码器的 _key_string：对象键不带 STRING 标签。
        return self._raw(self._u32()).decode('utf-8')

    def _tagged_string(self) -> str:
        # 线缆上的独立字符串值带 STRING 标签；裸字符串（对象键）不带。
        if self._u8() != TAG_STRING:
            raise TBEError('期望 STRING 标签')
        return self._string()

    def decode(self):
        tag = self._u8()
        if tag == TAG_UNDEF:
            return None
        if tag == TAG_NULL:
            return None
        if tag == TAG_FALSE:
            return False
        if tag == TAG_TRUE:
            return True
        if tag == TAG_INT:
            return _zigzag_decode(self._varint())
        if tag == TAG_FLOAT:
            return self._f64()
        if tag == TAG_STRING:
            return self._string()
        if tag == TAG_BYTES:
            return self._raw(self._u32())
        if tag == TAG_BIGINT:
            return int(self._tagged_string())
        if tag == TAG_REF:
            ref = self._u32()
            if ref >= len(self._refs):
                raise TBEError(f'引用了尚未声明的对象 #{ref}')
            return self._refs[ref]
        if tag == TAG_DECL:
            ref = self._u32()
            if ref != len(self._refs):
                raise TBEError(f'DECL 编号乱序: 期望 {len(self._refs)}，实际 {ref}')
            # 先占位再回填，支持解码过程中的前向/自引用。
            self._refs.append(None)
            previous = self._pending_ref
            self._pending_ref = ref
            try:
                value = self._decode_declared()
            finally:
                self._pending_ref = previous
            self._refs[ref] = value
            return value
        raise TBEError(f'未知 TBE 标签: {tag}')

    def _decode_declared(self):
        tag = self._u8()
        if tag == TAG_DATE:
            return datetime.datetime.fromtimestamp(self._f64() / 1000.0,
                                                   tz=datetime.timezone.utc)
        if tag == TAG_REGEXP:
            return RegexpValue(self._tagged_string(), self._tagged_string())
        if tag == TAG_ARRAYBUFFER:
            return ArrayBufferValue(self._raw(self._u32()))
        if tag == TAG_TYPEDARRAY:
            kind = _TYPED_NAMES[self._u32()]
            return TypedArrayValue(kind, self._raw(self._u32()))
        if tag == TAG_BLOB:
            return BlobValue(self._tagged_string(), self._raw(self._u32()))
        if tag == TAG_ARRAY:
            count = self._u32()
            value = [None] * count
            self._refs[self._pending_ref] = value
            for index in range(count):
                value[index] = self.decode()
            self._expect_end()
            return value
        if tag == TAG_OBJECT:
            count = self._u32()
            value = {}
            self._refs[self._pending_ref] = value
            for _ in range(count):
                key = self._key_string()
                value[key] = self.decode()
            self._expect_end()
            return value
        if tag == TAG_MAP:
            count = self._u32()
            value = MapValue([])
            self._refs[self._pending_ref] = value
            for _ in range(count):
                value.items.append((self.decode(), self.decode()))
            self._expect_end()
            return value
        if tag == TAG_SET:
            count = self._u32()
            value = set()
            self._refs[self._pending_ref] = value
            for _ in range(count):
                value.add(self.decode())
            self._expect_end()
            return value
        raise TBEError(f'复合位置出现非法标签: {tag}')

    def _expect_end(self) -> None:
        tag = self._u8()
        if tag != TAG_END:
            raise TBEError(f'缺少 END 标签，实际为 {tag}')

    def finish(self):
        if self._offset != len(self._data):
            raise TBEError(f'存在 {len(self._data) - self._offset} 个未消费字节')


def decode(data: bytes):
    """解码单条 TBE 记录并做首尾完整性检查。"""
    decoder = TBEDecoder(data)
    value = decoder.decode()
    decoder.finish()
    return value
