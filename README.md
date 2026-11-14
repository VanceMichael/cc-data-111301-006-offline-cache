# 离线网页本地数据库的授权一致性导出

这个 Python 项目用于业务来源资料的检查与原始字节归档，并提供在授权页面内执行的
IndexedDB 本地资料导出。当前提供单份资料预览、内容寻址保存，以及分块、可核对、
带一致性快照边界的浏览器导出。`offline_cache/core.py` 处理现有资料预览，`archive.py`
保存原文件，`receiver.py` 是回环接收端，`tbe.py`/`frame.py` 是独立编解码与帧重放，
`inspect_export.py` 提供独立校验与提取；`fixtures/` 保存本地业务演练资料与浏览器侧
脚本，`tests/` 检查已有行为。设计与协议见 `docs/export.md`。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`；空清单表示只使用标准库。
浏览器端到端测试另用 Playwright（已在 requirements 中固定）。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

## 运行

```sh
python3 -m offline_cache inspect fixtures/catalog.json
python3 -m offline_cache capture fixtures/catalog.json --archive .work/raw
```

归档返回 SHA-256、字节数及原文件名；文件名不参与内容身份。预览只说明单份来源的基本结构，不代表关联资料已经齐全。输入不可识别时命令失败，不产生空的成功结果。

## 本地资料导出

先在本机启动只绑定回环地址的接收端（令牌仅显示一次）：

```sh
python3 -m offline_cache receive --root .work/exports --port 8765
```

巡店员在演练来源的授权页面打开 `/console`，粘贴回环地址与令牌，扫描可见数据库与
store 结构、勾选本次范围后导出。一次导出使用单个只读事务快照，分块交接并逐块确认，
Blob 按原始字节直传。客服随后用独立命令核对：

```sh
python3 -m offline_cache exports --root .work/exports
python3 -m offline_cache verify .work/exports/<exportId>
python3 -m offline_cache extract .work/exports/<exportId> --out .work/blobs
```

`verify` 重新流式重放全部 chunk，逐 store 报告 `complete` 或 `needs_reexport`；
中断、版本升级、事务中止或标签页关闭都不会留下被标成完整的半包，重导生成独立目录。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q offline_cache tests
```

## 本地演示

```sh
python3 -m offline_cache demo
```

演示实际读取来源资料，打印原始归档摘要及结构预览，使用临时目录并自动清理。所有示例身份与业务数值均为虚构，不需要外部账号。

浏览器页面冒烟：

```sh
python3 -m offline_cache.browser
```

