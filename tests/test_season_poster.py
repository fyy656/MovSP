"""剧集季海报：落盘位置、命名与识别。

历史问题：TMDb 每季都有独立海报，但 apply_season_result 只写分集 NFO 和剧照，
季海报从没落盘，结果所有季在媒体库里共用剧集封面，看起来一模一样。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='season-poster-test-'))

from movsp import library_store, series_scan, storage

JPEG = b'\xff\xd8\xff' + bytes(400) + b'\xff\xd9'


class SeasonPosterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='season-data-')
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

    def _scan(self, media, kind='series'):
        library = library_store.add_library(
            f'库{os.urandom(4).hex()}', 'p',
            [{'path': str(media), 'recursive': True}], kind=kind)
        job = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (library_store.get_scan(job['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library

    def _media(self, prefix):
        return Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)))

    def test_season_dirs_written_when_multi_season(self):
        """Season 子目录结构：海报写进对应季目录的 poster.jpg。"""
        media = self._media('season-multi-')
        show = media / '名姝 (2017)'
        show.mkdir(parents=True)
        for season in (1, 2, 3):
            d = show / f'Season {season}'
            d.mkdir()
            (d / f'S{season:02d}E01.mkv').write_bytes(b'x' * 1024)

        library = self._scan(media)
        series = next(f for f in library_store.list_films(library_id=library['id'])['items']
                      if f['kind'] == 'series')

        # 各季目录应能定位到不同的子目录
        dirs = [library_store._season_dir(series, s) for s in (1, 2, 3)]
        self.assertEqual(len({str(d) for d in dirs}), 3, '三季应落在三个不同目录')
        for season in (1, 2, 3):
            self.assertIn(f'Season {season}', str(library_store._season_dir(series, season)))

    def test_single_dir_uses_season_prefixed_name(self):
        """一季一个顶层目录时，季目录就是剧集目录，应改用 seasonNN-poster.jpg。"""
        media = self._media('season-single-')
        show = media / '某剧 (2020)'
        show.mkdir(parents=True)
        for season in (1, 2):
            (show / f'S{season:02d}E01.mkv').write_bytes(b'x' * 1024)

        library = self._scan(media)
        series = next(f for f in library_store.list_films(library_id=library['id'])['items']
                      if f['kind'] == 'series')
        base = Path(series['dir_path'])
        # 单目录多季：_season_dir 两季都指向剧集根
        self.assertEqual(library_store._season_dir(series, 1), base)
        self.assertEqual(library_store._season_dir(series, 2), base)
        # 所以落盘要用带季号的名字，避免两季互相覆盖
        expected = base / 'season02-poster.jpg'
        self.assertEqual(expected.name, 'season02-poster.jpg')

    def test_series_cover_falls_back_to_season_poster(self):
        """多季分目录时剧集根目录没有海报，应回退用第一季的季海报做封面。"""
        media = self._media('season-fallback-')
        show = media / '无根海报剧 (2019)'
        show.mkdir(parents=True)
        for season in (1, 2):
            d = show / f'Season {season}'
            d.mkdir()
            (d / f'S{season:02d}E01.mkv').write_bytes(b'x' * 1024)
        # 只在季目录放海报，根目录不放
        (show / 'Season 1' / 'poster.jpg').write_bytes(JPEG)

        library = self._scan(media)
        series = next(f for f in library_store.list_films(library_id=library['id'])['items']
                      if f['kind'] == 'series')
        self.assertTrue(series.get('cover_path'), '应回退到季海报作为剧集封面')
        self.assertIn('Season 1', series['cover_path'])

    def test_root_poster_wins_over_season(self):
        """根目录有海报时优先用它，不要被子目录的季海报顶掉。"""
        media = self._media('season-root-')
        show = media / '有根海报剧 (2018)'
        show.mkdir(parents=True)
        (show / 'poster.jpg').write_bytes(JPEG + b'root')
        d = show / 'Season 1'
        d.mkdir()
        (d / 'S01E01.mkv').write_bytes(b'x' * 1024)
        (d / 'poster.jpg').write_bytes(JPEG + b'season')

        library = self._scan(media)
        series = next(f for f in library_store.list_films(library_id=library['id'])['items']
                      if f['kind'] == 'series')
        self.assertTrue(series.get('cover_path'))
        self.assertEqual(Path(series['cover_path']).parent, show,
                         '应使用根目录的海报')

    def test_find_poster_sees_season_dir_poster(self):
        """find_poster 本身能识别季目录里的 poster.jpg。"""
        d = self._media('season-find-')
        (d / 'poster.jpg').write_bytes(JPEG)
        self.assertTrue(series_scan.find_poster(d))


if __name__ == '__main__':
    unittest.main()
