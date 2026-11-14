"""阶段 2：标题路径刮削（scrape_plan / native_scrape / NFO 外部 ID）。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('MOVSP_DATA_DIR', str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-native'))

from movsp import scrapers, storage
from movsp import library_store
from movsp import native_scrape, scrape_plan


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


class CleanFilenameTests(unittest.TestCase):
    def test_removes_technical_noise(self):
        cases = {
            'Star.Wars.1977.2160p.BluRay.x265.HDR': 'Star Wars 1977',
            'Interstellar (2014) [2160p] [HDR]': 'Interstellar 2014',
            'The.Matrix.1999.REMASTERED.1080p.BluRay.x264.DTS-HD.MA.5.1': 'The Matrix 1999',
            '流浪地球.2019.1080p.WEB-DL': '流浪地球 2019',
            '教父.The.Godfather.1972.2160p.UHD.BluRay.x265.10bit.HDR': '教父 The Godfather 1972',
            'Movie.Name.CD1': 'Movie Name',
        }
        for raw, expected in cases.items():
            self.assertEqual(scrape_plan.clean_filename(raw), expected, raw)

    def test_keeps_bare_titles(self):
        self.assertEqual(scrape_plan.clean_filename('SSIS-001'), 'SSIS-001')
        self.assertEqual(scrape_plan.clean_filename('某部电影'), '某部电影')

    def test_year_extraction_ignores_bare_numbers(self):
        # 1917 / 2012 是片名，单独的年份要靠括号或分隔符识别
        self.assertEqual(scrape_plan.extract_year('1917.2019.1080p.BluRay'), 2019)
        self.assertIsNone(scrape_plan.extract_year('1917'))
        self.assertEqual(scrape_plan.extract_year('Some Movie (2019)'), 2019)

    def test_strip_year_from_title(self):
        """标题里不能带年份，否则 TMDb 按标题搜不到（"三体 2023" 查无结果）。"""
        cases = {
            '三体 (2023)': '三体',
            '三体 2023': '三体',
            'Star Wars (1977)': 'Star Wars',
            '1917': '1917',          # 纯数字片名不能被当成"只有年份"
            '三体': '三体',
        }
        for raw, expected in cases.items():
            self.assertEqual(scrape_plan.strip_year(raw), expected, raw)

    def test_clean_release_group_suffix(self):
        """发布组后缀必须清掉，否则标题搜不到。"""
        raw = '飞驰人生2.Pegasus.2.2024.WEB-DL.2160p.HEVC.AAC-ZmWeb'
        cleaned = scrape_plan.clean_filename(raw)
        self.assertEqual(cleaned, '飞驰人生2 Pegasus 2 2024')
        self.assertEqual(scrape_plan.strip_year(cleaned), '飞驰人生2 Pegasus 2')
        self.assertEqual(scrape_plan.extract_year(raw), 2024)

    def test_clean_frame_rate_and_language_suffix(self):
        """60fps / Mandarin / CHS 这类尾部标记必须清掉（实机踩过）。"""
        raw = ('给阿嬷的情书.Dear.You.2026.2160p.WEB-DL.DV.H.265.60fps.10bit.'
               'AAC.CHS.Mandarin-CSWEB')
        cleaned = scrape_plan.clean_filename(raw)
        self.assertEqual(cleaned, '给阿嬷的情书 Dear You 2026')
        self.assertEqual(scrape_plan.strip_year(cleaned), '给阿嬷的情书 Dear You')
        self.assertEqual(scrape_plan.extract_year(raw), 2026)
        for noise in ('60fps', 'Mandarin', 'CHS', 'DV', 'H 265'):
            self.assertNotIn(noise, cleaned)

    def test_clean_leading_bracket_and_release_group(self):
        """PT 站「[中文名].English.Name...-Group」写法要清干净。"""
        raw = ('[长津湖之水门桥].The.Battle.At.Lake.Changjin.Ⅱ.2022.1080p.120fps.'
               'WEB-DL.HEVC.10bit.DDP-QHstudIo')
        cleaned = scrape_plan.clean_filename(raw)
        self.assertEqual(cleaned, '长津湖之水门桥 The Battle At Lake Changjin Ⅱ 2022')
        title = scrape_plan.strip_year(cleaned)
        self.assertEqual(title, '长津湖之水门桥 The Battle At Lake Changjin Ⅱ')
        # 不能残留残括号或发布组
        for residue in (']', '[', 'QHstudIo', '-QHstudIo'):
            self.assertNotIn(residue, cleaned)
        self.assertEqual(scrape_plan.extract_year(raw), 2022)

    def test_year_mid_title_is_stripped(self):
        """年份不在末尾时也要能剥掉（清洗后已去掉发布组）。"""
        self.assertEqual(scrape_plan.strip_year('星战 1977 特别版'), '星战 特别版')

    def test_series_query_strips_year(self):
        info = scrape_plan.detect_series_query(
            nfo=None, series_dir=Path('某剧 (2021)'))
        self.assertEqual(info['kind'], 'series')
        self.assertEqual(info['title'], '某剧')
        self.assertEqual(info['year'], 2021)


class QueryDetectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='query-detect-', ignore_cleanup_errors=True)))

    def _make_dir(self, name, files):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        for filename in files:
            (path / filename).write_bytes(b'x')
        return path

    def test_no_magic_scrape_on_scan(self):
        """扫描只建索引，不能自己触发刮削任务（刮削必须由用户点）。"""
        import inspect
        from movsp import library_store
        src = inspect.getsource(library_store._run_scan)
        self.assertNotIn('start_scrape(', src, '扫描不应调用 start_scrape')
        self.assertNotIn('create_task(', src, '扫描不应直接建刮削任务')

    def test_suggested_title_only_when_nfo_empty(self):
        """NFO 没标题时才用文件名建议值；有标题时不覆盖。"""
        from movsp import library_store
        self.assertEqual(library_store._guess_title(
            self._make_dir('Some.Movie.2019', ['Some.Movie.2019.mkv']),
            Path('Some.Movie.2019.mkv')), 'Some Movie')

    def test_single_film_directory_uses_directory_name(self):
        folder = self._make_dir('星球大战 (1977)', ['Star.Wars.1977.2160p.mkv'])
        info = scrape_plan.detect_query(directory=folder, video=folder / 'Star.Wars.1977.2160p.mkv')
        self.assertEqual(info['kind'], 'title')
        # 年份单独存在 year 字段，标题里不再重复（否则 TMDb 搜不到）
        self.assertEqual(info['title'], '星球大战')
        self.assertEqual(info['title_source'], 'directory')
        self.assertEqual(info['year'], 1977)

    def test_smattering_files_ignore_parent_directory_name(self):
        """散装文件：父目录名不能当标题。"""
        folder = self._make_dir('电影', ['Star.Wars.1977.1080p.mkv', '流浪地球.2019.1080p.mkv'])
        video = folder / 'Star.Wars.1977.1080p.mkv'
        info = scrape_plan.detect_query(directory=folder, video=video)
        self.assertEqual(info['title_source'], 'filename')
        self.assertEqual(info['title'], 'Star Wars')
        self.assertEqual(info['year'], 1977)
        self.assertNotIn('电影', info['title'])

    def test_root_directory_name_never_used(self):
        """媒体库根目录本身永远不当标题来源，哪怕里面只有 1 个文件。"""
        folder = self._make_dir('电影', ['Only.Movie.2020.mkv'])
        info = scrape_plan.detect_query(directory=folder, video=folder / 'Only.Movie.2020.mkv',
                                        root_path=str(folder))
        self.assertEqual(info['title_source'], 'filename')
        self.assertEqual(info['title'], 'Only Movie')
        self.assertEqual(info['year'], 2020)

    def test_nfo_title_wins(self):
        folder = self._make_dir('某目录', ['whatever.mkv'])
        info = scrape_plan.detect_query(
            nfo={'title': '正确的电影名', 'original_title': 'Original Name'},
            directory=folder, video=folder / 'whatever.mkv')
        self.assertEqual(info['title'], '正确的电影名')
        self.assertEqual(info['title_source'], 'nfo')

    def test_code_takes_priority(self):
        folder = self._make_dir('SSIS-001', ['SSIS-001.mkv'])
        info = scrape_plan.detect_query(directory=folder, video=folder / 'SSIS-001.mkv',
                                        code='SSIS-001')
        self.assertEqual(info['kind'], 'code')
        self.assertEqual(info['code'], 'SSIS-001')

    def test_external_id_is_carried(self):
        folder = self._make_dir('某片', ['a.mkv'])
        info = scrape_plan.detect_query(
            nfo={'tmdb_id': '535167', 'imdb_id': 'tt7605074'},
            directory=folder, video=folder / 'a.mkv')
        self.assertEqual(info['external_id'], {'tmdb': '535167', 'imdb': 'tt7605074'})


class MergeTests(unittest.TestCase):
    class _Info:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def test_first_non_empty_field_wins(self):
        first = self._Info(title='A', cover='c1', plot=None)
        second = self._Info(title='B', cover='c2', plot='剧情')
        merged = native_scrape._merge([('one', first), ('two', second)])
        self.assertEqual(merged['title'], 'A', '靠前的刮削器优先')
        self.assertEqual(merged['plot'], '剧情', '前一个没有的字段才用后面的')
        self.assertEqual(merged['_field_sources']['title'], 'one')

    def test_external_ids_are_merged(self):
        first = self._Info(title='A', tmdb_id='11')
        second = self._Info(title='B', imdb_id='tt0076759')
        merged = native_scrape._merge([('one', first), ('two', second)])
        self.assertEqual(merged['tmdb_id'], '11')
        self.assertEqual(merged['imdb_id'], 'tt0076759')


class UrlNormalizationTests(unittest.TestCase):
    """用户填的地址常常缺协议头（api.tmdb.org），后端必须补全。"""

    def setUp(self):
        source = scrapers.PLUGIN_SOURCES['tmdb']
        namespace = {}
        exec(compile(source, 'tmdb.py', 'exec'), namespace)  # noqa: S102 - 测试里执行自家插件
        self.normalize = namespace['_normalize_base']

    def test_adds_https_scheme(self):
        self.assertEqual(self.normalize('api.tmdb.org', 'https://api.themoviedb.org/3', '/3'),
                         'https://api.tmdb.org/3')

    def test_keeps_existing_scheme(self):
        self.assertEqual(self.normalize('http://api.tmdb.org/3', 'x', '/3'),
                         'http://api.tmdb.org/3')

    def test_does_not_duplicate_version(self):
        self.assertEqual(self.normalize('api.tmdb.org/3', 'x', '/3'), 'https://api.tmdb.org/3')
        self.assertEqual(self.normalize('https://api.tmdb.org/3/', 'x', '/3'),
                         'https://api.tmdb.org/3')

    def test_empty_uses_default(self):
        self.assertEqual(self.normalize('', 'https://api.themoviedb.org/3', '/3'),
                         'https://api.themoviedb.org/3')

    def test_no_version_for_image_base(self):
        self.assertEqual(self.normalize('image.tmdb.org/t/p', 'x', ''),
                         'https://image.tmdb.org/t/p')


class NativeScrapeFlowTests(unittest.TestCase):
    """用假的本地刮削器验证整条链路，不依赖网络。"""

    def setUp(self):
        _isolate(self, 'native-flow-')
        self.root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='native-media-', ignore_cleanup_errors=True)))
        # 写一个离线刮削器：按 _query_title 返回固定资料
        scrapers.save_source('fake_movie', (
            'def parse_data(movie):\n'
            '    title = getattr(movie, "_query_title", "") or ""\n'
            '    year = getattr(movie, "_query_year", None)\n'
            '    movie.title = f"抓到的-{title}"\n'
            '    movie.plot = f"年份={year}"\n'
            '    movie.tmdb_id = "12345"\n'
            '    movie.cover = "https://example.invalid/c.jpg"\n'
            '    movie.actress = ["演员甲"]\n'
        ))

    def test_scrape_by_query_uses_query_context(self):
        query = {'kind': 'title', 'code': '', 'title': '某电影', 'year': 2021,
                 'title_source': 'filename', 'external_id': {}}
        result = native_scrape.scrape_by_query(query, [{'name': 'fake_movie'}])
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['title'], '抓到的-某电影')
        self.assertEqual(result['data']['plot'], '年份=2021')
        self.assertEqual(result['data']['tmdb_id'], '12345')

    def test_missing_scraper_reports_error(self):
        query = {'kind': 'title', 'code': '', 'title': 'X', 'year': None,
                 'title_source': 'filename', 'external_id': {}}
        result = native_scrape.scrape_by_query(query, [{'name': 'not_exists'}])
        self.assertFalse(result['ok'])
        self.assertIn('not_exists', result['errors'])

    def test_no_nodes(self):
        result = native_scrape.scrape_by_query({'title': 'X'}, [])
        self.assertFalse(result['ok'])
        self.assertIn('没有可用的刮削器', result['message'])

    def test_one_failure_does_not_block_others(self):
        query = {'kind': 'title', 'code': '', 'title': '某电影', 'year': 2021,
                 'title_source': 'filename', 'external_id': {}}
        result = native_scrape.scrape_by_query(
            query, [{'name': 'not_exists'}, {'name': 'fake_movie'}])
        self.assertTrue(result['ok'], '一个失败不影响另一个')
        self.assertEqual(result['data']['title'], '抓到的-某电影')
        self.assertIn('not_exists', result['errors'])


class NfoExternalIdTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'nfo-id-')
        self.root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='nfo-media-', ignore_cleanup_errors=True)))

    def _film(self, name='Movie.2020'):
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        video = folder / f'{name}.mkv'
        video.write_bytes(b'x')
        return {
            'id': 'f-1', 'kind': 'movie', 'dir_path': str(folder), 'video_path': str(video),
            'dvdid': '', 'title': '', 'original_title': '', 'plot': '', 'release_date': '',
            'studio': '', 'publisher': '', 'director': '', 'runtime': 0,
            'actress': [], 'tmdb_id': '', 'imdb_id': '',
        }

    def test_write_and_read_external_ids(self):
        film = self._film()
        values = {'dvdid': '', 'tmdb_id': '535167', 'imdb_id': 'tt7605074',
                  'title': '流浪地球', 'original_title': 'The Wandering Earth',
                  'plot': '', 'release_date': '2019-02-05', 'studio': '', 'publisher': '',
                  'director': '', 'runtime': 125, 'actress': []}
        target = library_store._write_nfo(film, values, None)
        self.assertTrue(target.is_file())
        parsed = library_store._parse_nfo(target)
        self.assertEqual(parsed['tmdb_id'], '535167')
        self.assertEqual(parsed['imdb_id'], 'tt7605074')
        self.assertEqual(parsed['dvdid'], '', '普通电影不应写番号')
        self.assertEqual(parsed['title'], '流浪地球')

    def test_jav_nfo_unchanged(self):
        film = self._film('SSIS-001')
        values = {'dvdid': 'SSIS-001', 'tmdb_id': '', 'imdb_id': '',
                  'title': '标题', 'original_title': '', 'plot': '', 'release_date': '',
                  'studio': '', 'publisher': '', 'director': '', 'runtime': 120, 'actress': []}
        target = library_store._write_nfo(film, values, None)
        parsed = library_store._parse_nfo(target)
        self.assertEqual(parsed['dvdid'], 'SSIS-001')
        self.assertEqual(parsed['tmdb_id'], '', 'JAV 不应写 tmdb')
        self.assertEqual(parsed['imdb_id'], '')


class ImageSizeTests(unittest.TestCase):
    """图片地址必须用 TMDb 支持的尺寸。

    踩过的坑：`w600` 不是 TMDb 的海报尺寸，请求会直接 SSLError；
    `original` 动辄几 MB，下载不稳定（实测约 1/6 失败）。
    """

    def _helper(self):
        from movsp import scrapers
        body = scrapers._TMDB_BODY
        # 从模板里取出 _image_url 单独编译执行
        start = body.index('def _image_url')
        end = body.index('def ', start + 10)
        namespace = {}
        exec(body[start:end], namespace)  # noqa: S102 - 测试环境执行模板片段
        return namespace['_image_url']

    def test_supported_sizes_only(self):
        from movsp import scrapers
        body = scrapers._TMDB_BODY
        # TMDb 海报支持的后缀：w92/w154/w185/w342/w500/w780/original
        allowed = {'w92', 'w154', 'w185', 'w342', 'w500', 'w780',
                   'w1280', 'w300', 'original'}
        import re
        used = set(re.findall(r'_image_url\(image_base,\s*"([^"]+)"', body))
        self.assertTrue(used, '应有用到 _image_url')
        bad = used - allowed
        self.assertEqual(bad, set(), '用了 TMDb 不支持的尺寸: %s' % bad)
        # w600 是不存在的尺寸，曾经导致下载全失败
        self.assertNotIn('w600', used)

    def test_builds_expected_url(self):
        build = self._helper()
        self.assertEqual(build('https://image.tmdb.org/t/p', 'w500', '/a.jpg'),
                         'https://image.tmdb.org/t/p/w500/a.jpg')
        self.assertEqual(build('https://image.tmdb.org/t/p', 'w500', ''), '')


if __name__ == '__main__':
    unittest.main()
