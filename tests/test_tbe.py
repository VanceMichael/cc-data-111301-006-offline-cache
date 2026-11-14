import datetime
import re
import subprocess
import sys
import unittest
from pathlib import Path
import tempfile
import os

from offline_cache.tbe import (
    TBEEncoder, TBEDecoder, decode, TBEError, BlobValue, ArrayBufferValue,
    TypedArrayValue, RegexpValue, MapValue,
)

ROOT = Path(__file__).resolve().parents[1]


def _node_binary():
    return os.environ.get('NODE_BIN', 'node')


def _node_available():
    try:
        result = subprocess.run([_node_binary(), '--version'],
                                capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


NODE_GOLDEN_SCRIPT = r'''
const fs = require('fs');
const TBE = require(process.argv[2]);
(async () => {
  const cyclic = { name: 'loop' };
  cyclic.self = cyclic;
  const blob = new Blob([new Uint8Array([1, 2, 3, 255])], { type: 'image/png' });
  const value = {
    undef: undefined, n: null, b: true, i: -12345, f: 3.14159,
    big: 123456789012345678901234567890n,
    str: '巡店资料 🔒',
    date: new Date('2026-11-01T08:00:00.000Z'),
    re: /abc[d]*/gim,
    arr: [1, 'x', undefined, new Uint8Array([0, 255]), new Int32Array([1, -7])],
    buf: new Uint8Array([9, 8, 7]).buffer,
    view: new Float64Array([1.5, -2.25]),
    blob, blobAgain: blob,
    map: new Map([['k1', 1], [2, 'two']]),
    set: new Set([1, 1, 'a']),
    nested: { d: new Date(0), arr2: [new Uint8ClampedArray([255, 300])] },
    cyclic,
  };
  fs.writeFileSync(process.argv[3], await TBE.encode(value));
})();
'''


class TBEPythonTests(unittest.TestCase):
    def test_roundtrip_scalars_and_containers(self):
        value = {
            'none': None, 'yes': True, 'no': False, 'int': -987654321,
            'float': 2.5, 'str': '门店甲',
            'list': [1, 'two', None, True],
            'nested': {'a': {'b': [1, 2, 3]}},
        }
        self.assertEqual(decode(TBEEncoder().encode(value)), value)

    def test_special_numbers(self):
        import math
        for raw in (float('nan'), float('inf'), float('-inf')):
            back = decode(TBEEncoder().encode(raw))
            self.assertTrue(math.isnan(back) if math.isnan(raw) else back == raw)

    def test_datetime_regexp_bytes(self):
        moment = datetime.datetime(2026, 11, 1, 8, 0, tzinfo=datetime.timezone.utc)
        value = {'at': moment, 're': RegexpValue('a.*b', 'gim'), 'raw': b'\x00\xff'}
        back = decode(TBEEncoder().encode(value))
        self.assertEqual(back['at'], moment)
        self.assertEqual(back['re'].pattern, 'a.*b')
        self.assertEqual(back['re'].flags, 'gim')
        self.assertEqual(back['raw'], b'\x00\xff')

    def test_typed_arrays_and_buffers(self):
        value = {
            'u8': TypedArrayValue('Uint8Array', bytes([0, 255])),
            'i32': TypedArrayValue('Int32Array', (1).to_bytes(4, 'little', signed=True) +
                                   (-7).to_bytes(4, 'little', signed=True)),
            'buf': ArrayBufferValue(b'raw-bytes'),
        }
        back = decode(TBEEncoder().encode(value))
        self.assertEqual(back['u8'].kind, 'Uint8Array')
        self.assertEqual(back['u8'].data, bytes([0, 255]))
        self.assertEqual(back['i32'].kind, 'Int32Array')
        self.assertEqual(back['buf'].data, b'raw-bytes')

    def test_blob_preserves_mime_and_bytes(self):
        blob = BlobValue('image/png', bytes(range(256)))
        back = decode(TBEEncoder().encode({'photo': blob}))
        self.assertEqual(back['photo'].mime, 'image/png')
        self.assertEqual(back['photo'].data, bytes(range(256)))

    def test_map_and_set(self):
        value = {'m': MapValue([('k', 1), (2, 'two')]), 's': {1, 'a'}}
        back = decode(TBEEncoder().encode(value))
        self.assertEqual(back['m'].items, [('k', 1), (2, 'two')])
        self.assertEqual(back['s'], {1, 'a'})

    def test_cyclic_reference_is_shared(self):
        value = {'name': 'loop'}
        value['self'] = value
        back = decode(TBEEncoder().encode(value))
        self.assertIs(back['self'], back)
        self.assertEqual(back['name'], 'loop')

    def test_shared_blob_is_decoded_once(self):
        blob = BlobValue('application/octet-stream', b'abcd')
        value = {'a': blob, 'b': blob}
        back = decode(TBEEncoder().encode(value))
        self.assertIs(back['a'], back['b'])

    def test_truncated_data_is_rejected(self):
        good = TBEEncoder().encode({'a': 1})
        with self.assertRaises(TBEError):
            decode(good[:-2])
        with self.assertRaises(TBEError):
            decode(good + b'\x00')

    def test_unknown_tag_is_rejected(self):
        # 标签 21 尚未分配。
        with self.assertRaises(TBEError):
            decode(bytes([21]))

    def test_forward_ref_without_declaration_fails(self):
        from offline_cache.tbe import TAG_REF
        with self.assertRaises(TBEError):
            decode(bytes([TAG_REF, 1, 0, 0, 0]))


@unittest.skipUnless(_node_available(), '需要 Node.js 做跨语言互测')
class TBECrossLanguageTests(unittest.TestCase):
    def test_javascript_golden_vector_decodes_in_python(self):
        tbe_js = ROOT / 'fixtures' / 'tbe.js'
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'gen.js'
            golden = Path(directory) / 'golden.tbe'
            script.write_text(NODE_GOLDEN_SCRIPT, encoding='utf-8')
            subprocess.run([_node_binary(), str(script), str(tbe_js), str(golden)],
                           check=True, capture_output=True, timeout=30,
                           cwd=str(ROOT))
            value = decode(golden.read_bytes())
        self.assertEqual(value['str'], '巡店资料 🔒')
        self.assertEqual(value['i'], -12345)
        self.assertEqual(value['big'], 123456789012345678901234567890)
        self.assertEqual(value['date'],
                         datetime.datetime(2026, 11, 1, 8, 0,
                                           tzinfo=datetime.timezone.utc))
        self.assertEqual(value['re'].pattern, 'abc[d]*')
        self.assertEqual(value['blob'].mime, 'image/png')
        self.assertEqual(value['blob'].data.hex(), '010203ff')
        self.assertIs(value['blob'], value['blobAgain'])
        self.assertIs(value['cyclic']['self'], value['cyclic'])
        self.assertEqual(value['map'].items, [('k1', 1), (2, 'two')])
        self.assertEqual(value['set'], {1, 'a'})
        self.assertEqual(value['arr'][3].data, bytes([0, 255]))

    def test_python_vector_decodes_in_javascript(self):
        moment = datetime.datetime(2026, 11, 1, 8, 0, tzinfo=datetime.timezone.utc)
        value = {
            'visit': ['门店甲', 1],
            'at': moment,
            'flags': TypedArrayValue('Uint8Array', bytes([0, 1, 255])),
            'photo': BlobValue('application/octet-stream', bytes([0, 255, 16, 32])),
            'meta': MapValue([('k', 1), (2, 'two')]),
        }
        decoder_script = r'''
const fs = require('fs');
const TBE = require(process.argv[2]);
const v = TBE.decode(fs.readFileSync(process.argv[3]));
if (v.visit[0] !== '门店甲' || v.visit[1] !== 1) throw new Error('visit');
if (v.at.toISOString() !== '2026-11-01T08:00:00.000Z') throw new Error('date');
if (!(v.flags instanceof Uint8Array) || v.flags[2] !== 255) throw new Error('flags');
if (!(v.photo instanceof Blob) || v.photo.size !== 4) throw new Error('photo');
if (!(v.meta instanceof Map) || v.meta.get(2) !== 'two') throw new Error('meta');
console.log('ok');
'''
        with tempfile.TemporaryDirectory() as directory:
            vector = Path(directory) / 'py.tbe'
            script = Path(directory) / 'dec.js'
            vector.write_bytes(TBEEncoder().encode(value))
            script.write_text(decoder_script, encoding='utf-8')
            result = subprocess.run(
                [_node_binary(), str(script),
                 str(ROOT / 'fixtures' / 'tbe.js'), str(vector)],
                check=True, capture_output=True, text=True, timeout=30, cwd=str(ROOT))
            self.assertIn('ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
