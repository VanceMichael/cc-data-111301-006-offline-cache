# 来源资料约定

网页使用浏览器原生 IndexedDB。visits 的键路径是 [store, visit]，photos 使用外部键；走访值包含 Date 和 Uint8Array，照片值为 Blob。页面的新增按钮写入走访，升级按钮把数据库版本推进到 2 并增加 notes。页面本身不包含导出 API；授权导出能力由 `export-console.html` 配合 `tbe.js`、`frame.js`、`exporter.js` 在同源页面内提供，详见 `docs/export.md`。

## 导出编解码约定

- TBE v1 标签编码保留 Date、ArrayBuffer/TypedArray、Blob、Map、Set、BigInt、`undefined` 与循环引用，Blob 按原始字节直传。浏览器实现见 `fixtures/tbe.js`，Python 独立实现见 `offline_cache/tbe.py`，两端用跨语言测试向量互测。
- 分块帧（TBEF v1）见 `fixtures/frame.js` 与 `offline_cache/frame.py`：帧头 56 字节含负载 SHA-256，帧序号全局连续，存储序号对应本次 stores 清单下标，RECORD/END 两种帧类型。
- 一次导出用单个跨所选 store 的只读事务作为一致性快照边界；中断只产生 `active/failed` 半包，重导使用新 exportId，不与旧目录合并。
