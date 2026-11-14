/*
 * frame.js —— 与 offline_cache/frame.py 对等的分块帧工具。
 *
 * 帧头 56 字节小端布局：
 *   魔数 "TBEF" | 版本 u8=1 | 帧类型 u8 | 保留 u16=0
 *   负载长度 u32 | 存储序号 u32 | 帧序号 u64 | 负载 SHA-256 32B
 *
 * 帧序号在一次导出内全局连续递增；存储序号是本次导出 stores 清单的下标。
 * 全部使用浏览器原生 SubtleCrypto 与 DataView，不引入第三方库。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TBEFrame = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MAGIC = 0x46454254; // "TBEF" 的小端 u32 读法
  const MAGIC_BYTES = new Uint8Array([0x54, 0x42, 0x45, 0x46]);
  const VERSION = 1;
  const HEADER_SIZE = 56;
  const FRAME_RECORD = 1;
  const FRAME_END = 2;

  async function sha256 (bytes) {
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return new Uint8Array(digest);
  }

  // 帧序号可能超过 2^32，用 BigUint64 写小端。
  function writeHeader (view, kind, payloadLength, storeIndex, sequence, digest) {
    view.setUint32(0, MAGIC, true);
    view.setUint8(4, VERSION);
    view.setUint8(5, kind);
    view.setUint16(6, 0, true);
    view.setUint32(8, payloadLength, true);
    view.setUint32(12, storeIndex, true);
    view.setBigUint64(16, BigInt(sequence), true);
    for (let i = 0; i < 32; i++) view.setUint8(24 + i, digest[i]);
  }

  async function buildFrame (kind, sequence, storeIndex, payload) {
    const digest = await sha256(payload);
    const buffer = new ArrayBuffer(HEADER_SIZE + payload.byteLength);
    const view = new DataView(buffer);
    writeHeader(view, kind, payload.byteLength, storeIndex, sequence, digest);
    new Uint8Array(buffer, HEADER_SIZE).set(payload);
    return new Uint8Array(buffer);
  }

  function buildRecordFrame (sequence, storeIndex, payload) {
    return buildFrame(FRAME_RECORD, sequence, storeIndex, payload);
  }

  function buildEndFrame (sequence, storeIndex) {
    return buildFrame(FRAME_END, sequence, storeIndex, new Uint8Array(0));
  }

  // 校验一整块 chunk 的每个帧头与负载摘要，返回帧描述，供页面侧自检。
  async function parseChunk (bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const frames = [];
    let offset = 0;
    while (offset < bytes.byteLength) {
      if (bytes.byteLength - offset < HEADER_SIZE) {
        throw new RangeError('帧头不完整');
      }
      if (view.getUint32(offset, true) !== MAGIC) throw new TypeError('帧魔数错误');
      if (view.getUint8(offset + 4) !== VERSION) throw new TypeError('帧版本不支持');
      const kind = view.getUint8(offset + 5);
      const length = view.getUint32(offset + 8, true);
      const storeIndex = view.getUint32(offset + 12, true);
      const sequence = Number(view.getBigUint64(offset + 16, true));
      const digest = bytes.subarray(offset + 24, offset + 56);
      const payload = bytes.subarray(offset + HEADER_SIZE, offset + HEADER_SIZE + length);
      const actual = await sha256(payload);
      for (let i = 0; i < 32; i++) {
        if (digest[i] !== actual[i]) throw new Error('帧 ' + sequence + ' 负载摘要不一致');
      }
      frames.push({ kind, sequence, storeIndex, length, payload });
      offset += HEADER_SIZE + length;
    }
    return frames;
  }

  return {
    MAGIC_BYTES, VERSION, HEADER_SIZE, FRAME_RECORD, FRAME_END,
    sha256, buildRecordFrame, buildEndFrame, parseChunk
  };
});
