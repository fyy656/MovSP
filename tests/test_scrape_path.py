"""阶段 1：刮削路径推导 + JAV 无番号跳过（D1）。"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

os.environ.setdefault('MOVSP_DATA_DIR', str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-paths'))

from movsp import storage

with patch('threading.Thread.start'):
    from movsp import server
from movsp import library_store, scrapers


def _fresh_dir(case, prefix):
    """建一个**保证唯一**的空目录。

    不用 TemporaryDirectory：Windows 上清理失败时它可能复用同名路径，
    导致上一个用例残留的文件被算进当前用例（曾出现 API-25 这种幽灵番号）。
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    case.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _isolate(case, prefix):
    """把 storage / library_store 的数据目录都指向临时目录。"""
    root = _fresh_dir(case, prefix)
    for module in (storage, library_store):
        previous = module.DATA_DIR
        for name, value in list(vars(module).items()):
            if isinstance(value, Path) and value.is_relative_to(previous):
                case.enterContext(patch.object(module, name, root / value.relative_to(previous)))
    storage.ensure_seed_data()
    library_store._reset_ready()
    case.addCleanup(library_store._reset_ready)
    # 不启动后台 worker：共享线程会在用例之间消费别人排队的任务，
    # 导致「本该被跳过」的用例看到任务被悄悄执行了。
    from movsp import tasks as task_runner
    queued: list[dict] = []

    def fake_enqueue(task):
        queued.append(task)

    case.enterContext(patch.object(task_runner, '_enqueue_task', fake_enqueue))
    case.queued_tasks = queued
    return root


class ScrapePathTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'scrape-path-')
        scrapers.set_enabled('tmdb', True)
        scrapers.set_enabled('local_json', True)
        scrapers.set_enabled('javbus', True)

    def preset(self, name, scrapers_list):
        storage.save_preset({'id': name, 'name': name, 'mode': 'form',
                             'form': {'crawler': {'scrapers': scrapers_list}},
                             'task_concurrency': 1})
        return name

    # ---- 路径推导 ----

    def test_jav_only_preset_resolves_to_jav(self):
        info = scrapers.resolve_path(self.preset('jav', ['javbus', 'javdb']))
        self.assertEqual(info['path'], 'jav')
        self.assertIn('javbus', info['scrapers'])
        self.assertEqual(info['warnings'], [])

    def test_title_only_preset_resolves_to_title(self):
        info = scrapers.resolve_path(self.preset('film', ['tmdb']))
        self.assertEqual(info['path'], 'title')

    def test_mixed_preset_resolves_to_hybrid(self):
        info = scrapers.resolve_path(self.preset('mixed', ['javbus', 'tmdb']))
        self.assertEqual(info['path'], 'hybrid')

    def test_empty_preset_resolves_to_none_with_warning(self):
        info = scrapers.resolve_path(self.preset('empty', []))
        self.assertEqual(info['path'], 'none')
        self.assertTrue(info['warnings'])

    def test_disabled_scrapers_do_not_affect_path(self):
        """停用的刮削器不算数：停掉 tmdb 后混合预设应退回 jav。"""
        name = self.preset('mixed2', ['javbus', 'tmdb'])
        self.assertEqual(scrapers.resolve_path(name)['path'], 'hybrid')
        scrapers.set_enabled('tmdb', False)
        self.assertEqual(scrapers.resolve_path(name)['path'], 'jav')

    def test_legacy_selection_format_still_resolves(self):
        storage.save_preset({'id': 'legacy', 'name': 'legacy', 'mode': 'form',
                             'form': {'crawler': {'selection': {'normal': ['javbus']}}},
                             'task_concurrency': 1})
        self.assertEqual(scrapers.resolve_path('legacy')['path'], 'jav')

    def test_missing_preset_falls_back_to_default(self):
        info = scrapers.resolve_path('does-not-exist')
        self.assertIn(info['path'], ('jav', 'title', 'hybrid', 'none'))


class LibraryScrapePathTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'lib-path-')
        self.root = _fresh_dir(self, 'media-')
        scrapers.set_enabled('tmdb', True)
        scrapers.set_enabled('javbus', True)
        # 测试用的假视频只有几个字节，把最小匹配大小调到 0
        tiny = {'scanner': {'minimum_size': 0}, 'crawler': {'scrapers': ['javbus']}}
        storage.save_preset({'id': 'javpreset', 'name': 'JAV', 'mode': 'form',
                             'form': tiny, 'task_concurrency': 1})
        storage.save_preset({'id': 'filmpreset', 'name': 'Film', 'mode': 'form',
                             'form': {**tiny, 'crawler': {'scrapers': ['tmdb']}}, 'task_concurrency': 1})

    def _new_folder(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _scan(self, library_id):
        scan = library_store.start_scan(library_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = library_store.get_scan(scan['id']) or {}
            if state.get('status') in ('succeeded', 'failed'):
                return state
            time.sleep(0.02)
        self.fail('扫描超时')

    def test_library_exposes_resolved_scrape_path(self):
        jav_dir = self._new_folder('jav')
        film_dir = self._new_folder('film')
        library = library_store.add_library('正片', 'javpreset', [{'path': jav_dir, 'recursive': True}])
        self.assertEqual(library['scrape_path']['path'], 'jav')
        film_lib = library_store.add_library('电影', 'filmpreset', [{'path': film_dir, 'recursive': True}])
        self.assertEqual(film_lib['scrape_path']['path'], 'title')

    def _make_film(self, library_id, filename, dvdid=''):
        library = library_store.get_library(library_id)
        folder = Path(library['folders'][0]['path'])
        Path(folder, filename).write_bytes(b'x')
        self._scan(library_id)
        films = library_store.list_films(library_id=library_id)['items']
        self.assertTrue(films, '扫描应产生至少一部影片')
        film = films[0]
        if dvdid:
            library_store.update_film(film['id'], {'dvdid': dvdid})
            film = library_store.get_film(film['id'])
        return film

    def test_jav_preset_without_code_is_skipped(self):
        """D1：JAV 预设 + 无番号 → 不创建任务，报可读错误。"""
        library = library_store.add_library('正片', 'javpreset',
                                            [{'path': self._new_folder('jav1'), 'recursive': True}])
        film = self._make_film(library['id'], 'unknown_movie.mkv')
        self.assertEqual(film['dvdid'], '')
        with self.assertRaises(ValueError) as ctx:
            library_store.start_scrape(film['id'])
        self.assertIn('番号', str(ctx.exception))
        # 不允许残留排队状态
        self.assertNotIn(library_store.get_film(film['id'])['scrape_state'], ('queued', 'running'))

    def test_jav_preset_with_code_still_queues(self):
        """有番号时行为不变：正常入队。"""
        library = library_store.add_library('正片', 'javpreset',
                                            [{'path': self._new_folder('jav2'), 'recursive': True}])
        film = self._make_film(library['id'], 'SSIS-001.mkv', dvdid='SSIS-001')
        result = library_store.start_scrape(film['id'])
        self.assertIn('task_id', result)
        self.assertEqual(library_store.get_film(film['id'])['scrape_state'], 'queued')

    def test_title_preset_does_not_require_code(self):
        """电影预设不要求番号；阶段 1 仍走 JAV 引擎（阶段 2 才接标题链路）。"""
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film1'), 'recursive': True}])
        film = self._make_film(library['id'], 'Some.Movie.2019.mkv')
        result = library_store.start_scrape(film['id'])
        self.assertIn('task_id', result)

    def test_movie_missing_fields_exclude_code(self):
        """标题路径的影片不能把「缺番号」算进缺失项；缺的是外部 ID 而不是番号。"""
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film2'), 'recursive': True}])
        folder = Path(library['folders'][0]['path'])
        Path(folder, 'Some.Movie.2019.mkv').write_bytes(b'x')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]
        self.assertNotIn('dvdid', film['missing_fields'], '电影不该提示缺番号')
        # 没有 NFO 时确实缺外部 ID，这是合理的
        self.assertIn('external_id', film['missing_fields'])

    def test_movie_nfo_tmdb_is_read_on_scan(self):
        """NFO 里已有的 tmdb id 必须在扫描时读进来，否则用户还得手填。"""
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film3'), 'recursive': True}])
        folder = Path(library['folders'][0]['path'])
        Path(folder, 'Some.Movie.2019.mkv').write_bytes(b'x')
        (folder / 'movie.nfo').write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<movie><title>某电影</title>'
            '<uniqueid type="tmdb" default="true">535167</uniqueid></movie>',
            encoding='utf-8')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]
        self.assertEqual(film['tmdb_id'], '535167')
        self.assertEqual(film['title'], '某电影')
        self.assertNotIn('dvdid', film['missing_fields'])

    def test_saved_external_id_is_used_for_scrape(self):
        """用户保存了 tmdb id 后，刮削必须按 ID 直取，不再按标题搜索。"""
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film4'), 'recursive': True}])
        film = self._make_film(library['id'], 'Some.Movie.2019.mkv')
        library_store.update_film(film['id'], {'tmdb_id': '535167', 'title': '某电影'})
        result = library_store.start_scrape(film['id'])
        task = self.queued_tasks[-1] if self.queued_tasks else None
        self.assertIsNotNone(task)
        query = task.get('scrape_query') or {}
        self.assertEqual((query.get('external_id') or {}).get('tmdb'), '535167')

    def test_manual_title_ignores_directory_year(self):
        """手输标题时不能再用目录名里的年份。

        真实案例：目录 `[绣春刀：无间炼狱]...2024...`，用户把标题改成「绣春刀」。
        若仍带 year=2024，TMDb 只会匹配到《绣春刀：诛邪》(2024)，正确的那部是 2014。
        """
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film5'), 'recursive': True}])
        film = self._make_film(library['id'],
                               '[绣春刀：无间炼狱].Brotherhood.of.Blades.2024.2160p.mkv')
        # 用户手输的标题（title_source=manual 才会作为 hint 传下去）
        library_store.update_film(film['id'], {'title': '绣春刀'})
        updated = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(updated['title_source'], 'manual')
        library_store.start_scrape(film['id'])
        task = self.queued_tasks[-1] if self.queued_tasks else None
        self.assertIsNotNone(task)
        query = task.get('scrape_query') or {}
        self.assertEqual(query.get('title'), '绣春刀')
        self.assertIsNone(query.get('year'), '手输标题时不应沿用目录年份')

    def test_manual_title_with_explicit_year_kept(self):
        """用户自己在标题里写了年份，仍然作为搜索条件。"""
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film6'), 'recursive': True}])
        film = self._make_film(library['id'], 'Some.Movie.2019.mkv')
        library_store.update_film(film['id'], {'title': '绣春刀 (2014)'})
        library_store.start_scrape(film['id'])
        task = self.queued_tasks[-1] if self.queued_tasks else None
        query = (task or {}).get('scrape_query') or {}
        self.assertEqual(query.get('year'), 2014)

    def test_nfo_title_is_used_as_query(self):
        """NFO 里的标题等同用户资料，必须优先于文件夹名。"""
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film7'), 'recursive': True}])
        folder = Path(library['folders'][0]['path'])
        (folder / 'Wrong.Folder.Name.2024.2160p.mkv').write_bytes(b'x')
        (folder / 'Wrong.Folder.Name.2024.2160p.nfo').write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<movie><title>正确的电影名</title><premiered>2014-08-07</premiered></movie>',
            encoding='utf-8')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]
        self.assertEqual(film['title_source'], 'nfo')
        library_store.start_scrape(film['id'])
        task = self.queued_tasks[-1] if self.queued_tasks else None
        query = (task or {}).get('scrape_query') or {}
        self.assertEqual(query.get('title'), '正确的电影名', '应按 NFO 标题搜索')
        self.assertEqual(query.get('year'), 2014, '年份应取 NFO 发行日期')
        self.assertNotIn('Wrong', str(query.get('title')), '不能再用文件夹名')

    def test_suggested_title_is_not_used_as_query(self):
        """文件名推测值只是预填，用户没确认前不能当成搜索依据。"""
        library = library_store.add_library('电影', 'filmpreset',
                                            [{'path': self._new_folder('film8'), 'recursive': True}])
        folder = Path(library['folders'][0]['path'])
        (folder / 'Only.Directory.Name.2024.mkv').write_bytes(b'x')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]
        self.assertEqual(film['title_source'], 'suggested')
        library_store.start_scrape(film['id'])
        task = self.queued_tasks[-1] if self.queued_tasks else None
        query = (task or {}).get('scrape_query') or {}
        # suggested 不传 hint，查询退回常规推导（source 不会是 manual）
        self.assertNotEqual(query.get('title_source'), 'manual')


class LibraryScrapeApiTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'lib-api-')
        storage.write_config(yaml.safe_dump({'network': {'retry': 3}}))
        self.client = self.enterContext(TestClient(server.app))
        previous_overrides = dict(server.app.dependency_overrides)
        self.addCleanup(lambda: (server.app.dependency_overrides.clear(),
                                 server.app.dependency_overrides.update(previous_overrides)))
        server.app.dependency_overrides[server.current_user] = lambda: {'username': 'admin', 'role': 'admin'}
        server.app.dependency_overrides[server.require_admin] = lambda: {'username': 'admin', 'role': 'admin'}
        scrapers.set_enabled('javbus', True)
        # 测试用的假视频只有几字节，把最小匹配大小调到 0，避免先被大小过滤拦下
        storage.save_preset({'id': 'javpreset', 'name': 'JAV', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0},
                                      'crawler': {'scrapers': ['javbus']}},
                             'task_concurrency': 1})

    def test_ordinary_filenames_are_not_codes(self):
        """普通文件名不能被当成番号（曾把 holiday.mkv 识别成 SXKIVP-39）。"""
        for name in ('holiday.mkv', 'no_code.mkv', 'Some.Movie.2019.mkv',
                     'movie.mp4', '给阿嬷的情书.mp4'):
            self.assertEqual(library_store._guess_dvdid(Path(name)), '', name)
        # 真番号仍然要能识别
        self.assertEqual(library_store._guess_dvdid(Path('SSIS-001.mkv')), 'SSIS-001')

    def test_library_path_is_not_mistaken_for_code(self):
        """媒体库路径里的 -数字 不能被当成番号（整库都会被误判）。"""
        cases = [
            Path(r'C:\tmp\media-api-179\holiday.mkv'),
            Path(r'D:\movies-2024\Some.Movie.2019.mkv'),
            Path(r'C:\tmp\dfgo-992\holiday.mkv'),
        ]
        for path in cases:
            self.assertEqual(library_store._guess_dvdid(path), '', str(path))
        # 以番号命名的目录仍然要能识别（多分片影片常见）
        self.assertEqual(
            library_store._guess_dvdid(Path(r'C:\lib\SSIS-001\random.mp4')), 'SSIS-001')

    def test_batch_scrape_reports_skipped_reason(self):
        media = _fresh_dir(self, 'media-api-')
        library = library_store.add_library('正片', 'javpreset', [{'path': str(media), 'recursive': True}])
        Path(media, 'holiday.mkv').write_bytes(b'x')
        # 前置校验：这个库必须解析成 jav 路径，否则后面的断言没有意义
        self.assertEqual(scrapers.resolve_path('javpreset')['path'], 'jav')
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        films = library_store.list_films(library_id=library['id'])['items']
        self.assertTrue(films, '扫描应产生影片')
        # 只应扫到我们自己写的那个文件：多余条目说明测试目录不干净
        self.assertEqual(len(films), 1, [item['video_path'] for item in films])
        # 双重前置校验：没有番号 + 预设解析为 jav，缺一不可
        self.assertEqual(films[0]['dvdid'], '', 'holiday.mkv 不应被识别出番号')
        self.assertEqual(scrapers.resolve_path(
            library_store.library_preset_for_folder(films[0]['root_id']))['path'], 'jav')
        response = self.client.post('/api/library/films/scrape',
                                    json={'ids': [film['id'] for film in films]})
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload['accepted'], [],
                         '无番号应向跳过而不是入队；实际 skipped=%r' % (payload.get('skipped'),))
        self.assertTrue(payload['skipped'])
        self.assertIn('番号', payload['skipped'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
