"""季海报：季号只认用户输入，不从目录名或 season.nfo 推断。

用户明确要求：「不需要从文件夹名称做任何季序号的推定」，
季号以刮削时填的值（数据库 films.season_number）为准。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='season-user-test-'))

from movsp import library_store, series_scan, storage

JPEG = b'\xff\xd8\xff' + bytes(120) + b'\xff\xd9'

TVSHOW_NFO = '''<?xml version="1.0" encoding="UTF-8"?>
<tvshow>
  <title>名姝</title>
  <thumb aspect="poster">https://image.tmdb.org/t/p/original/show.jpg</thumb>
  <thumb aspect="poster" season="1" type="season">https://image.tmdb.org/t/p/original/s1.jpg</thumb>
  <thumb aspect="poster" season="2" type="season">https://image.tmdb.org/t/p/original/s2.jpg</thumb>
</tvshow>'''


class UserSeasonOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='season-user-data-')
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

    def test_saved_season_number_round_trip(self):
        """数据库里存的季号能被读回（这是唯一的季号来源）。"""
        media = self._media('season-rt-')
        folder = media / '某剧 (2019)'
        folder.mkdir(parents=True)
        (folder / 'S01E01.mkv').write_bytes(b'x' * 1024)

        _, shows = self._scan(media)
        self.assertEqual(len(shows), 1)
        film_id = shows[0]['id']
        self.assertEqual(library_store._saved_season_number(film_id), 0)

        library_store.update_film(film_id, {
            'title': '某剧', 'tmdb_id': '1', 'imdb_id': '', 'write_nfo': False,
            'actress': [], 'runtime': 0, 'season_number': 2})
        self.assertEqual(library_store._saved_season_number(film_id), 2)

    def test_cover_uses_season_poster_for_saved_season(self):
        """填了季号且目录里有对应季海报 → 用它当封面。"""
        media = self._media('season-cover-')
        folder = media / '某剧 (2019)'
        folder.mkdir(parents=True)
        (folder / 'S01E01.mkv').write_bytes(b'x' * 1024)
        (folder / 'poster.jpg').write_bytes(JPEG + b'show')
        for season in (1, 2):
            (folder / f'season{season:02d}-poster.jpg').write_bytes(JPEG + bytes([season]))

        library, shows = self._scan(media)
        film_id = shows[0]['id']
        library_store.update_film(film_id, {
            'title': '某剧', 'tmdb_id': '1', 'imdb_id': '', 'write_nfo': False,
            'actress': [], 'runtime': 0, 'season_number': 2})

        cover = library_store._series_cover(folder, {}, 2)
        self.assertEqual(Path(cover).name, 'season02-poster.jpg',
                         '填了季号就该用该季海报，而不是 poster.jpg')

    def test_ambiguous_season_posters_are_not_guessed(self):
        """没填季号且目录里有多个季海报 → 不猜，回退到剧集级海报。"""
        media = self._media('season-amb-')
        folder = media / '某剧 (2019)'
        folder.mkdir(parents=True)
        (folder / 'S01E01.mkv').write_bytes(b'x' * 1024)
        (folder / 'poster.jpg').write_bytes(JPEG + b'show')
        for season in (1, 2, 3):
            (folder / f'season{season:02d}-poster.jpg').write_bytes(JPEG + bytes([season]))

        cover = library_store._series_cover(folder, {}, 0)
        self.assertEqual(Path(cover).name, 'poster.jpg',
                         '没填季号且有多个季海报时不能瞎猜')

    def test_single_season_poster_is_safe_to_use(self):
        """没填季号但只有唯一一个季海报 → 用它（不存在猜错）。"""
        media = self._media('season-only-')
        folder = media / '某剧 (2019)'
        folder.mkdir(parents=True)
        (folder / 'S01E01.mkv').write_bytes(b'x' * 1024)
        (folder / 'season02-poster.jpg').write_bytes(JPEG)

        cover = library_store._series_cover(folder, {}, 0)
        self.assertEqual(Path(cover).name, 'season02-poster.jpg')

    def test_dir_name_numbers_are_not_used_as_season(self):
        """目录名里的数字（名姝S02.Harlots.2018）不能被当季号。"""
        media = self._media('season-dirname-')
        folder = media / '名姝S02.Harlots.2018.1080p'
        folder.mkdir(parents=True)
        (folder / 'E01.mkv').write_bytes(b'x' * 1024)
        (folder / 'poster.jpg').write_bytes(JPEG + b'show')
        (folder / 'season02-poster.jpg').write_bytes(JPEG)

        # 没填季号：不能被目录名里的 S02 带偏，应回退 poster.jpg
        cover = library_store._series_cover(folder, {}, 0)
        self.assertEqual(Path(cover).name, 'poster.jpg',
                         '目录名里的季号不可信，未填季号时不应据此选图')

    def test_tvshow_nfo_season_posters_parsed(self):
        """tMM 写的 <thumb season=N type=season> 要能解析出季海报地址。"""
        d = self._media('season-nfo-')
        (d / 'tvshow.nfo').write_text(TVSHOW_NFO, encoding='utf-8')
        meta = series_scan.parse_tvshow_nfo(d / 'tvshow.nfo')
        posters = meta.get('season_posters') or {}
        self.assertEqual(sorted(posters), ['1', '2'])
        self.assertIn('s1.jpg', posters['1'])
        # 剧集海报不能被季海报顶掉
        self.assertIn('show.jpg', meta.get('poster_path') or '')

    def test_season_poster_url_from_nfo_helper(self):
        """按季号从 tvshow.nfo 取对应季海报 URL。"""
        d = self._media('season-url-')
        (d / 'tvshow.nfo').write_text(TVSHOW_NFO, encoding='utf-8')
        film = {'dir_path': str(d)}
        self.assertIn('s2.jpg', library_store._season_poster_url_from_nfo(film, 2))
        self.assertEqual(library_store._season_poster_url_from_nfo(film, 9), '')


if __name__ == '__main__':
    unittest.main()
