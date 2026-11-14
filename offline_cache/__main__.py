DESCRIPTION = '离线网页本地数据库的授权一致性导出'
SAMPLE = 'fixtures/catalog.json'

import argparse
import json
from pathlib import Path
import tempfile

from .archive import archive_file
from .core import inspect_file
from .inspect_export import verify_export, list_exports, extract_blobs
from .receiver import serve as serve_receiver, shutdown as shutdown_receiver


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command',
                        choices=['inspect', 'capture', 'demo',
                                 'receive', 'verify', 'exports', 'extract'])
    parser.add_argument('path', nargs='?', default=SAMPLE)
    parser.add_argument('--archive', default='.work/raw')
    parser.add_argument('--root', default='.work/exports',
                        help='接收端落盘根目录或导出所在根目录')
    parser.add_argument('--token', default=None, help='接收端 Bearer 令牌；缺省自动生成')
    parser.add_argument('--port', type=int, default=0, help='接收端端口，0 为系统分配')
    parser.add_argument('--out', default=None, help='extract 命令的二进制输出目录')
    args = parser.parse_args()

    if args.command == 'inspect':
        result = inspect_file(Path(args.path))
    elif args.command == 'capture':
        result = archive_file(Path(args.path), Path(args.archive))
    elif args.command == 'demo':
        with tempfile.TemporaryDirectory() as directory:
            result = {'原始资料': archive_file(Path(args.path), Path(directory)),
                      '资料预览': inspect_file(Path(args.path))}
    elif args.command == 'receive':
        result = _run_receiver(args)
    elif args.command == 'verify':
        result = verify_export(Path(args.path))
    elif args.command == 'exports':
        result = {'exports': list_exports(Path(args.root))}
    else:  # extract
        if not args.out:
            parser.error('extract 命令需要 --out 输出目录')
        result = {'extracted': extract_blobs(Path(args.path), Path(args.out))}
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _run_receiver(args):
    import threading
    server, state, address, token = serve_receiver(
        Path(args.root), token=args.token, port=args.port)
    print(json.dumps({
        '提示': '令牌仅显示一次，粘贴到授权页面；Ctrl-C 停止服务',
        'endpoint': address,
        'token': token,
        'root': str(Path(args.root).resolve())
    }, ensure_ascii=False, indent=2))
    stop = threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_receiver(server)
    return {'state': 'stopped'}


if __name__ == '__main__':
    main()
