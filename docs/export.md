# 授权本地资料导出：协议、一致性边界与操作

本工具解决巡店员在断网页面里把 IndexedDB 资料完整带回的问题。它由两部分组成：

- **浏览器侧**（`fixtures/tbe.js`、`frame.js`、`exporter.js`、`export-console.html`）：
  在巡店员本人打开的授权页面内运行，只读当前源的 IndexedDB，把记录分块交给本机接收端。
- **本机接收侧**（`offline_cache/receiver.py`、`frame.py`、`tbe.py`、`inspect_export.py`）：
  仅绑定回环地址的 HTTP 服务，分块落盘、逐块确认，并独立重放校验生成清单。

浏览器不读取其他站点、不访问用户目录；接收端只监听 `127.0.0.1`，强制 Bearer 令牌，
并拒绝非回环 `Host`。

## 一次导出的读取边界

一次导出在打开数据库后，用**单个覆盖全部所选 store 的只读事务**读取。IndexedDB
只读事务呈现事务创建瞬间的一致性快照：导出进行期间，页面继续写入、联网后的同步继续
发生，都不会进入本次结果。因此一次导出对应一个确定状态，不会把前后两个时刻的记录
混成一个快照。

实现上，游标在启动任何异步编码（`Blob.arrayBuffer()`）或网络发送**之前**先
`cursor.continue()` 挂起下一个 IDB 请求，使事务在所有 `await` 间隙都有未完成请求，
不会因事件循环空转而自动提交。

结构边界：

- 不假定数据库名称或 store 数量。先 `indexedDB.databases()` 枚举页面可见数据库，
  再打开库读取真实的 `objectStoreNames`、每个 store 的 `keyPath`（含复合数组键路径）、
  `autoIncrement` 与索引信息，由巡店员勾选本次范围。
- 一次导出只覆盖同一个数据库内的 store（一个事务无法跨库），多库需分次导出。
- 只读导出打开数据库时不提供版本号，绝不触发结构升级；若意外触发升级立即中止。

## 值编码：TBE v1

普通 `JSON.stringify` 无法保留 Date、ArrayBuffer/TypedArray、Blob、Map、Set、BigInt、
`undefined` 与循环引用。TBE（Tagged Binary Export，见 `fixtures/tbe.js` 与
`offline_cache/tbe.py`）用类型标签逐一保留：

- 整数 zigzag 变长编码，双精度小端 f64，字符串是 u32 长度 + UTF-8 字节；
- Date 存毫秒时间戳，正则存 source/flags，BigInt 存十进制字符串；
- ArrayBuffer、11 种 TypedArray、Blob 都按**原始字节直传**，不做 base64 膨胀，
  Blob 额外保留 MIME；
- 复合对象用 DECL/REF 编号保留共享与循环引用，解码端先占槽位再回填，支持自引用。

浏览器与 Python 各有一份独立实现，并通过跨语言测试向量双向互测，任一端都不依赖
另一端的库。

## 分块帧格式与接收确认

记录封装为帧，多帧聚成 chunk 文件（`chunk-000000.frame` …）。帧头固定 56 字节小端：

```
魔数 "TBEF" | 版本 u8=1 | 帧类型 u8 | 保留 u16
负载长度 u32 | 存储序号 u32 | 帧序号 u64 | 负载 SHA-256 32B
随后是负载（记录帧为 TBE 的 {key, value}，END 帧为空）
```

- 帧类型 `1=RECORD`、`2=END`；每个 store 读完写一个 END 帧。
- 帧序号在整个导出内全局连续递增，存储序号是本次 stores 清单的下标。
- 浏览器按目标块大小（默认 4 MiB，可调）聚块，**串行** POST 到
  `/chunk/{exportId}/{sequence}`，收到 ACK 才发下一块，形成背压。照片 Blob 因此
  既不会在浏览器堆成整库，也不会一次性进入 Python 内存：接收端按 64 KiB 固定块
  流式落盘并增量计算摘要。
- 同一序号重发是幂等的：字节摘要一致才确认，内容冲突直接 409 拒绝，绝不混用两次字节。
- 接收端对每个 chunk 回 `{sequence, bytes, sha256}` 确认。

## 状态机：绝不留下假完整包

每个 exportId 一个独立目录，状态只有三种：

| 状态 | 含义 |
| --- | --- |
| `active` | 已 `/begin`，正在收块，只有 chunk 文件与 `status.json` |
| `complete` | `/finalize` 的独立重放全部通过后，原子写入 `manifest.json` 并改名 |
| `failed` | 浏览器显式 `/fail`、事务中止、版本变更或校验失败 |

`complete` 只能由接收端在**自己重新流式重放全部 chunk 并核对通过**后写入，浏览器的
自述（帧数、块数、记录数）仅作交叉比对，不参与可信摘要。任何失败路径都把状态落成
`failed`，因此磁盘上不可能出现「标成完整的半包」。

触发失败的情形：

- 数据库版本升级或删除（`onversionchange`）：浏览器立即 `abort` 事务并通知 `/fail`；
- 事务 `onabort`/`onerror`、网络错误、重放发现帧号缺口/重复键/摘要不符/缺 END 帧；
- 标签页关闭：浏览器来不及 finalize，目录停留在 `active`，独立校验判为需要重导。

恢复规则：重新导出总是生成**新的 exportId、新目录、新事务**，从不向旧目录追加或
合并，因此两次读取不会被当成同一个快照。旧的 `active/failed` 目录保留为证据。

## 可核对的清单

`manifest.json` 中每个 store 给出：

- `records`、`recordBytes`；
- `keyDigest`：该 store 全部键（复合键规范化为文本，逐键换行）的 SHA-256，用于核对键集合；
- `recordDigest`：按帧顺序对每条记录负载摘要再求的 SHA-256；
- `binary`：值中每个 Blob/ArrayBuffer/TypedArray 的 MIME/类型、字节数与 SHA-256；
- `resolvedRefs` / `danglingRefs`：对象字符串字段到其他 store 键的关联，区分已解析与
  未解析（例如门店名不是任何 store 的键，会如实列为未解析，而不是静默丢弃）；
- `complete`：是否收到 END 帧读到 store 末尾。

chunk 级还有整文件 SHA-256 列表。

## 操作流程

接收端（本机，令牌只显示一次）：

```sh
python3 -m offline_cache receive --root .work/exports --port 8765
```

巡店员在授权页面打开 `/console`（演练服务路径），粘贴回环地址与令牌：

1. 「测试连接」确认回环可达；
2. 「扫描数据库」查看可见库与 store 结构，勾选本次范围；
3. 设定分块大小后「开始导出」，进度区显示已确认块数、帧数与各 store 记录数。

客服在任意时刻用独立命令检查，不依赖浏览器：

```sh
# 总览所有导出的状态
python3 -m offline_cache exports --root .work/exports
# 重放并核对单个导出，逐 store 报告 complete / needs_reexport
python3 -m offline_cache verify .work/exports/<exportId>
# 按内容寻址提取 Blob/二进制
python3 -m offline_cache extract .work/exports/<exportId> --out .work/blobs
```

`verify` 不信任落盘 manifest 文本，而是重新读取全部 chunk 重放，再与 manifest 比对；
状态非 complete、缺 END、摘要不符或关联断裂时，对应 store 标为 `needs_reexport`，
明确告知需要重新导出的范围。
