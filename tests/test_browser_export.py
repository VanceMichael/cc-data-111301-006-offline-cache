import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from offline_cache.http_source import source_server
from offline_cache.receiver import serve, shutdown
from offline_cache.inspect_export import verify_export, list_exports, extract_blobs


class BrowserExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.export_root = Path(self.temp.name) / 'exports'
        self.server, self.state, self.receiver, self.token = serve(
            self.export_root, token='browser-token-1234567890')
        self._source_cm = source_server('fixtures')
        self.portal, self.source_state = self._source_cm.__enter__()
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True, args=['--no-sandbox'])

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()
        self._source_cm.__exit__(None, None, None)
        shutdown(self.server)
        self.temp.cleanup()

    def _open_portal(self, page):
        page.goto(self.portal, wait_until='domcontentloaded')
        page.get_by_text('资料就绪', exact=True).wait_for(timeout=5000)
        # 注入同源于演练服务的三段导出脚本。
        page.add_script_tag(url='/tbe.js')
        page.add_script_tag(url='/frame.js')
        page.add_script_tag(url='/exporter.js')

    def _run_export(self, page, stores, chunk_bytes=65536):
        return page.evaluate(
            """async ({endpoint, token, stores, chunkBytes}) =>
                await TBEExport.runExport({
                  endpoint, token, databaseName: '巡店资料',
                  stores, chunkBytes,
                })""",
            {'endpoint': self.receiver, 'token': self.token,
             'stores': stores, 'chunkBytes': chunk_bytes})

    def _manifest(self, export_id):
        return json.loads((self.export_root / export_id /
                           'manifest.json').read_text(encoding='utf-8'))

    def _status(self, export_id):
        return json.loads((self.export_root / export_id /
                           'status.json').read_text(encoding='utf-8'))

    def test_full_export_preserves_types_blob_and_composite_key(self):
        page = self.browser.new_page()
        self._open_portal(page)
        result = self._run_export(page, ['visits', 'photos'])
        self.assertTrue(result['finished'])
        manifest = self._manifest(result['exportId'])
        reports = {item['name']: item for item in manifest['storeReports']}

        # 复合键 store：初始一条走访。
        self.assertEqual(reports['visits']['records'], 1)
        visit_keys = self._collect_keys(result['exportId'], 'visits')
        self.assertEqual(visit_keys, [['门店甲', 1]])

        # 照片 Blob 原始字节摘要与预置内容一致，未经过 base64。
        self.assertEqual(reports['photos']['records'], 1)
        binary = reports['photos']['binary']
        self.assertEqual(len(binary), 1)
        self.assertEqual(binary[0]['sha256'],
                         hashlib.sha256(bytes([0, 255, 16, 32])).hexdigest())
        self.assertEqual(binary[0]['mime'], 'application/octet-stream')

        # visits 里的 photo 引用能解析到 photos 的键。
        self.assertTrue(any(ref['target'] == 'photo-a'
                            for ref in reports['visits']['resolvedRefs']))

        # 独立校验判定完整。
        verdict = verify_export(self.export_root / result['exportId'])
        self.assertEqual(verdict['effectiveState'], 'complete')
        page.close()

    def _collect_keys(self, export_id, store_name):
        """用独立 TBE 重放收集某 store 的真实键，验证复合键未被 JSON 扁平化。"""
        from offline_cache.frame import iter_frames, FRAME_RECORD
        from offline_cache.tbe import decode
        begin = self._status(export_id)['begin']
        names = [item['name'] for item in begin['stores']]
        index = names.index(store_name)
        keys = []
        for chunk in sorted((self.export_root / export_id).glob('chunk-*.frame')):
            for frame in iter_frames(chunk.read_bytes()):
                if frame.kind == FRAME_RECORD and frame.store_index == index:
                    keys.append(decode(frame.payload)['key'])
        return keys

    def test_snapshot_excludes_writes_during_export(self):
        page = self.browser.new_page()
        self._open_portal(page)

        # 给每个分块交接加延迟，制造导出期间页面仍在写入的窗口。
        def slow_down(route):
            time.sleep(0.4)
            route.continue_()
        page.route('**/chunk/**', slow_down)

        export_promise = page.evaluate(
            """async ({endpoint, token}) => await TBEExport.runExport({
                 endpoint, token, databaseName: '巡店资料',
                 stores: ['visits'], chunkBytes: 65536,
               })""",
            {'endpoint': self.receiver, 'token': self.token})
        # 等待第一个分块进入在途，再点新增走访（导出事务已建立快照）。
        time.sleep(0.3)
        page.locator('#append').click()
        page.get_by_text('走访已保存', exact=True).wait_for(timeout=5000)
        result = export_promise

        manifest = self._manifest(result['exportId'])
        visits = next(r for r in manifest['storeReports'] if r['name'] == 'visits')
        # 快照只含事务开始时的 1 条，导出期间新增的第 2 条不得混入。
        self.assertEqual(visits['records'], 1)
        # 而数据库本身确实已有两条，证明读取边界是事务快照而非实时状态。
        count = page.evaluate(
            """async () => new Promise((resolve) => {
                 const req = indexedDB.open('巡店资料');
                 req.onsuccess = () => {
                   const db = req.result;
                   const tx = db.transaction('visits', 'readonly');
                   const cursor = tx.objectStore('visits').openCursor();
                   let n = 0;
                   cursor.onsuccess = (e) => {
                     if (e.target.result) { n++; e.target.result.continue(); }
                     else { db.close(); resolve(n); }
                   };
                 };
               })""")
        self.assertEqual(count, 2)
        page.unroute('**/chunk/**')
        page.close()

    def test_version_upgrade_during_export_marks_failed_not_complete(self):
        page = self.browser.new_page()
        self._open_portal(page)
        # 灌入足够多的记录，配合极小分块，让导出在升级发生时仍在进行。
        page.evaluate(
            """async () => new Promise((resolve, reject) => {
                 const req = indexedDB.open('巡店资料');
                 req.onsuccess = () => {
                   const db = req.result;
                   const tx = db.transaction(['visits'], 'readwrite');
                   const store = tx.objectStore('visits');
                   for (let i = 100; i < 400; i++) {
                     store.put({ store: '门店甲', visit: i, at: new Date(),
                                 photo: null, pad: 'x'.repeat(2000) });
                   }
                   tx.oncomplete = () => { db.close(); resolve(); };
                   tx.onerror = () => reject(tx.error);
                 };
                 req.onerror = () => reject(req.error);
               })""")
        # 让每个分块交接都明显变慢，确保升级能插入导出窗口。
        page.route('**/chunk/**', lambda route: (time.sleep(0.3), route.continue_()))

        with self.assertRaises(Exception):
            self._export_with_concurrent_upgrade(page)

        # 接收端应记录一个非 complete 的半包，且不存在 manifest。
        exports = list_exports(self.export_root)
        self.assertEqual(len(exports), 1)
        export_id = exports[0]['exportId']
        self.assertNotEqual(self._status(export_id)['state'], 'complete')
        self.assertFalse((self.export_root / export_id / 'manifest.json').exists())
        verdict = verify_export(self.export_root / export_id)
        self.assertEqual(verdict['effectiveState'], 'needs_reexport')
        page.close()

    def _export_with_concurrent_upgrade(self, page):
        """启动导出后立即从同页触发版本升级，返回被拒绝的导出承诺。"""
        return page.evaluate(
            """async ({endpoint, token}) => {
                 const exporting = TBEExport.runExport({
                   endpoint, token, databaseName: '巡店资料',
                   stores: ['visits', 'photos'], chunkBytes: 8192,
                 });
                 // 给导出事务建立的时间，再触发升级。
                 await new Promise((r) => setTimeout(r, 300));
                 const upgrade = new Promise((resolve, reject) => {
                   const req = indexedDB.open('巡店资料', 2);
                   req.onupgradeneeded = () => {
                     if (!req.result.objectStoreNames.contains('notes')) {
                       req.result.createObjectStore('notes');
                     }
                   };
                   req.onsuccess = () => { req.result.close(); resolve(); };
                   req.onerror = () => reject(req.error);
                   req.onblocked = () => reject(new Error('升级被阻塞'));
                 });
                 await upgrade;
                 await exporting;
               }""",
            {'endpoint': self.receiver, 'token': self.token})

    def test_closed_tab_leaves_no_complete_and_reexport_is_separate(self):
        page = self.browser.new_page()
        self._open_portal(page)
        page.route('**/chunk/**', lambda route: (time.sleep(0.5), route.continue_()))

        page.evaluate(
            """async ({endpoint, token}) => {
                 window._export = TBEExport.runExport({
                   endpoint, token, databaseName: '巡店资料',
                   stores: ['visits', 'photos'], chunkBytes: 65536,
                 }).catch((e) => e);
               }""",
            {'endpoint': self.receiver, 'token': self.token})
        time.sleep(0.3)
        page.close()  # 标签页关闭，导出中断

        # 旧导出绝不能是 complete。
        time.sleep(0.2)
        old = list_exports(self.export_root)
        self.assertEqual(len(old), 1)
        self.assertNotEqual(old[0]['state'], 'complete')
        self.assertEqual(verify_export(Path(old[0]['directory']))['effectiveState'],
                         'needs_reexport')

        # 重新导出：全新 exportId、独立目录，完成后与旧目录并存、不混合。
        page2 = self.browser.new_page()
        self._open_portal(page2)
        result = self._run_export(page2, ['visits', 'photos'])
        self.assertTrue(result['finished'])
        exports = list_exports(self.export_root)
        self.assertEqual(len(exports), 2)
        ids = {item['exportId'] for item in exports}
        self.assertIn(result['exportId'], ids)
        self.assertEqual(len(ids), 2)  # 两次读取没有混成一个快照
        self.assertEqual(verify_export(
            self.export_root / result['exportId'])['effectiveState'], 'complete')
        page2.close()

    def test_large_blob_is_streamed_as_raw_bytes_with_small_chunks(self):
        # 约 3 MiB 的照片，配合 256 KiB 分块：证明 Blob 原始字节直传、
        # 分块交接且 Python 端逐块落盘，不做 base64、不整库进内存。
        page = self.browser.new_page()
        self._open_portal(page)
        size = 3 * 1024 * 1024
        expected_digest = page.evaluate(
            """async (size) => {
                 const bytes = new Uint8Array(size);
                 for (let i = 0; i < size; i++) bytes[i] = i & 0xff;
                 const blob = new Blob([bytes], { type: 'image/jpeg' });
                 await new Promise((resolve, reject) => {
                   const req = indexedDB.open('巡店资料');
                   req.onsuccess = () => {
                     const db = req.result;
                     const tx = db.transaction('photos', 'readwrite');
                     tx.objectStore('photos').put(blob, 'photo-big');
                     tx.oncomplete = () => { db.close(); resolve(); };
                     tx.onerror = () => reject(tx.error);
                   };
                   req.onerror = () => reject(req.error);
                 });
                 const digest = await crypto.subtle.digest('SHA-256', bytes);
                 return Array.from(new Uint8Array(digest))
                   .map((b) => b.toString(16).padStart(2, '0')).join('');
               }""", size)

        result = self._run_export(page, ['photos'], chunk_bytes=256 * 1024)
        manifest = self._manifest(result['exportId'])
        photos = next(r for r in manifest['storeReports'] if r['name'] == 'photos')
        big = next(item for item in photos['binary']
                   if item['sha256'] == expected_digest)
        self.assertEqual(big['bytes'], size)
        self.assertEqual(big['mime'], 'image/jpeg')
        # 至少被切成多个 chunk，证明是分块交接而非一次塞入。
        self.assertGreater(manifest['chunkCount'], 1)
        # 提取出的 Blob 字节摘要与浏览器侧独立计算一致。
        with tempfile.TemporaryDirectory() as out:
            extracted = extract_blobs(self.export_root / result['exportId'], Path(out))
            target = next(item for item in extracted if item['sha256'] == expected_digest)
            self.assertEqual((Path(out) / target['file']).stat().st_size, size)
        page.close()

    def test_console_page_lists_databases_and_schema(self):
        # 同一浏览器上下文里先建库、再开控制台（模拟同源多标签共享 IndexedDB）。
        context = self.browser.new_context()
        seed = context.new_page()
        seed.goto(self.portal, wait_until='domcontentloaded')
        seed.get_by_text('资料就绪', exact=True).wait_for(timeout=5000)

        page = context.new_page()
        page.goto(self.portal + '/console', wait_until='domcontentloaded')
        page.locator('#scan').click()
        page.wait_for_selector('text=巡店资料', timeout=5000)
        body = page.locator('#schema').inner_text()
        self.assertIn('visits', body)
        self.assertIn('photos', body)
        self.assertIn('["store","visit"]', body)
        context.close()


if __name__ == '__main__':
    unittest.main()
