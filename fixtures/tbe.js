/*
 * TBE v1 —— Tagged Binary Export，IndexedDB 结构化值的带标签二进制编码。
 *
 * 设计目标：JSON.stringify 无法保留 Date、ArrayBuffer/TypedArray、Blob、Map、
 * Set、BigInt、undefined 与循环引用；TBE 用类型标签逐一保留它们，二进制数据
 * 按原始字节直传，不做 base64 膨胀。浏览器与 Python 两端使用同一份标签约定，
 * 任一端都能独立重放并核对。
 *
 * 整数使用 zigzag 变长编码；长度为小端 u32；双精度为小端 f64。
 * 该文件是无依赖的 ES2020 脚本，可在普通页面与 Node 中直接加载。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TBE = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const TAG = {
    UNDEF: 0, NULL: 1, FALSE: 2, TRUE: 3,
    INT: 4, FLOAT: 5, STRING: 6, BYTES: 7,
    DATE: 8, REGEXP: 9,
    ARRAY: 10, OBJECT: 11, MAP: 12, SET: 13,
    ARRAYBUFFER: 14, TYPEDARRAY: 15, BLOB: 16,
    BIGINT: 17, DECL: 18, REF: 19, END: 20
  };

  // 与 Python 端 offline_cache/tbe.py 中的 _TYPED_NAMES 一一对应。
  const TYPED_VIEWS = [
    'Int8Array', 'Uint8Array', 'Uint8ClampedArray', 'Int16Array',
    'Uint16Array', 'Int32Array', 'Uint32Array', 'Float32Array',
    'Float64Array', 'BigInt64Array', 'BigUint64Array'
  ];

  const textEncoder = new TextEncoder();
  const textDecoder = new TextDecoder();

  function utf8 (value) { return textEncoder.encode(String(value)); }

  class Encoder {
    constructor () {
      this.chunks = [];
      this.length = 0;
      this.refs = new Map(); // 可引用对象 -> 引用编号
      this.nextRef = 0;
    }

    push (bytes) {
      this.chunks.push(bytes);
      this.length += bytes.byteLength;
    }

    tag (value) { this.push(Uint8Array.of(value)); }

    u32 (value) {
      const out = new Uint8Array(4);
      new DataView(out.buffer).setUint32(0, value, true);
      this.push(out);
    }

    f64 (value) {
      const out = new Uint8Array(8);
      new DataView(out.buffer).setFloat64(0, value, true);
      this.push(out);
    }

    varint (value) {
      // 不能用 >>>：JS 位运算会把数字截断为 32 位，大整数会损坏。
      const out = [];
      while (value > 0x7f) {
        out.push((value & 0x7f) | 0x80);
        value = Math.floor(value / 128);
      }
      out.push(value & 0x7f);
      this.push(Uint8Array.from(out));
    }

    zigzag (value) { // value 为安全整数
      return value < 0 ? -2 * value - 1 : 2 * value;
    }

    string (value) {
      const bytes = utf8(value);
      this.tag(TAG.STRING);
      this.u32(bytes.byteLength);
      this.push(bytes);
    }

    // 对象键在线缆上不带 STRING 标签，直接是 u32 长度 + UTF-8 字节。
    keyString (value) {
      const bytes = utf8(value);
      this.u32(bytes.byteLength);
      this.push(bytes);
    }

    // 全异步：Blob 的字节需要异步读取，递归中的每一步都 await，
    // 保证多个/嵌套 Blob 的字节紧跟各自的头部，顺序不串行错位。
    async encode (value) {
      if (value === undefined) return this.tag(TAG.UNDEF);
      if (value === null) return this.tag(TAG.NULL);
      if (value === true) return this.tag(TAG.TRUE);
      if (value === false) return this.tag(TAG.FALSE);

      const type = typeof value;
      if (type === 'number') {
        if (Number.isSafeInteger(value)) {
          this.tag(TAG.INT);
          this.varint(this.zigzag(value));
        } else {
          this.tag(TAG.FLOAT);
          this.f64(value); // NaN / ±Infinity 由 f64 自然保留
        }
        return;
      }
      if (type === 'string') return this.string(value);
      if (type === 'bigint') {
        this.tag(TAG.BIGINT);
        this.string(value.toString());
        return;
      }

      // 以下为可循环引用的复合/二进制类型。
      const known = this.refs.get(value);
      if (known !== undefined) {
        this.tag(TAG.REF);
        this.u32(known);
        return;
      }

      if (value instanceof Date) {
        this.declare(value);
        this.tag(TAG.DATE); this.f64(value.getTime());
        return;
      }
      if (value instanceof RegExp) {
        this.declare(value);
        this.tag(TAG.REGEXP);
        this.string(value.source);
        this.string(value.flags);
        return;
      }
      if (value instanceof ArrayBuffer) {
        this.declare(value);
        this.tag(TAG.ARRAYBUFFER);
        this.u32(value.byteLength);
        this.push(new Uint8Array(value));
        return;
      }
      if (ArrayBuffer.isView(value) && !(value instanceof DataView)) {
        this.declare(value);
        const viewIndex = TYPED_VIEWS.indexOf(value.constructor.name);
        if (viewIndex < 0) throw new TypeError('不支持的视图类型: ' + value.constructor.name);
        this.tag(TAG.TYPEDARRAY);
        this.u32(viewIndex);
        this.u32(value.byteLength);
        this.push(new Uint8Array(value.buffer, value.byteOffset, value.byteLength));
        return;
      }
      if (typeof Blob !== 'undefined' && value instanceof Blob) {
        this.declare(value);
        this.tag(TAG.BLOB);
        this.string(value.type || '');
        this.u32(value.size);
        this.push(new Uint8Array(await value.arrayBuffer()));
        return;
      }
      if (value instanceof Map) {
        this.declare(value);
        this.tag(TAG.MAP);
        this.u32(value.size);
        for (const [k, v] of value) { await this.encode(k); await this.encode(v); }
        this.tag(TAG.END);
        return;
      }
      if (value instanceof Set) {
        this.declare(value);
        this.tag(TAG.SET);
        this.u32(value.size);
        for (const item of value) await this.encode(item);
        this.tag(TAG.END);
        return;
      }
      if (Array.isArray(value)) {
        this.declare(value);
        this.tag(TAG.ARRAY);
        this.u32(value.length);
        for (const item of value) await this.encode(item);
        this.tag(TAG.END);
        return;
      }

      // 普通对象（按可枚举字符串键输出，undefined 作为值保留）。
      this.declare(value);
      const keys = Object.keys(value);
      this.tag(TAG.OBJECT);
      this.u32(keys.length);
      for (const key of keys) {
        this.keyString(key);
        await this.encode(value[key]);
      }
      this.tag(TAG.END);
    }

    declare (value) {
      const id = this.nextRef++;
      this.refs.set(value, id);
      this.tag(TAG.DECL);
      this.u32(id);
    }

    finish () {
      const out = new Uint8Array(this.length);
      let offset = 0;
      for (const part of this.chunks) {
        out.set(part, offset);
        offset += part.byteLength;
      }
      return out;
    }
  }

  async function encode (value) {
    const encoder = new Encoder();
    await encoder.encode(value);
    return encoder.finish();
  }

  class Decoder {
    constructor (bytes) {
      this.view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      this.bytes = bytes;
      this.offset = 0;
      this.refs = [];
      this._pendingRef = undefined;
    }

    need (size) {
      if (this.offset + size > this.bytes.byteLength) {
        throw new RangeError('TBE 数据在第 ' + this.offset + ' 字节处截断');
      }
    }

    u8 () { this.need(1); const v = this.bytes[this.offset]; this.offset += 1; return v; }

    u32 () {
      this.need(4);
      const v = this.view.getUint32(this.offset, true);
      this.offset += 4;
      return v;
    }

    f64 () {
      this.need(8);
      const v = this.view.getFloat64(this.offset, true);
      this.offset += 8;
      return v;
    }

    varint () {
      let shift = 0;
      let result = 0;
      let byte;
      do {
        byte = this.u8();
        result += (byte & 0x7f) * Math.pow(2, shift);
        shift += 7;
        if (shift > 63) throw new RangeError('varint 过长');
      } while (byte & 0x80);
      return result;
    }

    raw (size) {
      this.need(size);
      const part = this.bytes.subarray(this.offset, this.offset + size);
      this.offset += size;
      return part;
    }
    string () {
      return textDecoder.decode(this.raw(this.u32()));
    }

    // 对应编码器的 keyString：对象键不带 STRING 标签。
    keyString () {
      return textDecoder.decode(this.raw(this.u32()));
    }

    decode () {
      const tag = this.u8();
      switch (tag) {
        case TAG.UNDEF: return undefined;
        case TAG.NULL: return null;
        case TAG.FALSE: return false;
        case TAG.TRUE: return true;
        case TAG.INT: {
          const zig = this.varint();
          const value = Math.floor(zig / 2);
          return (zig & 1) === 1 ? -value - 1 : value;
        }
        case TAG.FLOAT: return this.f64();
        case TAG.STRING: return this.string();
        case TAG.BYTES: return new Uint8Array(this.raw(this.u32()));
        case TAG.BIGINT: return BigInt(this.decode());
        case TAG.REF: {
          const id = this.u32();
          if (!(id in this.refs)) throw new RangeError('引用了尚未声明的对象 #' + id);
          return this.refs[id];
        }
        case TAG.DECL: {
          const id = this.u32();
          if (id !== this.refs.length) {
            throw new RangeError('DECL 编号乱序: 期望 ' + this.refs.length + '，实际 ' + id);
          }
          // 先占位，使解码过程中出现的前向 REF 能命中，再回填构造结果。
          this.refs.push(undefined);
          const previous = this._pendingRef;
          this._pendingRef = id;
          let value;
          try {
            value = this.decodeDeclared();
          } finally {
            this._pendingRef = previous;
          }
          this.refs[id] = value;
          return value;
        }
        default:
          throw new TypeError('未知 TBE 标签: ' + tag);
      }
    }
    decodeDeclared () {
      const tag = this.u8();
      switch (tag) {
        case TAG.DATE: return new Date(this.f64());
        case TAG.REGEXP: {
          const source = this.decode();
          const flags = this.decode();
          return new RegExp(source, flags);
        }
        case TAG.ARRAYBUFFER: {
          const size = this.u32();
          return new Uint8Array(this.raw(size)).buffer;
        }
        case TAG.TYPEDARRAY: {
          const kind = this.u32();
          const size = this.u32();
          const name = TYPED_VIEWS[kind];
          if (!name) throw new TypeError('未知 TypedArray 编号: ' + kind);
          const copy = new Uint8Array(this.raw(size)).slice().buffer;
          return new globalThis[name](copy);
        }
        case TAG.BLOB: {
          const mime = this.decode();
          const size = this.u32();
          const copy = new Uint8Array(this.raw(size)).slice();
          return new Blob([copy], { type: mime });
        }
        case TAG.ARRAY: {
          const count = this.u32();
          const arr = new Array(count);
          this.refs[this._pendingRef] = arr;
          for (let i = 0; i < count; i++) arr[i] = this.decode();
          this.expectEnd();
          return arr;
        }
        case TAG.OBJECT: {
          const count = this.u32();
          const obj = {};
          this.refs[this._pendingRef] = obj;
          for (let i = 0; i < count; i++) obj[this.keyString()] = this.decode();
          this.expectEnd();
          return obj;
        }
        case TAG.MAP: {
          const count = this.u32();
          const map = new Map();
          this.refs[this._pendingRef] = map;
          for (let i = 0; i < count; i++) map.set(this.decode(), this.decode());
          this.expectEnd();
          return map;
        }
        case TAG.SET: {
          const count = this.u32();
          const set = new Set();
          this.refs[this._pendingRef] = set;
          for (let i = 0; i < count; i++) set.add(this.decode());
          this.expectEnd();
          return set;
        }
        default:
          throw new TypeError('复合位置出现非法标签: ' + tag);
      }
    }

    expectEnd () {
      const tag = this.u8();
      if (tag !== TAG.END) throw new TypeError('缺少 END 标签，实际为 ' + tag);
    }
  }

  function decode (bytes) {
    const decoder = new Decoder(bytes);
    const value = decoder.decode();
    if (decoder.offset !== bytes.byteLength) {
      throw new RangeError('存在 ' + (bytes.byteLength - decoder.offset) + ' 个未消费字节');
    }
    return value;
  }

  return { TAG, TYPED_VIEWS, Encoder, Decoder, encode, decode, utf8 };
});
