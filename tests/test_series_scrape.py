"""阶段 4：剧集刮削（标题路径 + tvshow.nfo + kind 不被降级）。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('MOVSP_DATA_DIR', str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-series'))

from movsp import storage

with patch('threading.Thread.start'):
    from movsp import server
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
    from movsp import tasks as task_runner
    queued: list[dict] = []
    case.enterContext(patch.object(task_runner, '_enqueue_task', lambda task: queued.append(task)))
    case.queued_tasks = queued
    return root


class SeriesScrapeTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'series-scrape-')
        self.root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='series-media-', ignore_cleanup_errors=True)))
        scrapers.save_source('fake_tv', (
            '__provides__ = ["tv_by_title"]\n'
            'def parse_data(movie):\n'
            '    assert getattr(movie, "_query_kind", "") == "series"\n'
            '    movie.title = "某剧"\n'
            '    movie.tmdb_id = "999"\n'
            '    movie.imdb_id = "tt1234567"\n'
            '    movie.plot = "剧情"\n'
            '    movie.publish_date = "2023-01-15"\n'
        ))
        scrapers.set_enabled('fake_tv', True)
        storage.save_preset({
            'id': 'tvpreset', 'name': '影视', 'mode': 'form',
            'form': {'scanner': {'minimum_size': 0},
                     'crawler': {'scrapers': ['fake_tv']}},
            'task_concurrency': 1,
        })

    def _make_series(self, library_name='剧集库', folder='某剧 (2023)'):
        media = self.root / library_name
        series_dir = media / folder
        series_dir.mkdir(parents=True, exist_ok=True)
        (series_dir / 'S01E01.mkv').write_bytes(b'x' * 512)
        (series_dir / 'S01E02.mkv').write_bytes(b'x' * 512)
        library = library_store.add_library(
            library_name, 'tvpreset', [{'path': str(media), 'recursive': True}], kind='series')
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        films = library_store.list_films(library_id=library['id'])['items']
        series = next(item for item in films if item['kind'] == 'series')
        return library, series, series_dir

    def _run_inline(self, task_id):
        from movsp import tasks as task_runner
        picked = None
        for index, task in enumerate(list(self.queued_tasks)):
            if task.get('id') == task_id:
                picked = task
                del self.queued_tasks[index]
                break
        if picked is not None:
            task_runner._run_task(picked)

    def _wait_task(self, task_id):
        from movsp.task_store import record as task_record
        self._run_inline(task_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            record = task_record(task_id, logs=False)
            if record and record.get('status') in ('succeeded', 'failed', 'needs_choice'):
                return record
            time.sleep(0.05)
        return task_record(task_id, logs=False)

    def test_series_scrape_writes_tvshow_nfo(self):
        library, series, series_dir = self._make_series()
        self.assertEqual(series['kind'], 'series')
        result = library_store.start_scrape(series['id'])
        record = self._wait_task(result['task_id'])
        self.assertEqual(record.get('status'), 'succeeded', record.get('error'))
        query = record.get('scrape_query') or {}
        self.assertEqual(query.get('kind'), 'series')
        # 标题路径：标题来自目录名，年份单独传递（标题里不再带年份）
        self.assertEqual(query.get('title'), '某剧')
        self.assertEqual(query.get('year'), 2023)
        nfo = series_dir / 'tvshow.nfo'
        self.assertTrue(nfo.is_file(), '应写入 tvshow.nfo')
        from movsp import series_scan
        meta = series_scan.parse_tvshow_nfo(nfo)
        self.assertEqual(meta.get('tmdb_id'), '999')
        self.assertEqual(meta.get('imdb_id'), 'tt1234567')

    def test_series_kind_survives_scrape(self):
        """剧集刮削后不能被降级成 movie（曾因 _reconcile 走电影刷新而丢 kind）。"""
        library, series, _ = self._make_series()
        result = library_store.start_scrape(series['id'])
        self._wait_task(result['task_id'])
        deadline = time.monotonic() + 10
        film = library_store.get_film(series['id'])
        while time.monotonic() < deadline and film.get('scrape_state') != 'idle':
            time.sleep(0.05)
            film = library_store.get_film(series['id'])
        self.assertEqual(film['kind'], 'series', 'kind 必须保持 series')
        self.assertEqual(film['title'], '某剧')
        self.assertEqual(film['tmdb_id'], '999')
        self.assertEqual(film['imdb_id'], 'tt1234567')
        self.assertEqual(film['scrape_state'], 'idle')

    def test_series_rejects_jav_preset(self):
        """剧集不能按番号刮削：预设只有 JAV 刮削器时应给出清晰错误。"""
        storage.save_preset({'id': 'javonly', 'name': 'JAV', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0},
                                      'crawler': {'scrapers': ['javbus']}},
                             'task_concurrency': 1})
        scrapers.set_enabled('javbus', True)
        library, series, _ = self._make_series('JAV剧集', '某剧2 (2023)')
        library_store.update_library(library['id'], preset_id='javonly')
        with self.assertRaises(ValueError) as ctx:
            library_store.start_scrape(series['id'])
        self.assertIn('剧集', str(ctx.exception))

    def test_series_rescan_keeps_external_ids(self):
        """重新扫描后仍能从 tvshow.nfo 读回外部 ID。"""
        library, series, series_dir = self._make_series()
        result = library_store.start_scrape(series['id'])
        self._wait_task(result['task_id'])
        library_store.refresh_series(series['id'])
        film = library_store.get_film(series['id'])
        self.assertEqual(film['tmdb_id'], '999')

    def test_series_cover_uses_poster_name(self):
        """剧集：竖版写 poster.jpg、横版写 fanart.jpg（Emby/Jellyfin 认这两个名字）。"""
        film = {'kind': 'series', 'dir_path': str(self.root / 'tmpdir'), 'video_path': ''}
        Path(film['dir_path']).mkdir(parents=True, exist_ok=True)
        # 没有网络时不下载，只验证目标文件名规则
        poster = Path(film['dir_path']) / 'poster.jpg'
        poster.write_bytes(b'x')
        fanart = Path(film['dir_path']) / 'fanart.jpg'
        fanart.write_bytes(b'y')
        self.assertEqual(library_store._download_artwork(film, 'https://x/a.jpg', 'cover'),
                         str(poster))
        self.assertEqual(library_store._download_artwork(film, 'https://x/b.jpg', 'fanart'),
                         str(fanart))

    def test_movie_artwork_uses_emby_naming(self):
        """电影：竖版写 {stem}.jpg、横版写 {stem}-fanart.jpg，不用有歧义的 -poster。"""
        folder = Path(self.root / 'moviedir')
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'ABC-123.mp4').write_bytes(b'x')
        film = {'kind': 'movie', 'dir_path': str(folder), 'video_path': str(folder / 'ABC-123.mp4')}
        cover = folder / 'ABC-123.jpg'
        cover.write_bytes(b'v')
        fanart = folder / 'ABC-123-fanart.jpg'
        fanart.write_bytes(b'h')
        self.assertEqual(library_store._download_artwork(film, 'https://x/a.jpg', 'cover'),
                         str(cover))
        self.assertEqual(library_store._download_artwork(film, 'https://x/b.jpg', 'fanart'),
                         str(fanart))


class SeriesQueryTests(unittest.TestCase):
    def test_detect_series_query_strips_year(self):
        from movsp import scrape_plan
        info = scrape_plan.detect_series_query(series_dir=Path('三体 (2023)'))
        self.assertEqual(info['kind'], 'series')
        self.assertEqual(info['title'], '三体')
        self.assertEqual(info['year'], 2023)

    def test_detect_series_query_strips_season_marker(self):
        """剧集目录名带 S01 / 第2季 / Season 2，搜索时要清掉。"""
        from movsp import scrape_plan
        cases = {
            '老舅.Uncle.2025.S01.2160p.WEB-DL.HEVC.DDP2.0-ZmWeb': '老舅 Uncle',
            '繁花.S01.2023.2160p': '繁花',
            '某剧 第2季': '某剧',
            '庆余年 Season 2': '庆余年',
        }
        for raw, expected in cases.items():
            info = scrape_plan.detect_series_query(series_dir=Path(raw))
            self.assertEqual(info['title'], expected, raw)

    def test_series_query_uses_series_dir_not_parent(self):
        """目录名里带点（DDP2.0-ZmWeb）不能被当成文件扩展名，否则会取到上级目录。"""
        from movsp import tasks
        import tempfile
        root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='series-q-', ignore_cleanup_errors=True)))
        series_dir = root / 'tv' / '老舅.Uncle.2025.S01.2160p.WEB-DL.HEVC.DDP2.0-ZmWeb'
        series_dir.mkdir(parents=True)
        (series_dir / 'ep.mkv').write_bytes(b'x')
        query = tasks._query_for_task(str(series_dir), None, None, None, True, None)
        self.assertEqual(query['title'], '老舅 Uncle')
        self.assertEqual(query['year'], 2025)
        self.assertEqual(query['kind'], 'series')

    def test_task_name_uses_series_dir(self):
        """剧集任务的显示名用目录名，不能用「01.mkv」这种集号。"""
        from movsp.tasks import _task_name
        import tempfile
        root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='taskname-', ignore_cleanup_errors=True)))
        series_dir = root / '03.爱情公寓3 4K (2012)'
        series_dir.mkdir(parents=True)
        (series_dir / '01.mkv').write_bytes(b'x')
        (series_dir / '02.mkv').write_bytes(b'x')

        # 剧集：用目录名，而不是第一个视频名
        self.assertEqual(_task_name(str(series_dir), prefer_dir=True),
                         '03.爱情公寓3 4K (2012)')
        # 旧行为会取第一个视频名（这就是显示成「01」的原因）
        self.assertEqual(_task_name(str(series_dir)), '01')

        # 电影仍取视频文件名（含番号/片名）
        movie_dir = root / 'movies' / 'Some.Movie.2019'
        movie_dir.mkdir(parents=True)
        (movie_dir / 'Some.Movie.2019.mkv').write_bytes(b'x')
        self.assertEqual(_task_name(str(movie_dir)), 'Some.Movie.2019')

    def test_required_keys_carried_and_validated(self):
        """预设勾了「封面」就必须真的拿到，否则任务要报出来。"""
        from movsp import tasks
        import tempfile
        root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='required-', ignore_cleanup_errors=True)))
        series_dir = root / '某剧.S01'
        series_dir.mkdir(parents=True)
        (series_dir / 'ep.mkv').write_bytes(b'x' * 512)

        task = {'required_keys': ['cover', 'title']}
        # 封面没下载成功：即使标题有了，也要报缺封面
        self.assertEqual(
            tasks._missing_required_keys(task, {'title': '某剧'}, {'cover_done': 0}),
            ['cover'])
        # 封面下载成功：全部满足
        self.assertEqual(
            tasks._missing_required_keys(task, {'title': '某剧'}, {'cover_done': 1}), [])
        # 标题也缺：两个都报
        self.assertEqual(
            tasks._missing_required_keys(task, {}, {'cover_done': 0}),
            ['cover', 'title'])
        # 预设没勾任何要求：不报
        self.assertEqual(tasks._missing_required_keys({}, {}, {}), [])
        # 高清封面单独判断
        self.assertEqual(
            tasks._missing_required_keys({'required_keys': ['big_cover']}, {}, {'fanart_done': 0}),
            ['big_cover'])

    def test_season_number_passed_into_query(self):
        """季编号必须进入查询：TMDb 一个剧 ID 含全部季，不指定只会取第 1 季。"""
        from movsp import tasks
        import tempfile
        root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='season-q-', ignore_cleanup_errors=True)))
        series_dir = root / 'tv' / '爱情公寓5'
        series_dir.mkdir(parents=True)
        (series_dir / 'ep.mkv').write_bytes(b'x')
        query = tasks._query_for_task(str(series_dir), None, None, None, True,
                                      '爱情公寓', 2020, 5)
        self.assertEqual(query['season'], 5, '应带上季号')
        self.assertEqual(query['title'], '爱情公寓')

        # 不填季号时为 0（表示未指定），走剧级信息
        no_season = tasks._query_for_task(str(series_dir), None, None, None, True,
                                          '爱情公寓', 2020, None)
        self.assertEqual(no_season.get('season'), 0, '未指定季时不应带上季号')

    def test_series_nfo_title_wins(self):
        from movsp import scrape_plan
        info = scrape_plan.detect_series_query(
            nfo={'title': 'NFO 剧名', 'premiered': '2021-05-01'}, series_dir=Path('某目录'))
        self.assertEqual(info['title'], 'NFO 剧名')
        self.assertEqual(info['title_source'], 'nfo')
        self.assertEqual(info['year'], 2021)

    def test_series_external_id_passthrough(self):
        from movsp import scrape_plan
        info = scrape_plan.detect_series_query(
            series_dir=Path('某剧'), external_id={'tmdb': '204541'})
        self.assertEqual(info['external_id'], {'tmdb': '204541'})


if __name__ == '__main__':
    unittest.main()
