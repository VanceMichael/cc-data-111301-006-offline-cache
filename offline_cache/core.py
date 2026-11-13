import json
from pathlib import Path


def inspect_file(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value['stores'], list):
        raise ValueError('对象仓库清单不合法')
    return {'database':value['database'],'version':value['version'],'stores':value['stores']}
