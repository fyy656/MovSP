"""剧集分集刮削：整季写入每集 NFO + season.nfo。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('MOVSP_DATA_DIR',
                      str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-eps'))

from movsp import storage

with patch('threading.Thread.start'):
    from movsp import server
from fastapi.testclient import TestClient

from movsp import library_store, scrapers


def _isolate(case, prefix):
    directory = case.enterContext(
        tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True))
    root = Path(directory)
    for module in (storage, library_store):
        previous = module.DATA_DIR
        for name, value in list(vars(module).items()):
            if isinstance(value, Path) and value.is_relative_to(previous):
                case.enterContext(patch.object(module, name, root / value.relative_to(previous)))
    storage.ensure_seed_data()
    library_store._reset_ready()
    case.addCleanup(library_store._reset_ready)
    return root


class SeasonScrapeTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'season-')
        self.media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='season-media-', ignore_cleanup_errors=True)))
        storage.save_preset({'id': 'tv', 'name': '影视', 'kind': 'media', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0},
                                      'crawler': {'scrapers': ['tmdb']}},
                             'task_concurrency': 1})

    def _make_series(self, name='三体.S01.2160p.WEB-DL', episodes=(1, 2, 3)):
        folder = self.media / name
        folder.mkdir(parents=True)
        for number in episodes:
            (folder / f'三体.S01E{number:02d}.mkv').write_bytes(b'x' * 512)
        library = library_store.add_library('剧集库', 'tv',
                                            [{'path': str(self.media), 'recursive': True}],
                                            kind='series')
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        films = library_store.list_films(library_id=library['id'])['items']
        series = next(item for item in films if item['kind'] == 'series')
        return library, series, folder

    def _season_data(self):
        return {
            'name': '第一季', 'overview': '季简介', 'poster': '',
            'episodes': [
                {'episode': 1, 'title': '第一集', 'plot': '剧情一',
                 'air_date': '2023-01-15', 'runtime': 43, 'still': ''},
                {'episode': 2, 'title': '第二集', 'plot': '剧情二',
                 'air_date': '2023-01-16', 'runtime': 44, 'still': ''},
                {'episode': 99, 'title': '不存在的集', 'plot': '',
                 'air_date': '', 'runtime': 0, 'still': ''},
            ],
        }

    def test_writes_episode_nfo_per_episode(self):
        """整季资料按集号写进各集 NFO。"""
        library, series, folder = self._make_series()
        result = library_store.apply_season_result(series['id'], 1, self._season_data())
        self.assertEqual(result['updated'], [1, 2])
        self.assertIn(99, result['missing'])
        first = folder / '三体.S01E01.nfo'
        self.assertTrue(first.is_file(), '应写入第 1 集 NFO')
        content = first.read_text(encoding='utf-8')
        self.assertIn('<episodedetails>', content)
        self.assertIn('第一集', content)
        self.assertIn('2023-01-15', content)
        self.assertIn('<season>1</season>', content)
        self.assertIn('<episode>1</episode>', content)

    def test_writes_season_nfo(self):
        """季级资料写 season.nfo（扁平结构下放在剧集目录）。"""
        library, series, folder = self._make_series()
        result = library_store.apply_season_result(series['id'], 1, self._season_data())
        self.assertTrue(result['season_nfo'])
        season_file = folder / 'season.nfo'
        self.assertTrue(season_file.is_file())
        content = season_file.read_text(encoding='utf-8')
        self.assertIn('<season>', content)
        self.assertIn('<seasonnumber>1</seasonnumber>', content)
        self.assertIn('第一季', content)

    def test_episode_index_gets_metadata(self):
        """写入后分集索引要能读到标题与播出日期。"""
        library, series, folder = self._make_series()
        library_store.apply_season_result(series['id'], 1, self._season_data())
        episodes = library_store.list_films(
            library_id=library['id'], kind='episode', parent=series['id'])['items']
        by_number = {item['episode']: item for item in episodes}
        self.assertEqual(by_number[1]['title'], '第一集')
        self.assertEqual(by_number[1]['release_date'], '2023-01-15')
        self.assertEqual(by_number[1]['runtime'], 43)

    def test_unknown_episodes_reported_not_failed(self):
        """TMDb 有但磁盘没有的集号只报告，不抛异常。"""
        library, series, folder = self._make_series(episodes=(1,))
        result = library_store.apply_season_result(series['id'], 1, self._season_data())
        self.assertEqual(result['updated'], [1])
        self.assertIn(2, result['missing'])
        self.assertIn(99, result['missing'])

    def test_empty_episodes_raises(self):
        library, series, folder = self._make_series()
        with self.assertRaises(ValueError):
            library_store.apply_season_result(series['id'], 1, {'episodes': []})

    def test_non_series_rejected(self):
        """电影条目不能调用分集刮削。"""
        movies = self.media / 'movies'
        movies.mkdir(exist_ok=True)
        (movies / 'Some.Movie.2019.mkv').write_bytes(b'x' * 512)
        library = library_store.add_library('电影', 'tv',
                                            [{'path': str(movies), 'recursive': True}])
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        film = library_store.list_films(library_id=library['id'])['items'][0]
        with self.assertRaises(ValueError):
            library_store.apply_season_result(film['id'], 1, self._season_data())


class SeasonScrapeApiTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'season-api-')
        self.client = self.enterContext(TestClient(server.app))
        previous = dict(server.app.dependency_overrides)
        self.addCleanup(lambda: (server.app.dependency_overrides.clear(),
                                 server.app.dependency_overrides.update(previous)))
        server.app.dependency_overrides[server.current_user] = lambda: {'username': 'admin', 'role': 'admin'}
        server.app.dependency_overrides[server.require_admin] = lambda: {'username': 'admin', 'role': 'admin'}

    def test_requires_tmdb_id(self):
        """没有 TMDb ID 时给出清晰提示，而不是报错崩溃。"""
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='season-api-media-', ignore_cleanup_errors=True)))
        folder = media / '某剧.S01'
        folder.mkdir(parents=True)
        (folder / '某剧.S01E01.mkv').write_bytes(b'x')
        storage.save_preset({'id': 'tv', 'name': '影视', 'kind': 'media', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0},
                                      'crawler': {'scrapers': ['tmdb']}},
                             'task_concurrency': 1})
        library = library_store.add_library('剧集库', 'tv',
                                            [{'path': str(media), 'recursive': True}], kind='series')
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        series = next(item for item in library_store.list_films(library_id=library['id'])['items']
                      if item['kind'] == 'series')
        response = self.client.post(f'/api/library/series/{series["id"]}/season-scrape',
                                    json={'season': 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn('TMDb ID', response.json()['detail'])

    def test_rejects_non_series(self):
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='season-movie-', ignore_cleanup_errors=True)))
        folder = media / '某电影 (2019)'
        folder.mkdir(parents=True)
        (folder / 'Some.Movie.2019.mkv').write_bytes(b'x' * 512)
        storage.save_preset({'id': 'mv', 'name': '影视', 'kind': 'media', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0},
                                      'crawler': {'scrapers': ['tmdb']}},
                             'task_concurrency': 1})
        library = library_store.add_library('电影', 'mv',
                                            [{'path': str(media), 'recursive': True}])
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        film = library_store.list_films(library_id=library['id'])['items'][0]
        response = self.client.post(f'/api/library/series/{film["id"]}/season-scrape',
                                    json={'season': 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn('剧集', response.json()['detail'])


if __name__ == '__main__':
    unittest.main()
