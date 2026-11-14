/*
 * exporter.js —— 授权页面内执行的 IndexedDB 本地资料导出引擎。
 *
 * 一致性边界（关键）：
 *   一次导出在「打开数据库」之后，用单个覆盖全部所选 store 的只读事务读取。
 *   IndexedDB 只读事务看到的是事务创建瞬间的一致性快照：导出期间页面的写入
 *   不会进入本次结果。读取用游标流水线推进，帧构造是同步微任务、网络发送在
 *   游标请求已挂起之后才等待，因此事务始终有未完成的 IDB 请求，不会因为等待
 *   接收端 ACK 而自动提交。
 *
 * 分块与背压：
 *   记录按目标字节数聚成 chunk，串行 POST 给回环接收端，逐块拿到 ACK 后再发
 *   下一块；在途块数量有上限，超出时先排空再继续游标，照片 Blob 不会在浏览器
 *   或 Python 一侧堆积成整库内存。
 *
 * 失败语义：
 *   versionchange（其他标签页升级/删除库）、事务 abort、网络错误都会中止导出，
 *   并通知接收端把本次导出标记为 failed。任何中断都不会产生 complete 目录；
 *   重新导出使用全新 exportId 与全新事务，绝不与旧目录合并成一个快照。
 *
 * 安全：只调用当前页面源内的 indexedDB，与接收端通信只允许回环地址，
 * 不读取其他站点、不访问用户目录。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./tbe.js'), require('./frame.js'));
  } else root.TBEExport = factory(root.TBE, root.TBEFrame);
})(typeof self !== 'undefined' ? self : this, function (TBE, TBEFrame) {
  'use strict';

  const DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024;

  function loopbackOrigin (endpoint) {
    let url;
    try {
      url = new URL(endpoint);
    } catch (error) {
      throw new Error('接收端地址不合法: ' + endpoint);
    }
    const host = url.hostname.toLowerCase();
    if (url.protocol !== 'http:' ||
        !(host === '127.0.0.1' || host === '::1' || host === 'localhost')) {
      throw new Error('仅允许向本机回环接收端交接资料: ' + endpoint);
    }
    return url.origin + url.pathname.replace(/\/$/, '');
  }

  // 读取页面可见的全部数据库，不假定固定库名。databases() 不可用时明确报错。
  async function listDatabases () {
    if (!indexedDB || !indexedDB.databases) {
      throw new Error('当前浏览器不支持 indexedDB.databases()，无法枚举数据库');
    }
    const infos = await indexedDB.databases();
    return infos
      .filter((info) => info.name && info.name.length > 0)
      .map((info) => ({ name: info.name, version: info.version }));
  }

  function openDatabase (name) {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(name);
      request.onupgradeneeded = () => {
        // 只读导出绝不触发结构升级；立即关闭并失败。
        try { request.result.close(); } catch (error) { /* 忽略 */ }
        reject(new Error('打开时触发了意外的结构升级，已中止: ' + name));
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('打开数据库失败'));
      request.onblocked = () => reject(new Error('数据库被其他连接阻塞: ' + name));
    });
  }

  // 读取库内真实结构，不假定 store 名称、键路径或索引。
  function describeSchema (database) {
    return Array.from(database.objectStoreNames).map((storeName) => {
      const store = database.transaction(storeName, 'readonly').objectStore(storeName);
      const indexes = Array.from(store.indexNames).map((indexName) => {
        const index = store.index(indexName);
        return {
          name: indexName,
          keyPath: index.keyPath === undefined ? null : index.keyPath,
          unique: index.unique,
          multiEntry: index.multiEntry
        };
      });
      return {
        name: storeName,
        keyPath: store.keyPath === undefined ? null : store.keyPath,
        autoIncrement: store.autoIncrement,
        indexes
      };
    });
  }

  async function requestJson (base, token, path, method, body) {
    const response = await fetch(base + path, {
      method,
      headers: {
        Authorization: 'Bearer ' + token,
        'Content-Type': 'application/octet-stream'
      },
      body: body === undefined ? undefined : body
    });
    const text = await response.text();
    let parsed = null;
    if (text) {
      try { parsed = JSON.parse(text); } catch (error) { parsed = null; }
    }
    if (!response.ok) {
      const failure = new Error(parsed && parsed.error ? parsed.error : ('HTTP ' + response.status));
      failure.status = response.status;
      throw failure;
    }
    return parsed;
  }

  function makeExportId () {
    const stamp = new Date().toISOString().replace(/[-:T.Z]/g, '').slice(0, 14);
    const random = crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
      : Array.from(crypto.getRandomValues(new Uint8Array(6)),
        (byte) => byte.toString(16).padStart(2, '0')).join('');
    return 'export-' + stamp + '-' + random;
  }

  function concatFrames (frames) {
    const total = frames.reduce((sum, item) => sum + item.byteLength, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const frame of frames) {
      out.set(frame, offset);
      offset += frame.byteLength;
    }
    return out;
  }

  // 主入口。options: endpoint, token, databaseName, stores[],
  // chunkBytes?, onProgress?
  async function runExport (options) {
    const base = loopbackOrigin(options.endpoint);
    const token = options.token;
    const chunkBytes = options.chunkBytes || DEFAULT_CHUNK_BYTES;
    const onProgress = options.onProgress || (() => {});

    const exportId = makeExportId();
    const database = await openDatabase(options.databaseName);
    const schema = describeSchema(database);

    const selected = options.stores.slice();
    const missing = selected.filter((name) => !database.objectStoreNames.contains(name));
    if (missing.length) {
      database.close();
      throw new Error('所选对象仓库已不存在: ' + missing.join(', '));
    }
    const stores = schema.filter((item) => selected.includes(item.name));
    const storeIndex = new Map(stores.map((item, index) => [item.name, index]));

    await requestJson(base, token, '/begin', 'POST', JSON.stringify({
      exportId,
      database: { name: database.name, version: database.version },
      origin: location.origin,
      stores,
      chunkBytes
    }));

    const state = {
      exportId,
      database: database.name,
      version: database.version,
      stores: stores.map((item) => item.name),
      chunkBytes,
      frame: 0,
      chunk: 0,
      records: Object.fromEntries(stores.map((item) => [item.name, 0])),
      bytes: 0,
      chunksAcked: 0,
      finished: false,
      failed: null
    };
    const emit = () => onProgress({ ...state });

    // 单一只读事务覆盖全部所选 store —— 本次读取的一致性快照边界。
    const transaction = database.transaction(selected, 'readonly');

    // 事务生命周期承诺在发起任何请求前就登记好，避免晚注册丢事件。
    const transactionSettled = new Promise((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(transaction.error || new Error('读取事务被中止'));
      transaction.onerror = () => reject(transaction.error || new Error('读取事务发生错误'));
    });
    // 其他标签页请求升级/删除库：本连接让位，导出立即失败，绝不留下半包。
    database.onversionchange = () => {
      try { transaction.abort(); } catch (error) { /* 忽略 */ }
    };

    let abortReason = null;
    const fail = (error) => {
      if (!abortReason) abortReason = error;
      try { transaction.abort(); } catch (error) { /* 已结束 */ }
    };

    // 串行上传的分块队列，逐块拿到 ACK 再发下一块，实现背压。
    const queue = [];
    let uploadChain = Promise.resolve();

    function enqueueChunk (bytes) {
      const sequence = state.chunk;
      state.chunk += 1;
      state.bytes += bytes.byteLength;
      queue.push({ sequence, bytes });
      uploadChain = uploadChain.then(pump).catch((error) => {
        fail(error);
        throw error;
      });
    }

    async function pump () {
      while (queue.length) {
        if (abortReason) throw abortReason;
        const item = queue.shift();
        await sendChunk(item.sequence, item.bytes);
        state.chunksAcked += 1;
        emit();
      }
    }

    async function sendChunk (sequence, bytes) {
      // 同序号重发幂等：接收端按序号去重并核对整 chunk 摘要。
      let attempt = 0;
      for (;;) {
        try {
          const ack = await requestJson(base, token,
            '/chunk/' + exportId + '/' + sequence, 'POST', bytes);
          if (ack.sequence !== sequence || ack.bytes !== bytes.byteLength) {
            throw new Error('接收确认与请求不匹配');
          }
          return ack;
        } catch (error) {
          attempt += 1;
          const permanent = error.status && error.status >= 400 && error.status < 500;
          if (permanent || attempt >= 3) throw error;
          await new Promise((resolve) => setTimeout(resolve, 200 * attempt));
        }
      }
    }

    function readStore (storeName) {
      const index = storeIndex.get(storeName);
      const store = transaction.objectStore(storeName);
      let frames = [];
      let chunkSize = 0;

      async function flush (endFrame) {
        if (endFrame) frames.push(endFrame);
        if (!frames.length) return;
        const chunk = concatFrames(frames);
        frames = [];
        chunkSize = 0;
        enqueueChunk(chunk);
      }

      return new Promise((resolve, reject) => {
        // 处理链：每条记录「先同步 continue 挂起下一个游标请求，再 await 编码」，
        // 使事务在任何异步间隙都有未完成的 IDB 请求，绝不自动提交。
        let chain = Promise.resolve();
        let done = false;

        const cursorRequest = store.openCursor();
        cursorRequest.onsuccess = (event) => {
          if (abortReason) {
            if (!done) { done = true; reject(abortReason); }
            return;
          }
          const cursor = event.target.result;
          if (!cursor) {
            chain = chain.then(async () => {
              const endFrame = await TBEFrame.buildEndFrame(state.frame, index);
              state.frame += 1;
              await flush(endFrame);
              if (!done) { done = true; resolve(); }
            }).catch((error) => {
              if (!done) { done = true; reject(error); }
            });
            return;
          }
          // 关键：在启动任何异步编码前先推进游标，挂起下一次请求。
          cursor.continue();
          const snapshotKey = cursor.key;
          const snapshotValue = cursor.value;
          chain = chain.then(async () => {
            const payload = await TBE.encode({ key: snapshotKey, value: snapshotValue });
            const frame = await TBEFrame.buildRecordFrame(state.frame, index, payload);
            state.frame += 1;
            state.records[storeName] += 1;
            frames.push(frame);
            chunkSize += frame.byteLength;
            emit();
            if (chunkSize >= chunkBytes) {
              await flush(null);
              // 排空在途分块；此时下一个游标请求早已挂起，事务保持活跃。
              await uploadChain;
            }
            if (abortReason) throw abortReason;
          }).catch((error) => {
            if (!done) { done = true; reject(error); }
            throw error;
          });
        };
        cursorRequest.onerror = () =>
          reject(cursorRequest.error || new Error('游标读取失败: ' + storeName));
      });
    }

    try {
      for (const storeName of selected) {
        await readStore(storeName);
      }
      // 所有分块拿到 ACK，且事务自然完成（未 abort）。
      await uploadChain;
      await transactionSettled;
      if (abortReason) throw abortReason;

      const finalized = await requestJson(base, token,
        '/finalize/' + exportId, 'POST', JSON.stringify({
          frameCount: state.frame,
          chunkCount: state.chunk,
          records: state.records
        }));
      state.finished = true;
      state.manifest = finalized.manifest;
      state.report = finalized.report;
      emit();
      return state;
    } catch (error) {
      state.failed = String((error && error.message) || error);
      emit();
      try {
        await requestJson(base, token, '/fail/' + exportId, 'POST',
          JSON.stringify({ error: state.failed })).catch(() => {});
      } catch (error2) { /* 接收端不可达不改变失败结论 */ }
      throw error;
    } finally {
      database.close();
    }
  }

  return {
    listDatabases, openDatabase, describeSchema, runExport,
    loopbackOrigin, DEFAULT_CHUNK_BYTES
  };
});
