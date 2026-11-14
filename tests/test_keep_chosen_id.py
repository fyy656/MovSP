"""候选选定的外部 ID 不能被「保存资料」覆盖回旧值。"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='keepid-'))

from movsp import library_store, storage


class KeepChosenIdTests(unittest.TestCase):
    """「保存资料」不改外部 ID；只有显式提交才覆盖。

    踩过的坑：候选里改成动漫版后，输入框还留着旧的真人版 ID，
    用户再点「保存并刮削」就把选择覆盖回去了，看起来像「死活不改」。
    """

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='keepid-data-')
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
            tempfile.TemporaryDirectory(prefix='keepid-media-', ignore_cleanup_errors=True)))
        show = media / '某剧.S01'
        show.mkdir(parents=True)
        (show / 'E01.mkv').write_bytes(b'x' * 512)
        library = library_store.add_library(
            f'库{os.urandom(4).hex()}', 'p', [{'path': str(media), 'recursive': True}],
            kind='series')
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return next(f for f in library_store.list_films(library_id=library['id'])['items']
                    if f['kind'] == 'series')

    def test_omitting_id_keeps_current_value(self):
        """不提交 tmdb_id 时保留库里的值（候选刚选定的那一条）。"""
        series = self._make_series()
        library_store.update_film(series['id'], {
            'title': '某剧', 'original_title': '', 'tmdb_id': '106449', 'imdb_id': '',
            'write_nfo': False, 'actress': [], 'runtime': 0})
        self.assertEqual(
            library_store.get_film(series['id'], reconcile=False)['tmdb_id'], '106449')

        # 模拟「保存资料」不提交 ID
        library_store.update_film(series['id'], {
            'title': '某剧', 'original_title': '', 'write_nfo': False,
            'actress': [], 'runtime': 0})

        after = library_store.get_film(series['id'], reconcile=False)
        self.assertEqual(after['tmdb_id'], '106449', '未提交 ID 时不应被清空或覆盖')

    def test_explicit_empty_id_clears(self):
        """显式提交空字符串才清空（用户主动清掉 ID，要能重新按标题搜）。"""
        series = self._make_series()
        library_store.update_film(series['id'], {
            'title': '某剧', 'original_title': '', 'tmdb_id': '243224', 'imdb_id': '',
            'write_nfo': False, 'actress': [], 'runtime': 0})

        library_store.update_film(series['id'], {
            'title': '某剧', 'tmdb_id': '', 'imdb_id': '',
            'write_nfo': False, 'actress': [], 'runtime': 0})

        after = library_store.get_film(series['id'], reconcile=False)
        self.assertFalse(after.get('tmdb_id'), '显式传空应清空')

    def test_explicit_new_id_overrides(self):
        """显式提交新 ID 要生效（用户手填或换一部）。"""
        series = self._make_series()
        library_store.update_film(series['id'], {
            'title': '某剧', 'tmdb_id': '243224', 'imdb_id': '',
            'write_nfo': False, 'actress': [], 'runtime': 0})

        library_store.update_film(series['id'], {
            'title': '某剧', 'tmdb_id': '106449', 'imdb_id': '',
            'write_nfo': False, 'actress': [], 'runtime': 0})

        self.assertEqual(
            library_store.get_film(series['id'], reconcile=False)['tmdb_id'], '106449')


if __name__ == '__main__':
    unittest.main()
