import unittest
from offline_cache.browser import smoke


class BrowserTests(unittest.TestCase):
    def test_local_database_and_write(self):
        result = smoke()
        self.assertEqual(result['状态'], '走访已保存')
        self.assertEqual(result['databases'][0]['name'], '巡店资料')
