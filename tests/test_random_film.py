"""媒体库随机播放：只抽能播的、支持排除、空库报错。"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='random-test-'))

from movsp import library_store, storage


class RandomFilmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='random-data-')
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

    def _build_library(self, movie_count=5):
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='random-media-', ignore_cleanup_errors=True)))
        for i in range(1, movie_count + 1):
            folder = media / f'Movie {i} (202{i})'
            folder.mkdir(parents=True)
            (folder / f'movie{i}.mkv').write_bytes(b'x' * 512)
        library = library_store.add_library(
            f'库{os.urandom(4).hex()}', 'p', [{'path': str(media), 'recursive': True}])
        job = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (library_store.get_scan(job['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library

    def test_returns_a_playable_film(self):
        library = self._build_library(5)
        film = library_store.random_film(library['id'])
        self.assertIsNotNone(film, '库里有余量时应返回一部')
        self.assertEqual(film['kind'], 'movie')
        self.assertTrue(film.get('video_path'), '返回的必须是有视频的条目')

    def test_is_actually_random(self):
        """多次抽取应覆盖到不同影片（不是每次都第一条）。"""
        library = self._build_library(5)
        seen = {library_store.random_film(library['id'])['id'] for _ in range(20)}
        self.assertGreaterEqual(len(seen), 3, f'20 次只抽到 {len(seen)} 部，不像随机')

    def test_exclude_skips_given_film(self):
        library = self._build_library(3)
        first = library_store.random_film(library['id'])
        second = library_store.random_film(library['id'], exclude=first['id'])
        self.assertIsNotNone(second)
        self.assertNotEqual(second['id'], first['id'], 'exclude 应排除指定影片')

    def test_empty_library_returns_none(self):
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='random-empty-', ignore_cleanup_errors=True)))
        library = library_store.add_library(
            f'空库{os.urandom(4).hex()}', 'p', [{'path': str(media), 'recursive': True}])
        job = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (library_store.get_scan(job['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        self.assertIsNone(library_store.random_film(library['id']), '空库应返回 None')

    def test_respects_status_filter(self):
        """筛选条件要生效：只抽符合状态的影片。"""
        library = self._build_library(3)
        # 未刮削的影片状态是 idle；按 complete 筛应该抽不到
        film = library_store.random_film(library['id'], status='complete')
        self.assertIsNone(film, '按「资料完整」筛时，未刮削的影片不应被抽到')


if __name__ == '__main__':
    unittest.main()
