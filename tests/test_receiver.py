import hashlib
import http.client
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from offline_cache.frame import build_frame, FRAME_RECORD, FRAME_END
from offline_cache.receiver import serve, shutdown, ReceiverError
from offline_cache.tbe import TBEEncoder, BlobValue
from offline_cache.inspect_export import verify_export, list_exports, extract_blobs


def _request(address, token, path, method='GET', body=None, bad_token=False):
    headers = {}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + ('wrong' if bad_token else token)
    if body is not None:
        headers['Content-Type'] = 'application/octet-stream'
    request = urllib.request.Request(
        address + path, data=body, method=method, headers=headers)
    return urllib.request.urlopen(request, timeout=5)


def _raw_request_with_host(address, host, token, path):
    """直接用 http.client 伪造 Host 头，验证回环主机限制。"""
    parsed = urllib.request.urlparse(address)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request('GET', path, headers={'Authorization': 'Bearer ' + token,
                                             'Host': host})
    return connection.getresponse()


def _json(response):
    return json.loads(response.read().decode('utf-8'))


class ExportBuilder:
    """按导出协议拼出帧号、块号连续的请求体，模拟浏览器发送顺序。"""

    def __init__(self, stores):
        self.stores = stores
        self.frame = 0
        self.chunk = 0
        self.records = {name: 0 for name in stores}
        self.bodies = {}  # chunk 序号 -> 字节

    def record(self, store_name, key, value):
        index = self.stores.index(store_name)
        payload = TBEEncoder().encode({'key': key, 'value': value})
        frame = build_frame(FRAME_RECORD, self.frame, index, payload)
        self.bodies.setdefault(self.chunk, []).append(frame)
        self.frame += 1
        self.records[store_name] += 1
        return self

    def end(self, store_name):
        index = self.stores.index(store_name)
        frame = build_frame(FRAME_END, self.frame, index, b'')
        self.bodies.setdefault(self.chunk, []).append(frame)
        self.frame += 1
        return self

    def new_chunk(self):
        # 把当前缓冲帧封成一块并推进块号。
        self.chunk += 1
        return self

    def chunk_bytes(self, chunk_sequence):
        return b''.join(self.bodies[chunk_sequence])

    def chunk_sequences(self):
        return sorted(self.bodies)


class ReceiverFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server, self.state, self.address, self.token = serve(
            self.root / 'exports', token='test-token-1234567890')

    def close(self):
        shutdown(self.server)
        self.temp.cleanup()

    def begin(self, builder, export_id='export-20261114080000-abcdef012345',
              version=1):
        schema = [
            {'name': name, 'keyPath': ['store', 'visit'] if name == 'visits' else None,
             'autoIncrement': False, 'indexes': []}
            for name in builder.stores
        ]
        meta = {'exportId': export_id,
                'database': {'name': '巡店资料', 'version': version},
                'origin': 'http://127.0.0.1:9999', 'stores': schema,
                'chunkBytes': 1024}
        _json(_request(self.address, self.token, '/begin', 'POST',
                       json.dumps(meta).encode()))
        return export_id

    def upload(self, export_id, builder):
        for sequence in builder.chunk_sequences():
            _request(self.address, self.token,
                     f'/chunk/{export_id}/{sequence}', 'POST',
                     builder.chunk_bytes(sequence))

    def finalize(self, export_id, builder):
        return _json(_request(
            self.address, self.token, f'/finalize/{export_id}', 'POST',
            json.dumps({'frameCount': builder.frame,
                        'chunkCount': len(builder.bodies),
                        'records': builder.records}).encode()))


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.fx = ReceiverFixture()

    def tearDown(self):
        self.fx.close()

    def test_ping_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            _request(self.fx.address, None, '/ping')
        self.assertEqual(failure.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            _request(self.fx.address, self.fx.token, '/ping', bad_token=True)
        self.assertEqual(failure.exception.code, 401)
        self.assertTrue(_json(_request(self.fx.address, self.fx.token, '/ping'))['ok'])

    def test_begin_chunk_finalize_happy_path(self):
        builder = ExportBuilder(['visits', 'photos'])
        builder.record('visits', ['门店甲', 1], {'photo': 'photo-a'})
        builder.end('visits').new_chunk()
        builder.record('photos', 'photo-a', {'mime': 'image/png', 'size': 4})
        builder.end('photos')
        export_id = self.fx.begin(builder)
        self.fx.upload(export_id, builder)
        final = self.fx.finalize(export_id, builder)
        self.assertEqual(final['state'], 'complete')

        manifest = json.loads((self.fx.root / 'exports' / export_id /
                               'manifest.json').read_text())
        reports = {item['name']: item for item in manifest['storeReports']}
        self.assertEqual(reports['visits']['records'], 1)
        self.assertEqual(reports['photos']['records'], 1)
        self.assertTrue(all(item['complete'] for item in manifest['storeReports']))
        self.assertEqual(len(reports['visits']['keyDigest']), 64)
        self.assertEqual(manifest['frameCount'], 4)
        self.assertEqual(manifest['chunkCount'], 2)

    def test_duplicate_chunk_is_idempotent_but_conflict_rejected(self):
        builder = ExportBuilder(['visits'])
        builder.record('visits', ['甲', 1], {'x': 1})
        export_id = self.fx.begin(builder)
        body = builder.chunk_bytes(0)
        _request(self.fx.address, self.fx.token,
                 f'/chunk/{export_id}/0', 'POST', body)
        again = _json(_request(self.fx.address, self.fx.token,
                               f'/chunk/{export_id}/0', 'POST', body))
        self.assertTrue(again['duplicate'])

        other = ExportBuilder(['visits'])
        other.record('visits', ['乙', 2], {'x': 2})
        with self.assertRaises(urllib.error.HTTPError) as failure:
            _request(self.fx.address, self.fx.token,
                     f'/chunk/{export_id}/0', 'POST', other.chunk_bytes(0))
        self.assertEqual(failure.exception.code, 409)

    def test_second_begin_with_same_id_rejected(self):
        builder = ExportBuilder(['visits'])
        export_id = self.fx.begin(builder)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.fx.begin(builder, export_id)
        self.assertEqual(failure.exception.code, 409)

    def test_missing_end_frame_cannot_finalize(self):
        builder = ExportBuilder(['visits', 'photos'])
        builder.record('visits', ['甲', 1], {})  # 故意不发任何 END
        export_id = self.fx.begin(builder)
        self.fx.upload(export_id, builder)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.fx.finalize(export_id, builder)
        self.assertEqual(failure.exception.code, 409)
        status = json.loads((self.fx.root / 'exports' / export_id /
                             'status.json').read_text())
        self.assertEqual(status['state'], 'failed')
        self.assertFalse((self.fx.root / 'exports' / export_id /
                          'manifest.json').exists())

    def test_explicit_fail_marks_partial_not_complete(self):
        builder = ExportBuilder(['visits'])
        builder.record('visits', ['甲', 1], {})
        export_id = self.fx.begin(builder)
        self.fx.upload(export_id, builder)
        result = _json(_request(
            self.fx.address, self.fx.token, f'/fail/{export_id}', 'POST',
            json.dumps({'error': '事务被中止'}).encode()))
        self.assertEqual(result['state'], 'failed')
        with self.assertRaises(urllib.error.HTTPError):
            self.fx.finalize(export_id, builder)
        verdict = verify_export(self.fx.root / 'exports' / export_id)
        self.assertEqual(verdict['effectiveState'], 'needs_reexport')

    def test_frame_sequence_gap_is_rejected_at_finalize(self):
        export_id = self.fx.begin(ExportBuilder(['visits', 'photos']))
        # 帧号直接从 5 起跳，制造序号缺口。
        bad = build_frame(FRAME_RECORD, 5, 0,
                          TBEEncoder().encode({'key': ['甲', 1], 'value': {}}))
        _request(self.fx.address, self.fx.token,
                 f'/chunk/{export_id}/0', 'POST', bad)
        _request(self.fx.address, self.fx.token,
                 f'/chunk/{export_id}/1', 'POST',
                 build_frame(FRAME_END, 6, 0, b''))
        with self.assertRaises(urllib.error.HTTPError):
            _request(self.fx.address, self.fx.token,
                     f'/finalize/{export_id}', 'POST', b'{}')

    def test_tampered_chunk_detected_by_frame_digest(self):
        export_id = self.fx.begin(ExportBuilder(['visits', 'photos']))
        good = build_frame(FRAME_RECORD, 0, 0,
                           TBEEncoder().encode({'key': ['甲', 1], 'value': {}}))
        damaged = bytearray(good)
        damaged[-1] ^= 0xFF
        _request(self.fx.address, self.fx.token,
                 f'/chunk/{export_id}/0', 'POST', bytes(damaged))
        _request(self.fx.address, self.fx.token,
                 f'/chunk/{export_id}/1', 'POST',
                 build_frame(FRAME_END, 1, 0, b''))
        with self.assertRaises(urllib.error.HTTPError):
            _request(self.fx.address, self.fx.token,
                     f'/finalize/{export_id}', 'POST', b'{}')

    def test_blob_digests_and_cross_store_refs_in_manifest(self):
        builder = ExportBuilder(['visits', 'photos'])
        builder.record('visits', ['门店甲', 1], {'photo': 'photo-a'})
        builder.end('visits').new_chunk()
        builder.record('photos', 'photo-a',
                       BlobValue('image/png', b'\x00\xff\x10\x20'))
        builder.end('photos')
        export_id = self.fx.begin(builder)
        self.fx.upload(export_id, builder)
        self.fx.finalize(export_id, builder)

        manifest = json.loads((self.fx.root / 'exports' / export_id /
                               'manifest.json').read_text())
        reports = {item['name']: item for item in manifest['storeReports']}
        self.assertEqual(len(reports['photos']['binary']), 1)
        self.assertEqual(reports['photos']['binary'][0]['sha256'],
                         hashlib.sha256(b'\x00\xff\x10\x20').hexdigest())
        self.assertTrue(any(ref['target'] == 'photo-a'
                            for ref in reports['visits']['resolvedRefs']))

    def test_verify_and_list_and_extract_after_complete(self):
        builder = ExportBuilder(['visits', 'photos'])
        builder.record('visits', ['甲', 1], {'photo': 'photo-a'})
        builder.end('visits').new_chunk()
        builder.record('photos', 'photo-a', BlobValue('image/png', b'PNG-DATA'))
        builder.end('photos')
        export_id = self.fx.begin(builder)
        self.fx.upload(export_id, builder)
        self.fx.finalize(export_id, builder)

        export_dir = self.fx.root / 'exports' / export_id
        verdict = verify_export(export_dir)
        self.assertEqual(verdict['effectiveState'], 'complete')
        self.assertTrue(all(s['state'] == 'complete' for s in verdict['stores']))

        listed = list_exports(self.fx.root / 'exports')
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['state'], 'complete')

        with tempfile.TemporaryDirectory() as out:
            extracted = extract_blobs(export_dir, Path(out))
            self.assertEqual(len(extracted), 1)
            self.assertEqual(extracted[0]['sha256'],
                             hashlib.sha256(b'PNG-DATA').hexdigest())
            self.assertTrue((Path(out) / extracted[0]['file']).exists())

    def test_duplicate_key_in_same_store_rejected(self):
        export_id = self.fx.begin(ExportBuilder(['visits', 'photos']))
        first = build_frame(FRAME_RECORD, 0, 0,
                            TBEEncoder().encode({'key': ['甲', 1], 'value': {}}))
        duplicate = build_frame(FRAME_RECORD, 1, 0,
                                TBEEncoder().encode({'key': ['甲', 1], 'value': {'x': 1}}))
        end = build_frame(FRAME_END, 2, 0, b'')
        body = b''.join([first, duplicate, end])
        _request(self.fx.address, self.fx.token,
                 f'/chunk/{export_id}/0', 'POST', body)
        with self.assertRaises(urllib.error.HTTPError):
            _request(self.fx.address, self.fx.token,
                     f'/finalize/{export_id}', 'POST', b'{}')

    def test_export_id_path_traversal_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            _request(self.fx.address, self.fx.token,
                     '/chunk/..%2fevil/0', 'POST', b'x')
        self.assertIn(failure.exception.code, (400, 404, 409))
        self.assertFalse((self.fx.root / 'evil').exists())

    def test_verify_detects_tampered_manifest_by_replaying_bytes(self):
        builder = ExportBuilder(['visits', 'photos'])
        builder.record('visits', ['甲', 1], {'photo': 'photo-a'})
        builder.end('visits').new_chunk()
        builder.record('photos', 'photo-a', BlobValue('image/png', b'X'))
        builder.end('photos')
        export_id = self.fx.begin(builder)
        self.fx.upload(export_id, builder)
        self.fx.finalize(export_id, builder)
        export_dir = self.fx.root / 'exports' / export_id

        # 手工篡改 manifest 里的键摘要，模拟不可信清单。
        manifest_path = export_dir / 'manifest.json'
        saved = json.loads(manifest_path.read_text())
        saved['storeReports'][0]['keyDigest'] = '00' * 32
        manifest_path.write_text(json.dumps(saved), encoding='utf-8')

        verdict = verify_export(export_dir)
        # 字节重放本身完整，但 manifest 与重放不符必须被点名。
        self.assertTrue(any('manifest 与字节重放结果不一致' in problem
                            for problem in verdict['problems']))
        self.assertEqual(verdict['effectiveState'], 'needs_reexport')

    def test_non_loopback_host_rejected(self):
        response = _raw_request_with_host(
            self.fx.address, 'example.com', self.fx.token, '/ping')
        self.assertEqual(response.status, 403)
        response.read()
        allowed = _raw_request_with_host(
            self.fx.address, '127.0.0.1', self.fx.token, '/ping')
        self.assertEqual(allowed.status, 200)
        allowed.read()


if __name__ == '__main__':
    unittest.main()
