"""独立读取与校验命令使用的逻辑。

不依赖浏览器、也不信任导出目录里的 manifest 文本：verify 重新流式读取全部
chunk 并重放帧/TBE，得到「当前实际状态」，再与落盘的 manifest 比对。这样即使
manifest 被手工改动，校验结果仍以字节重放为准。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .receiver import ReceiverError, replay_export
from .tbe import BlobValue


@dataclass
class StoreVerdict:
    name: str
    state: str  # complete / needs_reexport
    records: int
    key_digest: str
    record_digest: str
    binary_count: int
    dangling_refs: int
    notes: list


def _load_status(export_dir: Path):
    status_path = export_dir / 'status.json'
    if not status_path.exists():
        return None
    return json.loads(status_path.read_text(encoding='utf-8'))


def verify_export(export_dir: Path):
    """返回一份可直接打印的核对结论。state 为 needs_reexport 时必须重导。"""
    export_dir = Path(export_dir)
    status = _load_status(export_dir)
    if status is None:
        raise ReceiverError(f'{export_dir} 不是一个导出目录（缺少 status.json）')

    verdict = {
        'exportId': status.get('exportId'),
        'directory': str(export_dir),
        'declaredState': status.get('state'),
        'effectiveState': 'needs_reexport',
        'stores': [],
        'problems': []
    }

    if status.get('state') != 'complete':
        verdict['problems'].append(
            '导出状态为 ' + str(status.get('state')) + '，不是 complete，需要重新导出')
        # 仍尽量重放，给出已收到的部分规模，帮助判断重导范围。
        begin = status.get('begin')
        if begin:
            try:
                partial = replay_export(export_dir, begin)
                verdict['partialReplay'] = _summarize(partial)
            except ReceiverError as error:
                verdict['problems'].append('部分重放也未通过: ' + str(error))
        return verdict

    begin = status['begin']
    try:
        replayed = replay_export(export_dir, begin)
    except ReceiverError as error:
        verdict['problems'].append('独立重放失败: ' + str(error))
        return verdict

    manifest_path = export_dir / 'manifest.json'
    saved = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not _manifests_agree(saved, replayed):
        verdict['problems'].append('落盘 manifest 与字节重放结果不一致，manifest 不可信')

    saved_reports = {item['name']: item for item in saved.get('storeReports', [])}
    for report in replayed['storeReports']:
        saved_report = saved_reports.get(report['name'], {})
        notes = []
        store_state = 'complete'
        if not report['complete']:
            store_state = 'needs_reexport'
            notes.append('缺少 END 帧，读取未到 store 末尾')
        if report['danglingRefs']:
            notes.append(f"存在 {len(report['danglingRefs'])} 条未落到其他 store 键的引用")
        if saved_report.get('keyDigest') and \
                saved_report.get('keyDigest') != report['keyDigest']:
            store_state = 'needs_reexport'
            notes.append('键集合摘要与 manifest 记录不符')
        verdict['stores'].append({
            'name': report['name'],
            'state': store_state,
            'records': report['records'],
            'recordBytes': report['recordBytes'],
            'keyDigest': report['keyDigest'],
            'recordDigest': report['recordDigest'],
            'binaryCount': len(report['binary']),
            'danglingRefs': len(report['danglingRefs']),
            'notes': notes
        })

    if not verdict['problems'] and all(
            item['state'] == 'complete' for item in verdict['stores']):
        verdict['effectiveState'] = 'complete'
    else:
        if any(item['state'] == 'needs_reexport' for item in verdict['stores']):
            verdict['problems'].append('有 store 未读完整，需要重新导出')
    return verdict


def _summarize(manifest: dict) -> dict:
    return {
        'frameCount': manifest['frameCount'],
        'chunkCount': manifest['chunkCount'],
        'stores': [
            {'name': item['name'], 'records': item['records'],
             'complete': item['complete']}
            for item in manifest['storeReports']
        ]
    }


def _manifests_agree(saved: dict, replayed: dict) -> bool:
    if saved.get('frameCount') != replayed['frameCount']:
        return False
    if saved.get('chunkCount') != replayed['chunkCount']:
        return False
    if len(saved.get('storeReports', [])) != len(replayed['storeReports']):
        return False
    for left, right in zip(saved['storeReports'], replayed['storeReports']):
        for field in ('name', 'records', 'keyDigest', 'recordDigest'):
            if left.get(field) != right.get(field):
                return False
    saved_chunks = [(item['file'], item['sha256']) for item in saved.get('chunks', [])]
    replayed_chunks = [(item['file'], item['sha256']) for item in replayed['chunks']]
    return saved_chunks == replayed_chunks


def list_exports(root: Path):
    """枚举接收根目录下的全部导出及其状态，供客服总览哪些要重导。"""
    root = Path(root)
    if not root.exists():
        return []
    result = []
    for export_dir in sorted(root.iterdir()):
        if not export_dir.is_dir():
            continue
        status = _load_status(export_dir)
        if status is None:
            continue
        result.append({
            'exportId': status.get('exportId', export_dir.name),
            'state': status.get('state'),
            'directory': str(export_dir),
            'chunks': len(status.get('chunks', []))
        })
    return result


def extract_blobs(export_dir: Path, destination: Path):
    """把重放过程中遇到的 Blob/二进制按内容寻址写出，返回提取清单。

    仍按 chunk/帧/记录逐级处理，避免整库进内存；同名内容只写一份。
    """
    from .frame import iter_frames, FRAME_RECORD
    from .tbe import decode as tbe_decode

    export_dir = Path(export_dir)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    begin = _load_status(export_dir)['begin']
    store_names = [item['name'] for item in begin['stores']]
    written = {}

    for chunk_path in sorted(export_dir.glob('chunk-*.frame')):
        for frame in iter_frames(chunk_path.read_bytes()):
            if frame.kind != FRAME_RECORD:
                continue
            record = tbe_decode(frame.payload)
            store_name = store_names[frame.store_index]
            for blob in _iter_blobs(record['value']):
                digest = hashlib.sha256(blob.data).hexdigest()
                if digest in written:
                    continue
                suffix = _suffix_for_mime(blob.mime)
                target = destination / f'{digest}{suffix}'
                target.write_bytes(blob.data)
                written[digest] = {
                    'store': store_name,
                    'file': target.name,
                    'mime': blob.mime,
                    'bytes': len(blob.data),
                    'sha256': digest
                }
    return list(written.values())


def _iter_blobs(value):
    if isinstance(value, BlobValue):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_blobs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_blobs(item)
    elif isinstance(value, set):
        for item in value:
            yield from _iter_blobs(item)


def _suffix_for_mime(mime: str) -> str:
    mapping = {
        'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
        'image/webp': '.webp', 'application/pdf': '.pdf',
        'application/octet-stream': '.bin', 'text/plain': '.txt'
    }
    return mapping.get((mime or '').split(';')[0].strip(), '.bin')
