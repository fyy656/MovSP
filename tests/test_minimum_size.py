"""最小匹配文件大小：电影与剧集都按预设值生效。

历史上的坑：剧集路径曾被强行封顶到 1MiB，界面上填多少都几乎不起作用，
用户以为设置坏了。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='minsize-test-'))

from movsp import library_store, storage

MiB = 1024 * 1024


class MinimumSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='minsize-data-')
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
        storage.save_preset({
            'id': 'min50', 'name': 'min50', 'kind': 'media', 'mode': 'form',
            'form': {'scanner': {'minimum_size': '50MiB'}},
            'task_concurrency': 1})

    def _media(self, prefix):
        return Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)))

    def _scan(self, media, kind=None):
        kwargs = {'kind': kind} if kind else {}
        library = library_store.add_library(
            f'库{os.urandom(4).hex()}', 'min50',
            [{'path': str(media), 'recursive': True}], **kwargs)
        job = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            info = library_store.get_scan(job['id']) or {}
            if info.get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library, library_store.get_scan(job['id']) or {}

    def test_movie_below_threshold_is_skipped(self):
        media = self._media('min-movie-')
        small = media / 'Small Movie (2020)'
        small.mkdir(parents=True)
        (small / 'movie.mkv').write_bytes(b'x' * (30 * MiB))
        big = media / 'Big Movie (2021)'
        big.mkdir(parents=True)
        (big / 'movie.mkv').write_bytes(b'x' * (80 * MiB))

        library, info = self._scan(media)
        self.assertEqual(info.get('skipped'), 1, '30MB 应被跳过')
        items = library_store.list_films(library_id=library['id'])['items']
        self.assertEqual(len(items), 1, '只保留 80MB 那部')

    def test_series_below_threshold_is_skipped(self):
        """剧集按单集判断，且不再被 1MiB 封顶。"""
        media = self._media('min-series-')
        for name, size in (('小剧.S01', 30), ('大剧.S01', 80)):
            folder = media / name
            folder.mkdir(parents=True)
            (folder / 'E01.mkv').write_bytes(b'x' * (size * MiB))

        library, info = self._scan(media, kind='series')
        self.assertEqual(info.get('skipped'), 1, '30MB 单集应被跳过')
        episodes = library_store.list_films(
            library_id=library['id'], kind='episode')['items']
        self.assertEqual(len(episodes), 1, '只保留 80MB 那集')

    def test_series_is_not_capped_at_one_mib(self):
        """回归：剧集阈值必须与预设一致，不能被压到 1MiB。"""
        media = self._media('min-cap-')
        folder = media / '中剧.S01'
        folder.mkdir(parents=True)
        # 10MB 单集：大于旧封顶的 1MiB，但小于预设的 50MiB
        (folder / 'E01.mkv').write_bytes(b'x' * (10 * MiB))

        library, info = self._scan(media, kind='series')
        self.assertEqual(info.get('skipped'), 1,
                         '10MB 单集应被 50MiB 阈值过滤（旧实现会保留）')
        episodes = library_store.list_films(
            library_id=library['id'], kind='episode')['items']
        self.assertEqual(episodes, [], '不应有任何分集进库')


if __name__ == '__main__':
    unittest.main()
