# 来源资料约定

网页使用浏览器原生 IndexedDB。visits 的键路径是 [store, visit]，photos 使用外部键；走访值包含 Date 和 Uint8Array，照片值为 Blob。页面的新增按钮写入走访，升级按钮把数据库版本推进到 2 并增加 notes。页面不包含导出 API。
