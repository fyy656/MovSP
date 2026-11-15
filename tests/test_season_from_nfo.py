"""季海报：季号只认 NFO，不从目录名推断。

用户明确要求：不要从文件夹名推季号，刮削时会写入 NFO。

覆盖两种真实目录结构：
  A. 总目录 + 季子目录（名姝S01-S03/名姝S01.…）
  B. 一季一个顶层目录（[为全人类 第1季].…）
两种都要优先取 seasonNN-poster.jpg，而不是一律用 poster.jpg。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='season-nfo-test-'))

from movsp import library_store, series_scan, storage

JPEG = b'\xff\xd8\xff' + bytes(120) + b'\xff\xd9'
SEASON_NFO = ('<?xml version="1.0" encoding="utf-8"?>'
              '<season><seasonnumber>%d</seasonnumber></season>')


class SeasonFromNfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='season-nfo-data-')
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

    def _media(self, prefix):
        return Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)))

    def _scan(self, media):
        library = library_store.add_library(
            f'库{os.urandom(4).hex()}', 'p',
            [{'path': str(media), 'recursive': True}], kind='series')
        job = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (library_store.get_scan(job['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library, [f for f in library_store.list_films(library_id=library['id'])['items']
                         if f['kind'] == 'series']

    def test_season_number_read_from_nfo(self):
        """season.nfo 里的 seasonnumber 才是季号依据。"""
        d = self._media('nfo-read-')
        (d / 'season.nfo').write_text(SEASON_NFO % 3, encoding='utf-8')
        self.assertEqual(series_scan._season_number_from_nfo(d), 3)

    def test_ambiguous_dir_name_does_not_decide_season(self):
        """目录名里的数字不能当季号：名姝S02.… 若 NFO 说第 1 季，以 NFO 为准。"""
        d = self._media('nfo-wins-')
        (d / 'season02-poster.jpg').write_bytes(JPEG)
        (d / 'season01-poster.jpg').write_bytes(JPEG + b'one')
        (d / 'season.nfo').write_text(SEASON_NFO % 1, encoding='utf-8')

        # 目录名自称 S02，但 NFO 说 1 —— _season_number_from_nfo 应返回 1
        self.assertEqual(series_scan._season_number_from_nfo(d), 1)
        # find_season_poster 按传入季号找，不猜
        self.assertTrue(series_scan.find_season_poster(d, 1))
        self.assertTrue(series_scan.find_season_poster(d, 2))

    def test_one_season_per_dir_prefers_season_poster(self):
        """一季一顶层目录：用户填了季号后，有 poster.jpg 也要优先用 seasonNN-poster.jpg。

        季号只认用户输入（数据库），season.nfo 不作数。
        """
        media = self._media('season-per-dir-')
        for season, year in ((1, 2019), (2, 2021)):
            folder = media / f'[某剧 第{season}季].Show.{year}.S{season:02d}'
            folder.mkdir(parents=True)
            (folder / f'S{season:02d}E01.mkv').write_bytes(b'x' * 1024)
            (folder / f'season{season:02d}-poster.jpg').write_bytes(JPEG + bytes([season]))
            # 剧集级同名海报，用来验证不会被误用
            (folder / 'poster.jpg').write_bytes(JPEG + b'show-level')

        _, shows = self._scan(media)
        self.assertEqual(len(shows), 2, '一季一个目录 → 两个剧集条目')
        # 模拟用户在资料页给每一部填季号
        for show, season in zip(sorted(shows, key=lambda s: s['dir_path']), (1, 2)):
            library_store.update_film(show['id'], {
                'title': show['title'], 'tmdb_id': '1', 'imdb_id': '', 'write_nfo': False,
                'actress': [], 'runtime': 0, 'season_number': season})

        covers = set()
        for show in shows:
            covered = library_store._series_cover(
                Path(show['dir_path']), {},
                library_store._saved_season_number(show['id']))
            self.assertRegex(Path(covered).name, r'season0\d-poster\.jpg',
                             '填了季号必须用季海报，不能用剧集级 poster.jpg')
            covers.add(Path(covered).name)
        self.assertEqual(len(covers), 2, '两季应各用各的季海报')

    def test_show_dir_with_season_subdirs(self):
        """总目录 + 季子目录：每个季子目录按自己的 NFO 取季海报。"""
        media = self._media('season-subdirs-')
        show_dir = media / '某剧S01-S02.Show.2017-2019'
        show_dir.mkdir(parents=True)
        (show_dir / 'poster.jpg').write_bytes(JPEG + b'show')
        for season in (1, 2):
            sub = show_dir / f'某剧S{season:02d}.Show.201{6 + season}'
            sub.mkdir()
            (sub / f'S{season:02d}E01.mkv').write_bytes(b'x' * 1024)
            (sub / 'season.nfo').write_text(SEASON_NFO % season, encoding='utf-8')
            (sub / f'season{season:02d}-poster.jpg').write_bytes(JPEG + bytes([season]))

        _, shows = self._scan(media)
        self.assertTrue(shows)
        # 每个条目的 _season_dir 应能按 NFO 定位到对应季目录
        for show in shows:
            season_no = series_scan._season_number_from_nfo(Path(show['dir_path']))
            if season_no:
                located = library_store._season_dir(show, season_no)
                self.assertTrue(
                    (located / f'season{season_no:02d}-poster.jpg').is_file(),
                    f'第 {season_no} 季应能定位到含季海报的目录')

    def test_find_season_poster_naming_variants(self):
        """补零/不补零、大小写、纯 seasonNN.jpg 都要认。"""
        d = self._media('season-names-')
        cases = [('season01-poster.jpg', 1), ('season2-poster.jpg', 2),
                 ('Season 3-poster.jpg', 3), ('season04.jpg', 4)]
        for name, season in cases:
            target = self._media(f'one-{season}-')
            (target / name).write_bytes(JPEG)
            self.assertTrue(series_scan.find_season_poster(target, season),
                            f'{name} 应被识别为第 {season} 季海报')

    def test_no_season_nfo_falls_back_to_poster(self):
        """没有 season.nfo 时退回剧集级海报，不因缺文件而空白。"""
        media = self._media('season-fallback-')
        folder = media / '某剧 (2019)'
        folder.mkdir(parents=True)
        (folder / 'S01E01.mkv').write_bytes(b'x' * 1024)
        (folder / 'poster.jpg').write_bytes(JPEG)

        _, shows = self._scan(media)
        self.assertEqual(len(shows), 1)
        self.assertTrue(shows[0].get('cover_path'), '应退回 poster.jpg')


if __name__ == '__main__':
    unittest.main()
