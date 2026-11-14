"""多结果待确认：不自动落盘 + 超时撤销。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='choice-test-'))

from movsp import library_store, storage


class PendingChoiceTests(unittest.TestCase):
    """多结果时只挂状态，不把「第一条」当成用户的选择写进影片。"""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='choice-data-')
        # 记录原值，测试结束必须还原：DATA_DIR 被改过之后，
        # 其它模块（如 thumbnails.THUMBNAIL_DIR）的路径断言会失效。
        cls._originals = []
        for module in (storage, library_store):
            previous = module.DATA_DIR
            for name, value in list(vars(module).items()):
                if isinstance(value, Path) and value.is_relative_to(previous):
                    cls._originals.append((module, name, value))
                    setattr(module, name, Path(cls.root) / value.relative_to(previous))

    @classmethod
    def tearDownClass(cls):
        for module, name, value in cls._originals:
            setattr(module, name, value)
        cls._originals = []

    def setUp(self):
        storage.ensure_seed_data()
        library_store._reset_ready()
        self.addCleanup(library_store._reset_ready)
        storage.save_preset({'id': 'p', 'name': 't', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})

    def _make_series(self):
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='choice-media-', ignore_cleanup_errors=True)))
        show = media / '某剧.S01'
        show.mkdir(parents=True)
        (show / 'E01.mkv').write_bytes(b'x' * 512)
        library = library_store.add_library(
            f'库{os.urandom(4).hex()}', 'p', [{'path': str(media), 'recursive': True}],
            kind='series')
        scan = library_store.start_scan(library['id'])
        import time
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library, next(f for f in library_store.list_films(library_id=library['id'])['items']
                             if f['kind'] == 'series')

    def test_needs_choice_does_not_write_result(self):
        """待确认时不写入标题/ID/封面——用户可能一个都不选。"""
        library, series = self._make_series()
        title_before = series['title']

        task = {
            'id': 'fake-task-1',
            'scrape_path': 'title',
            'scraped_data': {'title': '错误的第一条', 'tmdb_id': '999999',
                             'cover': 'https://example.invalid/x.jpg'},
            'scrape_candidates': [{'tmdb_id': '1'}, {'tmdb_id': '2'}],
        }
        library_store.refresh_film_after_task(series['id'], 'needs_choice', '', task)

        after = library_store.get_film(series['id'], reconcile=False)
        self.assertEqual(after['scrape_state'], 'needs_choice')
        self.assertEqual(after['title'], title_before, '不应把默认结果写进标题')
        self.assertFalse(after.get('tmdb_id'), '不应写入 tmdb_id')
        self.assertFalse(after.get('cover_path'), '不应写入封面')

    def test_expired_choice_returns_to_idle(self):
        """超时未选择：撤销待确认，可重新刮削。"""
        library, series = self._make_series()
        with library_store.connection() as db:
            db.execute("UPDATE films SET scrape_state='needs_choice', updated_at=? WHERE id=?",
                       ('2020-01-01T00:00:00+08:00', series['id']))

        library_store.list_films(library_id=library['id'])  # 查询时触发清理

        after = library_store.get_film(series['id'], reconcile=False)
        self.assertEqual(after['scrape_state'], 'idle', '超时应回到 idle')
        self.assertIn('超时', after.get('last_error') or '', '应说明撤销原因')

    def test_fresh_choice_is_kept(self):
        """未超时的待确认不能被清理掉。"""
        from movsp.timeutils import now_iso
        library, series = self._make_series()
        with library_store.connection() as db:
            db.execute("UPDATE films SET scrape_state='needs_choice', updated_at=? WHERE id=?",
                       (now_iso(), series['id']))

        library_store.list_films(library_id=library['id'])
        after = library_store.get_film(series['id'], reconcile=False)
        self.assertEqual(after['scrape_state'], 'needs_choice', '新产生的待确认应保留')

    def test_ttl_is_reasonable(self):
        self.assertGreaterEqual(library_store.CHOICE_TTL_SECONDS, 600,
                                'TTL 太短会让用户来不及选择')
        self.assertLessEqual(library_store.CHOICE_TTL_SECONDS, 24 * 3600,
                             'TTL 太长会让状态长期挂着')


if __name__ == '__main__':
    unittest.main()
