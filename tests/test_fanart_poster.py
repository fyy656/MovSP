"""横版海报（fanart）与竖版封面的区分、落库与接口。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('MOVSP_DATA_DIR',
                      str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-fanart'))

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
    from movsp import tasks as task_runner
    case.enterContext(patch.object(task_runner, '_enqueue_task', lambda task: None))
    return root


class FanartDiscoveryTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'fanart-detect-')
        self.media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='fanart-media-', ignore_cleanup_errors=True)))

    def test_portrait_and_landscape_are_separated(self):
        """Emby 规则：同名图/-poster 是竖版 Primary，-fanart 才是横版 Backdrop。"""
        folder = self.media / 'ABC-123'
        folder.mkdir()
        (folder / 'ABC-123.mp4').write_bytes(b'x')
        (folder / 'ABC-123.jpg').write_bytes(b'vertical')
        (folder / 'ABC-123-fanart.jpg').write_bytes(b'horizontal')
        self.assertEqual(Path(library_store._find_cover(folder, 'ABC-123')).name,
                         'ABC-123.jpg')
        fanart, count = library_store._find_fanart(folder, 'ABC-123')
        self.assertEqual(Path(fanart).name, 'ABC-123-fanart.jpg')
        self.assertGreaterEqual(count, 1)

    def test_poster_suffix_is_portrait_not_landscape(self):
        """`-poster` 是竖版 Primary，绝不能当横版（Jellyfin 官方命名表）。"""
        folder = self.media / 'XYZ-999'
        folder.mkdir()
        (folder / 'XYZ-999.mp4').write_bytes(b'x')
        (folder / 'XYZ-999-poster.jpg').write_bytes(b'vertical')
        self.assertEqual(Path(library_store._find_cover(folder, 'XYZ-999')).name,
                         'XYZ-999-poster.jpg')
        self.assertEqual(library_store._find_fanart(folder, 'XYZ-999')[0], '',
                         '只有 -poster 时不该有横版图')

    def test_landscape_only_is_not_used_as_cover(self):
        """只有横版图时，竖版封面应为空，而不是把横版拉伸当竖版。"""
        folder = self.media / 'DEF-456'
        folder.mkdir()
        (folder / 'DEF-456.mp4').write_bytes(b'x')
        (folder / 'DEF-456-fanart.jpg').write_bytes(b'horizontal')
        self.assertEqual(library_store._find_cover(folder, 'DEF-456'), '')
        self.assertEqual(Path(library_store._find_fanart(folder, 'DEF-456')[0]).name,
                         'DEF-456-fanart.jpg')

    def test_classic_naming_still_works(self):
        """传统 poster/fanart 命名保持兼容。"""
        folder = self.media / 'GHI-789'
        folder.mkdir()
        (folder / 'GHI-789.mp4').write_bytes(b'x')
        (folder / 'poster.jpg').write_bytes(b'v')
        (folder / 'fanart.jpg').write_bytes(b'h')
        self.assertEqual(Path(library_store._find_cover(folder, 'GHI-789')).name, 'poster.jpg')
        self.assertEqual(Path(library_store._find_fanart(folder, 'GHI-789')[0]).name, 'fanart.jpg')

    def test_backdrop_and_landscape_are_landscape(self):
        """backdrop / landscape / thumb 都是横版。"""
        folder = self.media / 'JKL-345'
        folder.mkdir()
        (folder / 'JKL-345.mp4').write_bytes(b'x')
        (folder / 'JKL-345.jpg').write_bytes(b'v')
        (folder / 'JKL-345-landscape.jpg').write_bytes(b'h')
        self.assertEqual(Path(library_store._find_fanart(folder, 'JKL-345')[0]).name,
                         'JKL-345-landscape.jpg')
        folder2 = self.media / 'MNO-678'
        folder2.mkdir()
        (folder2 / 'MNO-678.mp4').write_bytes(b'x')
        (folder2 / 'MNO-678.jpg').write_bytes(b'v')
        (folder2 / 'backdrop.jpg').write_bytes(b'h')
        self.assertEqual(Path(library_store._find_fanart(folder2, 'MNO-678')[0]).name,
                         'backdrop.jpg')

    def test_scan_persists_fanart_path(self):
        """扫描要把横版路径写进 films.fanart_path。"""
        storage.save_preset({'id': 'tiny', 'name': 'tiny', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})
        folder = self.media / 'JKL-012'
        folder.mkdir()
        (folder / 'JKL-012.mp4').write_bytes(b'x')
        (folder / 'JKL-012.jpg').write_bytes(b'v')
        (folder / 'JKL-012-fanart.jpg').write_bytes(b'h')
        library = library_store.add_library('库', 'tiny', [{'path': str(self.media), 'recursive': True}])
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        film = library_store.list_films(library_id=library['id'])['items'][0]
        self.assertEqual(Path(film['cover_path']).name, 'JKL-012.jpg')
        self.assertEqual(Path(film['fanart_path']).name, 'JKL-012-fanart.jpg')


    def test_movie_artwork_naming_is_emby_compliant(self):
        """标题路径下载的图必须按 Emby 规则命名，不能用有歧义的 -poster。

        竖版 → {stem}.jpg（Primary）；横版 → {stem}-fanart.jpg（Backdrop）。
        """
        folder = self.media / 'ART-001'
        folder.mkdir()
        (folder / 'ART-001.mp4').write_bytes(b'x')
        film = {'kind': 'movie', 'dir_path': str(folder),
                'video_path': str(folder / 'ART-001.mp4')}
        # 预先放好目标文件，验证命名规则（不联网）
        cover = folder / 'ART-001.jpg'
        cover.write_bytes(b'v')
        fanart = folder / 'ART-001-fanart.jpg'
        fanart.write_bytes(b'h')
        self.assertEqual(library_store._download_artwork(film, 'https://x/a.jpg', 'cover'),
                         str(cover))
        self.assertEqual(library_store._download_artwork(film, 'https://x/b.jpg', 'fanart'),
                         str(fanart))
        # 关键：绝不能生成 -poster.jpg
        self.assertFalse((folder / 'ART-001-poster.jpg').exists())


    def test_cover_priority_order(self):
        """竖版优先级：同名图 > 纯名 poster/folder/cover/default > 后缀。"""
        stem = 'PRI-001'

        def cover_with(files):
            folder = self.media / ('p' + str(len(files)) + str(hash(tuple(files)) % 9999))
            folder.mkdir(exist_ok=True)
            (folder / f'{stem}.mp4').write_bytes(b'x')
            for name in files:
                (folder / name).write_bytes(b'i')
            got = library_store._find_cover(folder, stem)
            return Path(got).name if got else ''

        # 同名图最优先
        self.assertEqual(cover_with([f'{stem}.jpg', 'poster.jpg']), f'{stem}.jpg')
        # 纯名层整体高于后缀层
        self.assertEqual(cover_with(['poster.jpg', f'{stem}-poster.jpg']), 'poster.jpg')
        # 纯名层内部：poster > folder > cover > default
        self.assertEqual(cover_with(['default.jpg', 'cover.jpg']), 'cover.jpg')
        self.assertEqual(cover_with(['cover.jpg', 'folder.jpg']), 'folder.jpg')
        self.assertEqual(cover_with(['folder.jpg', 'poster.jpg']), 'poster.jpg')
        # 后缀层内部
        self.assertEqual(cover_with([f'{stem}-folder.jpg', f'{stem}-poster.jpg']),
                         f'{stem}-poster.jpg')

    def test_fanart_priority_order(self):
        """横版优先级：Backdrop 后缀 > Backdrop 纯名 > Thumb 类（landscape/thumb 兜底）。"""
        stem = 'PRI-002'

        def fanart_with(files):
            folder = self.media / ('f' + str(len(files)) + str(hash(tuple(files)) % 9999))
            folder.mkdir(exist_ok=True)
            (folder / f'{stem}.mp4').write_bytes(b'x')
            for name in files:
                (folder / name).write_bytes(b'i')
            got = library_store._find_fanart(folder, stem)[0]
            return Path(got).name if got else ''

        # Backdrop 后缀优先于纯名
        self.assertEqual(fanart_with([f'{stem}-fanart.jpg', 'fanart.jpg']),
                         f'{stem}-fanart.jpg')
        # 纯名 backdrop 优先于 Thumb 后缀
        self.assertEqual(fanart_with(['backdrop.jpg', f'{stem}-thumb.jpg']), 'backdrop.jpg')
        self.assertEqual(fanart_with(['fanart.jpg', f'{stem}-landscape.jpg']), 'fanart.jpg')
        # Thumb 类只在没有 Backdrop 时兜底
        self.assertEqual(fanart_with([f'{stem}-thumb.jpg']), f'{stem}-thumb.jpg')
        self.assertEqual(fanart_with(['landscape.jpg']), 'landscape.jpg')
        # -poster 绝不能出现在横版里
        self.assertEqual(fanart_with([f'{stem}-poster.jpg']), '')

    def test_all_forms_coexist(self):
        """所有命名形式同时存在时的最终结果。"""
        folder = self.media / 'ALL-001'
        folder.mkdir()
        stem = 'ALL-001'
        (folder / f'{stem}.mkv').write_bytes(b'x')
        for name in (f'{stem}.jpg', 'poster.jpg', 'fanart.jpg',
                     f'{stem}-poster.jpg', f'{stem}-fanart.jpg',
                     f'{stem}-backdrop.jpg', f'{stem}-landscape.jpg',
                     'clearlogo.png', f'{stem}.nfo', f'{stem}.nfo.bak'):
            (folder / name).write_bytes(b'i')
        self.assertEqual(Path(library_store._find_cover(folder, stem)).name, f'{stem}.jpg')
        self.assertEqual(Path(library_store._find_fanart(folder, stem)[0]).name,
                         f'{stem}-fanart.jpg')


    def test_series_and_movie_use_same_rules(self):
        """剧集与影片必须用同一套 Emby 规则，否则两个库显示不一致。"""
        from movsp import series_scan
        stem = 'ABC-123'
        cases = [
            [f'{stem}.jpg', 'poster.jpg', 'folder.jpg',
             f'{stem}-fanart.jpg', 'fanart.jpg', 'backdrop.jpg'],
            ['poster.jpg', 'fanart.jpg'],
            ['poster.jpg', 'fanart.jpg', f'{stem}-poster.jpg'],
            [f'{stem}-fanart.jpg', 'fanart.jpg'],
            [f'{stem}-thumb.jpg'],
            ['backdrop.jpg', f'{stem}-thumb.jpg'],
        ]
        for index, files in enumerate(cases):
            folder = self.media / f'cmp{index}'
            folder.mkdir()
            (folder / f'{stem}.mp4').write_bytes(b'x')
            for name in files:
                (folder / name).write_bytes(b'i')
            movie_cover = Path(library_store._find_cover(folder, stem)).name
            movie_fanart = Path(library_store._find_fanart(folder, stem)[0]).name
            series_cover = Path(series_scan.find_poster(folder, stem)).name
            series_fanart = Path(series_scan.find_fanart(folder, stem)).name
            self.assertEqual(movie_cover, series_cover, f'竖版不一致: {files}')
            self.assertEqual(movie_fanart, series_fanart, f'横版不一致: {files}')

    def test_series_without_stem_uses_bare_names(self):
        """剧集目录没有单一视频文件名，只能认纯名主图。"""
        from movsp import series_scan
        folder = self.media / 'seriesonly'
        folder.mkdir()
        (folder / 'poster.jpg').write_bytes(b'v')
        (folder / 'fanart.jpg').write_bytes(b'h')
        self.assertEqual(Path(series_scan.find_poster(folder)).name, 'poster.jpg')
        self.assertEqual(Path(series_scan.find_fanart(folder)).name, 'fanart.jpg')


class FanartApiTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'fanart-api-')
        self.client = self.enterContext(TestClient(server.app))
        previous = dict(server.app.dependency_overrides)
        self.addCleanup(lambda: (server.app.dependency_overrides.clear(),
                                 server.app.dependency_overrides.update(previous)))
        server.app.dependency_overrides[server.current_user] = lambda: {'username': 'admin', 'role': 'admin'}
        server.app.dependency_overrides[server.require_admin] = lambda: {'username': 'admin', 'role': 'admin'}
        self.media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='fanart-api-media-', ignore_cleanup_errors=True)))

    def _make_film(self, stem='MNO-345'):
        storage.save_preset({'id': 'tiny', 'name': 'tiny', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})
        folder = self.media / stem
        folder.mkdir()
        (folder / f'{stem}.mp4').write_bytes(b'x')
        (folder / f'{stem}.jpg').write_bytes(b'PORTRAIT')
        (folder / f'{stem}-fanart.jpg').write_bytes(b'LANDSCAPE')
        library = library_store.add_library('库', 'tiny', [{'path': str(self.media), 'recursive': True}])
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library_store.list_films(library_id=library['id'])['items'][0]

    def test_fanart_endpoint_returns_landscape(self):
        film = self._make_film()
        cover = self.client.get(f'/api/library/films/{film["id"]}/cover')
        self.assertEqual(cover.status_code, 200)
        self.assertEqual(cover.content, b'PORTRAIT')
        fanart = self.client.get(f'/api/library/films/{film["id"]}/fanart')
        self.assertEqual(fanart.status_code, 200)
        self.assertEqual(fanart.content, b'LANDSCAPE', '横版接口必须返回横版图')

    def test_fanart_falls_back_to_cover(self):
        """没有独立横版图时，横版接口退回竖版封面（前端按比例处理，不会拉伸）。"""
        storage.save_preset({'id': 'tiny', 'name': 'tiny', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})
        folder = self.media / 'PQR-678'
        folder.mkdir()
        (folder / 'PQR-678.mp4').write_bytes(b'x')
        (folder / 'PQR-678.jpg').write_bytes(b'ONLY-PORTRAIT')
        library = library_store.add_library('库', 'tiny', [{'path': str(self.media), 'recursive': True}])
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        film = library_store.list_films(library_id=library['id'])['items'][0]
        fanart = self.client.get(f'/api/library/films/{film["id"]}/fanart')
        self.assertEqual(fanart.status_code, 200)
        self.assertEqual(fanart.content, b'ONLY-PORTRAIT')

    def test_unknown_film_returns_404(self):
        self.assertEqual(self.client.get('/api/library/films/nope/fanart').status_code, 404)


class BrokenNfoTests(unittest.TestCase):
    """磁盘上的 .nfo 不是合法 XML 时（被 PT 发布说明覆盖），保存不能崩。"""

    def setUp(self):
        self.media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='broken-nfo-', ignore_cleanup_errors=True)))
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix='broken-nfo-data-', ignore_cleanup_errors=True))
        root = Path(directory)
        for module in (storage, library_store):
            previous = module.DATA_DIR
            for name, value in list(vars(module).items()):
                if isinstance(value, Path) and value.is_relative_to(previous):
                    self.enterContext(patch.object(module, name, root / value.relative_to(previous)))
        storage.ensure_seed_data()
        library_store._reset_ready()
        self.addCleanup(library_store._reset_ready)

    def _make_film(self):
        storage.save_preset({'id': 'tiny', 'name': 't', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})
        folder = self.media / 'BROKEN-001'
        folder.mkdir()
        (folder / 'BROKEN-001.mkv').write_bytes(b'x' * 512)
        # 真实踩坑：.nfo 里是 BBCode 发布说明，不是 XML
        (folder / 'BROKEN-001.nfo').write_text(
            '[quote][b][color=Black][size=5]高清杜比 QHstudIo 出品！[/color][/size][/quote]\n'
            '[img]https://example.com/poster.jpg[/img]\n',
            encoding='utf-8')
        library = library_store.add_library('库', 'tiny', [{'path': str(self.media), 'recursive': True}])
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return folder, library_store.list_films(library_id=library['id'])['items'][0]

    def test_scan_flags_broken_nfo(self):
        """扫描要把非法 NFO 标成冲突，而不是当成正常资料。"""
        folder, film = self._make_film()
        self.assertEqual(film['status'], 'conflict')

    def test_save_recovers_broken_nfo(self):
        """保存时备份原文件并重建合法 XML，不抛异常。"""
        folder, film = self._make_film()
        updated = library_store.update_film(film['id'], {
            'title': '长津湖之水门桥', 'tmdb_id': '760868', 'plot': '剧情',
            'actress': [], 'runtime': 149, 'release_date': '2022-02-01',
        }, write_nfo=True)
        self.assertIsNotNone(updated)
        self.assertEqual(updated['title'], '长津湖之水门桥')
        self.assertEqual(updated['tmdb_id'], '760868')
        nfo = folder / 'BROKEN-001.nfo'
        content = nfo.read_text(encoding='utf-8')
        self.assertIn('<movie>', content)
        self.assertIn('长津湖之水门桥', content)
        # 原内容必须留一份备份
        self.assertTrue((folder / 'BROKEN-001.nfo.bak').is_file())
        self.assertIn('QHstudIo', (folder / 'BROKEN-001.nfo.bak').read_text(encoding='utf-8'))


class ArtworkOverwriteTests(unittest.TestCase):
    """重新刮削必须覆盖旧图：否则从第一季换到第五季，封面还是旧的。"""

    def setUp(self):
        self.media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='artwork-ow-', ignore_cleanup_errors=True)))

    def test_overwrite_replaces_existing(self):
        folder = self.media / 'SERIES'
        folder.mkdir()
        target = folder / 'poster.jpg'
        target.write_bytes(b'OLD')
        film = {'kind': 'series', 'dir_path': str(folder), 'video_path': ''}
        # 用不存在的域名配合 overwrite=False 验证「不覆盖就退出」
        self.assertEqual(library_store._download_artwork(
            film, 'https://invalid.invalid/x.jpg', 'cover', overwrite=False),
            str(target))
        self.assertEqual(target.read_bytes(), b'OLD', '不覆盖时保持原样')

    def test_default_skips_existing_artwork(self):
        """默认不覆盖：已有图就不重复下载（用户确认的正确行为）。"""
        movie = {'kind': 'movie', 'season_number': 0}
        self.assertFalse(library_store._artwork_should_overwrite(
            movie, {'scrape_query': {}}), '电影不覆盖')
        series_same = {'kind': 'series', 'season_number': 5}
        self.assertFalse(library_store._artwork_should_overwrite(
            series_same, {'scrape_query': {'season': 5}}), '同季不覆盖')
        self.assertFalse(library_store._artwork_should_overwrite(
            {'kind': 'series', 'season_number': 0}, {'scrape_query': {}}), '未填季号不覆盖')

    def test_overwrite_when_season_changed(self):
        """换了季才覆盖：否则第五季目录里留着的第一季海报永远换不掉。"""
        series = {'kind': 'series', 'season_number': 1}
        self.assertTrue(library_store._artwork_should_overwrite(
            series, {'scrape_query': {'season': 5}}), '第一季→第五季应覆盖')

    def test_artwork_kind_mapping_for_series(self):
        """剧集：竖版 poster.jpg、横版 fanart.jpg。"""
        folder = self.media / 'S2'
        folder.mkdir()
        (folder / 'poster.jpg').write_bytes(b'p')
        (folder / 'fanart.jpg').write_bytes(b'f')
        film = {'kind': 'series', 'dir_path': str(folder), 'video_path': ''}
        self.assertEqual(Path(library_store._download_artwork(
            film, 'https://x/a.jpg', 'cover')).name, 'poster.jpg')
        self.assertEqual(Path(library_store._download_artwork(
            film, 'https://x/b.jpg', 'fanart')).name, 'fanart.jpg')

    def test_episode_still_named_thumb(self):
        """分集剧照写 `<视频名>-thumb.jpg`（Emby/Jellyfin 认这个后缀）。"""
        folder = self.media / 'S3'
        folder.mkdir()
        video = folder / 'E01.mkv'
        video.write_bytes(b'x')
        thumb = folder / 'E01-thumb.jpg'
        thumb.write_bytes(b't')
        episode = {'id': 'e1', 'kind': 'episode', 'video_path': str(video),
                   'dir_path': str(folder), 'files': [str(video)]}
        # overwrite=False 时已有文件直接返回，不联网
        self.assertEqual(library_store._download_episode_still(
            episode, 'https://x/t.jpg', overwrite=False), str(thumb))
        self.assertEqual(thumb.read_bytes(), b't', '不覆盖时保持原样')

    def test_failure_is_logged_to_task(self):
        """图片下载失败要写进任务日志，否则用户以为刮削没生效。"""
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix='artwork-log-data-', ignore_cleanup_errors=True))
        root = Path(directory)
        for module in (storage, library_store):
            previous = module.DATA_DIR
            for name, value in list(vars(module).items()):
                if isinstance(value, Path) and value.is_relative_to(previous):
                    self.enterContext(patch.object(module, name, root / value.relative_to(previous)))
        storage.ensure_seed_data()
        library_store._reset_ready()
        self.addCleanup(library_store._reset_ready)
        media = self.media / 'lib'
        folder = media / 'SHOW.S01'
        folder.mkdir(parents=True)
        (folder / 'ep.mkv').write_bytes(b'x' * 512)
        storage.save_preset({'id': 'p', 'name': 't', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})
        library = library_store.add_library('日志库', 'p',
                                            [{'path': str(media), 'recursive': True}], kind='series')
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        series = next(f for f in library_store.list_films(library_id=library['id'])['items']
                      if f['kind'] == 'series')
        task = {'id': 'log-check-1', 'scraped_data': {
            'title': '某剧', 'cover': 'https://invalid.invalid/x.jpg',
            'big_cover': 'https://invalid.invalid/y.jpg'}}
        original = library_store._refresh_after_native
        library_store._refresh_after_native = lambda fid: None
        try:
            library_store.apply_native_result(series['id'], task)
        finally:
            library_store._refresh_after_native = original
        from movsp import tasks as task_runner
        logs = task_runner._logs.get('log-check-1') or []
        self.assertTrue(any('图片未更新' in line for line in logs),
                        '应提示图片未更新：%r' % logs)


if __name__ == '__main__':
    unittest.main()
