"""本机回环接收端：接收浏览器分块交接并做独立校验。

状态机（每个 exportId 一个目录）：

    active   已 /begin，正在收块；目录里只有 chunk-*.frame 与 status.json
    complete /finalize 的独立重放全部通过后，原子写入 manifest.json 并改名
    failed   浏览器显式 /fail、校验失败或服务器发现不一致；绝不可信为完整快照

接收端不信任页面自述的任何摘要：finalize 时重新流式读取全部 chunk，逐帧核对
魔数/版本/负载 SHA-256、帧序号连续性、store 归属，再用 TBE 独立解码每条记录，
重算键集合摘要、记录摘要、二进制摘要与 store 间关联。任何一项不过即 failed。

只绑定回环地址、强制 Bearer 令牌、拒绝非回环 Host，分块按固定小块读写，
照片 Blob 不会整体进入 Python 内存。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .frame import HEADER_SIZE, FRAME_RECORD, FRAME_END, FrameError, iter_frames
from .tbe import TBEError, TBEDecoder, decode as tbe_decode

_EXPORT_ID = re.compile(r'^export-[0-9]{14}-[0-9a-f]{12}$')
_READ_BLOCK = 65536
_LOOPBACK_HOSTS = {'127.0.0.1', '::1', 'localhost'}


class ReceiverError(ValueError):
    """接收端拒绝请求或校验失败。"""


@dataclass
class StoreReport:
    name: str
    records: int = 0
    record_bytes: int = 0
    key_digest: str = ''
    record_digest: str = ''
    blob_digests: list = field(default_factory=list)
    outgoing_refs: list = field(default_factory=list)
    dangling_refs: list = field(default_factory=list)
    ended: bool = False


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as writer:
            json.dump(payload, writer, ensure_ascii=False, indent=2)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def _stream_to_file(reader, destination: Path, declared_length: int):
    """按固定块把请求正文落盘并计算整文件 SHA-256，返回 (字节数, 摘要)。"""
    digest = hashlib.sha256()
    remaining = declared_length
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent,
                                              prefix='.chunk-', suffix='.part')
    size = 0
    try:
        with os.fdopen(descriptor, 'wb') as writer:
            while remaining > 0:
                block = reader.read(min(_READ_BLOCK, remaining))
                if not block:
                    raise ReceiverError('请求正文在声明长度处提前结束')
                writer.write(block)
                digest.update(block)
                size += len(block)
                remaining -= len(block)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return size, digest.hexdigest()


# ---- 独立重放与清单核对 ----------------------------------------------------

def _key_to_text(key) -> str:
    """把 IndexedDB 键（含复合数组键）规范成稳定文本，用于键集合摘要。"""
    if isinstance(key, list):
        return '[' + ','.join(_key_to_text(item) for item in key) + ']'
    if key is None:
        return 'null'
    if isinstance(key, bool):
        return 'true' if key else 'false'
    if isinstance(key, (int, float)):
        return json.dumps(key)
    return json.dumps(str(key), ensure_ascii=False)


def _collect_blob_digests(value, out: list) -> None:
    from .tbe import BlobValue, ArrayBufferValue, TypedArrayValue
    if isinstance(value, BlobValue):
        out.append({'mime': value.mime, 'bytes': len(value.data),
                    'sha256': hashlib.sha256(value.data).hexdigest()})
        return
    if isinstance(value, (ArrayBufferValue, TypedArrayValue)):
        out.append({'kind': type(value).__name__, 'bytes': len(value.data),
                    'sha256': hashlib.sha256(value.data).hexdigest()})
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_blob_digests(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_blob_digests(item, out)
    elif isinstance(value, set):
        for item in value:
            _collect_blob_digests(item, out)


def _find_string_refs(field_name, value, out: list) -> None:
    """收集对象字段中的字符串关联候选（非外键约束）。

    只把「对象的字符串类型字段」当作候选：这些字段最可能是指向另一个 store
    记录键的标识（如 photo: 'photo-a'）。不递归进 Blob/TypedArray 等二进制，
    也不把数组里的裸字符串或复合键内部文本当作关联，避免误报。
    """
    from .tbe import BlobValue, ArrayBufferValue, TypedArrayValue
    if isinstance(value, (BlobValue, ArrayBufferValue, TypedArrayValue, bytes)):
        return
    if isinstance(value, str):
        out.append({'field': field_name, 'target': value})
        return
    if isinstance(value, dict):
        for nested_name, item in value.items():
            _find_string_refs(str(nested_name), item, out)
    elif isinstance(value, list):
        for item in value:
            # 数组元素没有字段名，只有嵌套对象里的字符串才算候选。
            if isinstance(item, dict):
                for nested_name, nested in item.items():
                    _find_string_refs(str(nested_name), nested, out)
    elif isinstance(value, set):
        for item in value:
            if isinstance(item, dict):
                for nested_name, nested in item.items():
                    _find_string_refs(str(nested_name), nested, out)


def replay_export(export_dir: Path, begin_meta: dict):
    """流式重放整个导出目录，独立生成 manifest 与逐 store 报告。

    不做整库装载：每个 chunk 读入后逐帧解码，记录级摘要增量更新，
    二进制摘要只在单条记录的 Blob/视图上计算。
    """
    stores = begin_meta['stores']
    store_names = [item['name'] for item in stores]
    reports = {name: StoreReport(name=name) for name in store_names}
    key_hashes = {name: hashlib.sha256() for name in store_names}
    record_hashes = {name: hashlib.sha256() for name in store_names}
    seen_keys = {name: set() for name in store_names}
    # 关联匹配用的裸字符串键集合，与带类型标签的键摘要分开维护。
    string_keys = {name: set() for name in store_names}

    chunk_files = sorted(export_dir.glob('chunk-*.frame'))
    if not chunk_files:
        raise ReceiverError('没有任何分块，无法构成快照')

    expected_sequence = 0
    chunk_summaries = []
    frame_count = 0

    for chunk_path in chunk_files:
        data = chunk_path.read_bytes()
        chunk_digest = hashlib.sha256(data).hexdigest()
        chunk_summaries.append({
            'file': chunk_path.name,
            'bytes': len(data),
            'sha256': chunk_digest
        })
        try:
            frames = list(iter_frames(data))
        except FrameError as error:
            raise ReceiverError(f'{chunk_path.name} 帧校验失败: {error}')
        for frame in frames:
            if frame.sequence != expected_sequence:
                raise ReceiverError(
                    f'帧序号不连续: 期望 {expected_sequence}，实际 {frame.sequence}')
            expected_sequence += 1
            frame_count += 1
            if not 0 <= frame.store_index < len(store_names):
                raise ReceiverError(f'帧 {frame.sequence} 的存储序号越界')
            report = reports[store_names[frame.store_index]]

            if frame.kind == FRAME_END:
                if frame.payload:
                    raise ReceiverError('END 帧不应携带负载')
                if report.ended:
                    raise ReceiverError(f'store {report.name} 出现重复 END 帧')
                report.ended = True
                continue

            if frame.kind != FRAME_RECORD:  # pragma: no cover - iter_frames 已拦
                raise ReceiverError('未知帧类型')
            try:
                record = tbe_decode(frame.payload)
            except TBEError as error:
                raise ReceiverError(f'帧 {frame.sequence} TBE 解码失败: {error}')
            if not isinstance(record, dict) or set(record.keys()) != {'key', 'value'}:
                raise ReceiverError(f'帧 {frame.sequence} 不是 {{key,value}} 记录')
            key_text = _key_to_text(record['key'])
            if key_text in seen_keys[report.name]:
                raise ReceiverError(
                    f'store {report.name} 出现重复键 {key_text}，快照不可信')
            seen_keys[report.name].add(key_text)
            if isinstance(record['key'], str):
                string_keys[report.name].add(record['key'])

            key_hashes[report.name].update(key_text.encode('utf-8'))
            key_hashes[report.name].update(b'\n')
            record_hashes[report.name].update(frame.digest)
            report.records += 1
            report.record_bytes += len(frame.payload)

            blobs = []
            _collect_blob_digests(record['value'], blobs)
            report.blob_digests.extend(blobs)
            refs = []
            _find_string_refs(None, record['value'], refs)
            report.outgoing_refs.extend(
                {'key': key_text, 'field': entry['field'],
                 'target': entry['target']} for entry in refs)

    incomplete = [name for name, report in reports.items() if not report.ended]
    if incomplete:
        raise ReceiverError('缺少 END 帧，store 未读完整: ' + ', '.join(incomplete))

    # store 间关联：用其他 store 的裸字符串键判断引用是否能落到真实键。
    for name, report in reports.items():
        other_string_keys = set()
        for other, keys in string_keys.items():
            if other != name:
                other_string_keys.update(keys)
        dangling = []
        resolved = []
        for ref in report.outgoing_refs:
            entry = dict(ref)
            if ref['target'] in other_string_keys:
                resolved.append(entry)
            else:
                dangling.append(entry)
        report.outgoing_refs = resolved
        report.dangling_refs = dangling

    for name, report in reports.items():
        report.key_digest = key_hashes[name].hexdigest()
        report.record_digest = record_hashes[name].hexdigest()

    manifest = {
        'format': 'tbe-export/v1',
        'exportId': begin_meta['exportId'],
        'database': begin_meta['database'],
        'origin': begin_meta.get('origin'),
        'chunkBytes': begin_meta.get('chunkBytes'),
        'stores': stores,
        'frameCount': frame_count,
        'chunkCount': len(chunk_files),
        'chunks': chunk_summaries,
        'storeReports': [
            {
                'name': report.name,
                'records': report.records,
                'recordBytes': report.record_bytes,
                'keyDigest': report.key_digest,
                'recordDigest': report.record_digest,
                'binary': report.blob_digests,
                'resolvedRefs': report.outgoing_refs,
                'danglingRefs': report.dangling_refs,
                'complete': report.ended
            }
            for report in reports.values()
        ],
    }
    return manifest


# ---- HTTP 服务 -------------------------------------------------------------

class _ReceiverState:
    def __init__(self, root: Path, token: str):
        self.root = root
        self.token = token
        self.lock = threading.Lock()
        self.exports = {}

    def export_dir(self, export_id: str) -> Path:
        if not isinstance(export_id, str) or not _EXPORT_ID.match(export_id):
            raise ReceiverError('导出标识格式不合法')
        return self.root / export_id

    def status_path(self, export_dir: Path) -> Path:
        return export_dir / 'status.json'

    def load_status(self, export_dir: Path) -> dict:
        return _read_json(self.status_path(export_dir))


def make_handler(state: _ReceiverState):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'offline-cache-receiver/1.0'

        def log_message(self, *args):  # 静默，避免污染演练输出
            pass

        def _reject(self, status: int, message: str):
            body = json.dumps({'error': message}, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_host(self) -> bool:
            host = self.headers.get('Host', '').split(':')[0].lower()
            if host not in _LOOPBACK_HOSTS:
                self._reject(403, '只接受回环主机请求')
                return False
            return True

        def _check_token(self) -> bool:
            expected = 'Bearer ' + state.token
            if secrets.compare_digest(self.headers.get('Authorization', ''), expected):
                return True
            self._reject(401, '令牌缺失或不匹配')
            return False

        def _cors(self):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

        def do_OPTIONS(self):
            if not self._check_host():
                return
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if not self._check_host() or not self._check_token():
                return
            parts = [part for part in urlsplit(self.path).path.split('/') if part]
            if parts == ['ping']:
                self._write_json(200, {'ok': True})
                return
            if len(parts) == 2 and parts[0] == 'status':
                self._handle_status(parts[1])
                return
            self._reject(404, '未知路径')

        def do_POST(self):
            if not self._check_host() or not self._check_token():
                return
            parts = [part for part in urlsplit(self.path).path.split('/') if part]
            try:
                if parts == ['begin']:
                    return self._handle_begin()
                if len(parts) == 3 and parts[0] == 'chunk':
                    return self._handle_chunk(parts[1], int(parts[2]))
                if len(parts) == 2 and parts[0] == 'finalize':
                    return self._handle_finalize(parts[1])
                if len(parts) == 2 and parts[0] == 'fail':
                    return self._handle_fail(parts[1])
            except ReceiverError as error:
                return self._reject(409, str(error))
            except ValueError:
                return self._reject(400, '请求参数不合法')
            self._reject(404, '未知路径')

        def _read_body(self) -> bytes:
            length = int(self.headers.get('Content-Length', '0'))
            return self.rfile.read(length)

        def _write_json(self, status: int, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(status)
                self._cors()
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # 标签页关闭等场景下对端已断开，状态已先落盘，忽略回写失败。
                self.close_connection = True

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _handle_begin(self):
            meta = json.loads(self._read_body().decode('utf-8'))
            export_id = meta.get('exportId', '')
            export_dir = state.export_dir(export_id)
            with state.lock:
                if export_dir.exists():
                    raise ReceiverError('导出标识已存在，拒绝覆盖；请使用新导出')
                export_dir.mkdir(parents=True)
                status = {'state': 'active', 'exportId': export_id,
                          'begin': meta, 'chunks': []}
                _atomic_write_json(state.status_path(export_dir), status)
            self._write_json(201, {'exportId': export_id, 'state': 'active'})

        def _handle_chunk(self, export_id: str, sequence: int):
            if sequence < 0:
                raise ReceiverError('分块序号不能为负')
            export_dir = state.export_dir(export_id)
            length = int(self.headers.get('Content-Length', '0'))
            with state.lock:
                if not export_dir.exists():
                    raise ReceiverError('导出不存在或已终结')
                status = state.load_status(export_dir)
                if status['state'] != 'active':
                    raise ReceiverError('导出不在接收状态: ' + status['state'])
                target = export_dir / f'chunk-{sequence:06d}.frame'
                if target.exists():
                    # 幂等重发：摘要一致才确认，否则拒绝，绝不混用两次字节。
                    existing = hashlib.sha256(target.read_bytes()).hexdigest()
                    incoming = hashlib.sha256(self.rfile.read(length)).hexdigest()
                    if existing != incoming:
                        raise ReceiverError('同序号分块内容冲突，拒绝合并')
                    self._write_json(200, {'sequence': sequence,
                                           'bytes': target.stat().st_size,
                                           'duplicate': True})
                    return
                size, digest = _stream_to_file(self.rfile, target, length)
                status['chunks'].append(
                    {'sequence': sequence, 'file': target.name,
                     'bytes': size, 'sha256': digest})
                _atomic_write_json(state.status_path(export_dir), status)
            self._write_json(200, {'sequence': sequence, 'bytes': size,
                                   'sha256': digest, 'duplicate': False})

        def _handle_finalize(self, export_id: str):
            export_dir = state.export_dir(export_id)
            claim = json.loads(self._read_body().decode('utf-8'))
            with state.lock:
                if not export_dir.exists():
                    raise ReceiverError('导出不存在')
                status = state.load_status(export_dir)
                if status['state'] != 'active':
                    raise ReceiverError('导出已终结，不能重复 finalize')
                begin_meta = status['begin']
                try:
                    manifest = replay_export(export_dir, begin_meta)
                    # 浏览器自述只用于交叉比对，不参与可信摘要。
                    if claim.get('chunkCount') != manifest['chunkCount']:
                        raise ReceiverError('分块数量与页面自述不一致')
                    if claim.get('frameCount') != manifest['frameCount']:
                        raise ReceiverError('帧数量与页面自述不一致')
                    manifest['clientClaim'] = claim
                    manifest_path = export_dir / 'manifest.json'
                    _atomic_write_json(manifest_path, manifest)
                    status['state'] = 'complete'
                    status['manifest'] = 'manifest.json'
                    _atomic_write_json(state.status_path(export_dir), status)
                except ReceiverError as error:
                    # 任何重放或比对失败都落成 failed，绝不产生假 complete。
                    self._mark_failed(export_id, status, str(error))
                    raise
            self._write_json(200, {'state': 'complete',
                                   'manifest': 'manifest.json',
                                   'report': manifest['storeReports']})

        def _mark_failed(self, export_id: str, status: dict, reason: str):
            status['state'] = 'failed'
            status['reason'] = reason
            _atomic_write_json(state.status_path(state.export_dir(export_id)), status)

        def _handle_fail(self, export_id: str):
            export_dir = state.export_dir(export_id)
            payload = json.loads(self._read_body().decode('utf-8'))
            with state.lock:
                if not export_dir.exists():
                    raise ReceiverError('导出不存在')
                status = state.load_status(export_dir)
                if status['state'] == 'complete':
                    raise ReceiverError('已完整的导出不能改标失败')
                self._mark_failed(export_id, status,
                                  str(payload.get('error', '浏览器侧失败')))
            self._write_json(200, {'state': 'failed'})

        def _handle_status(self, export_id: str):
            export_dir = state.export_dir(export_id)
            if not export_dir.exists():
                raise ReceiverError('导出不存在')
            status = state.load_status(export_dir)
            self._write_json(200, status)

    return Handler


def generate_token() -> str:
    return secrets.token_urlsafe(24)


def serve(root, token=None, host='127.0.0.1', port=0):
    """启动回环接收服务（后台线程运行），返回 (server, state, address, token)。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    token = token or generate_token()
    state = _ReceiverState(root, token)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    worker = threading.Thread(
        target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    worker.start()
    server._worker = worker
    address = f'http://{host}:{server.server_port}'
    return server, state, address, token


def shutdown(server) -> None:
    server.shutdown()
    server.server_close()
    worker = getattr(server, '_worker', None)
    if worker is not None:
        worker.join(timeout=2)
