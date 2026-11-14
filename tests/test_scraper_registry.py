"""刮削器（搭积木）注册表与 /api/scrapers 管理接口的回归测试。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

os.environ.setdefault('MOVSP_DATA_DIR', str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-scrapers'))

from movsp import storage

with patch('threading.Thread.start'):
    from movsp import server
from movsp import scrapers
from movsp import tasks as task_runner


def tasks_flatten(crawler: dict) -> list[str]:
    """读取预设声明的刮削器有序列表（扁平格式 / 兼容旧分类格式）。"""
    return task_runner._flatten_preset_scrapers(crawler)


class ScraperRegistryTests(unittest.TestCase):
    """纯函数层：目录、开关、配置打码、插件源码、状态。"""

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='scrapers-', ignore_cleanup_errors=True))
        previous = storage.DATA_DIR
        for name, value in list(vars(storage).items()):
            if isinstance(value, Path) and value.is_relative_to(previous):
                self.enterContext(patch.object(storage, name, Path(directory) / value.relative_to(previous)))
        storage.ensure_seed_data()

    def test_catalog_contains_builtins_and_plugins(self):
        names = set(scrapers.available_names())
        for built_in in ('javbus', 'javdb', 'airav', 'fanza'):
            self.assertIn(built_in, names)
        self.assertIn('tmdb', names)          # 带 API 的插件
        self.assertIn('local_json', names)    # 离线插件
        catalog = scrapers.catalog()
        self.assertEqual(len(catalog), len(names))
        by_name = {item['name']: item for item in catalog}
        self.assertEqual(by_name['tmdb']['type'], 'api')
        self.assertTrue(any(f['key'] == 'api_key' and f['secret'] for f in by_name['tmdb']['settings_schema']))

    def test_plugins_disabled_by_default_and_toggle(self):
        self.assertFalse(scrapers.is_enabled('tmdb'))
        self.assertFalse(scrapers.is_enabled('local_json'))
        self.assertTrue(scrapers.is_enabled('fc2fan') is False)
        scrapers.set_enabled('tmdb', True)
        self.assertTrue(scrapers.is_enabled('tmdb'))
        scrapers.set_enabled('tmdb', False)
        self.assertFalse(scrapers.is_enabled('tmdb'))

    def test_settings_secret_is_masked_for_public(self):
        scrapers.update_settings('tmdb', {'api_key': 'sk-secret-9911', 'base_url': 'https://api.tmdb.org/3'})
        self.assertEqual(scrapers.get_settings('tmdb')['api_key'], 'sk-secret-9911')
        public = scrapers.public_settings('tmdb')
        self.assertEqual(public['api_key'], '********')
        self.assertEqual(public['base_url'], 'https://api.tmdb.org/3')
        self.assertNotIn('sk-secret-9911', str(public))

    def test_masked_value_keeps_existing_secret(self):
        scrapers.update_settings('tmdb', {'api_key': 'original-key'})
        scrapers.update_settings('tmdb', {'api_key': '********', 'language': 'ja-JP'})
        self.assertEqual(scrapers.get_settings('tmdb')['api_key'], 'original-key')
        self.assertEqual(scrapers.get_settings('tmdb')['language'], 'ja-JP')

    def test_status_roundtrip(self):
        scrapers.set_status('tmdb', {'state': 'ok', 'message': '配置就绪', 'mode': 'check'})
        status = scrapers.status_of('tmdb')
        self.assertEqual(status['state'], 'ok')
        self.assertIn('配置就绪', status['message'])
        self.assertTrue(status['checked_at'])

    def test_meta_overrides_and_plugin_files(self):
        scrapers.set_overrides('tmdb', {'display_name': '我的 TMDb',
                                        'provides': ['movie_by_title', 'tv_by_title']})
        entry = scrapers.catalog_entry('tmdb')
        self.assertEqual(entry['display_name'], '我的 TMDb')
        self.assertEqual(entry['provides'], ['movie_by_title', 'tv_by_title'])
        # 未知能力被丢弃，全部无效时保留原能力
        scrapers.set_overrides('tmdb', {'provides': ['not_a_capability']})
        self.assertEqual(scrapers.catalog_entry('tmdb')['provides'], ['movie_by_title', 'tv_by_title'])
        # 插件源码可以编译，且物化到 data/crawlers/
        for name in ('tmdb', 'local_json'):
            source = scrapers.plugin_source(name)
            self.assertIsNotNone(source)
            self.assertIn('def parse_data', source)
            compile(source, name + '.py', 'exec')
        self.assertTrue((storage.CUSTOM_CRAWLERS_DIR / 'tmdb.py').is_file())

    def test_user_edited_source_is_not_overwritten(self):
        scrapers.save_source('tmdb', '# user edit\ndef parse_data(movie):\n    pass\n')
        scrapers.ensure_plugin_files()
        self.assertIn('# user edit', scrapers.plugin_source('tmdb'))

    def test_restore_source(self):
        scrapers.save_source('tmdb', '# changed\n')
        source = scrapers.restore_source('tmdb')
        self.assertEqual(source, scrapers.PLUGIN_SOURCES['tmdb'])
        self.assertIn('def parse_data', source)

    def test_plugin_source_is_self_contained(self):
        """插件是独立文件，不能引用主程序里的符号。

        踩过的坑：环境变量改名时把 os.environ.get 换成了 env_compat，
        但插件执行时没有这个函数，导致所有 TMDb 刮削报
        NameError: name 'env_compat' is not defined。
        """
        # 只检查有源码的插件（内置站点爬虫由 vendor 引擎提供）
        sources = {name: scrapers.plugin_source(name) for name in scrapers.PLUGIN_SOURCES}
        sources = {name: src for name, src in sources.items() if src}
        self.assertTrue(sources, '至少应有一个带源码的插件')
        for name, source in sources.items():
            # 执行插件源码时，这些名字都不存在
            for forbidden in ('env_compat(', 'from .storage', 'from .scrapers',
                              'from movsp', 'import movsp'):
                self.assertNotIn(forbidden, source,
                                 f'{name} 插件不应依赖主程序符号: {forbidden}')

    def test_plugin_source_compiles(self):
        """所有插件源码都要能独立编译。"""
        for name in scrapers.PLUGIN_SOURCES:
            source = scrapers.plugin_source(name)
            if not source:
                continue  # 内置站点爬虫没有可编辑源码
            try:
                compile(source, f'{name}.py', 'exec')
            except SyntaxError as exc:
                self.fail(f'{name} 插件语法错误: {exc}')

    def test_plugin_env_helper_reads_legacy_name(self):
        """插件内的环境变量读取要兼容旧的 JAVSP_WEB_* 名字。"""
        source = scrapers.plugin_source('tmdb')
        self.assertIn('def _env(', source, '插件应自带 _env 帮助函数')
        self.assertIn('JAVSP_WEB_', source, '应兼容旧环境变量名')

    def test_legacy_disabled_compat_preserves_other_keys(self):
        scrapers.update_settings('tmdb', {'api_key': 'kept'})
        storage.save_disabled_built_in_crawlers({'javbus'})
        self.assertIn('javbus', scrapers.disabled_names())
        self.assertEqual(scrapers.get_settings('tmdb')['api_key'], 'kept')

    # ---- 能力模型 ----

    def test_catalog_has_no_groups_and_declares_capabilities(self):
        for item in scrapers.catalog():
            self.assertNotIn('groups', item, f"{item['name']} 不应再有分类字段")
            self.assertIn('provides', item)
            self.assertTrue(item['provides'], f"{item['name']} 必须声明至少一项能力")
            for capability in item['provides']:
                self.assertIn(capability, scrapers.CAPABILITIES)

    def test_code_scrapers_vs_title_scrapers(self):
        catalog = {item['name']: item for item in scrapers.catalog()}
        self.assertIn('movie_by_code', catalog['javbus']['provides'])
        self.assertNotIn('movie_by_title', catalog['javbus']['provides'])
        # TMDb 是标题型，不应混进番号能力
        self.assertIn('movie_by_title', catalog['tmdb']['provides'])
        self.assertNotIn('movie_by_code', catalog['tmdb']['provides'])

    def test_every_scraper_declares_a_sample(self):
        for item in scrapers.catalog():
            if item['kind'] == 'custom':
                continue
            self.assertTrue(item['sample'], f"{item['name']} 缺少检测样本")
            self.assertIn(item['sample_capability'], item['provides'])

    def test_capabilities_are_backend_only(self):
        """能力由注册表决定，用户不可编辑；管理页也不暴露能力勾选。"""
        for item in scrapers.catalog():
            for capability in item['provides']:
                self.assertIn(capability, scrapers.CAPABILITIES)
        # set_overrides 不再接受 provides
        scrapers.set_overrides('tmdb', {'provides': ['movie_by_code']})
        self.assertEqual(scrapers.catalog_entry('tmdb')['provides'],
                         ['movie_by_title', 'tv_by_title'])
        # 历史遗留的 provides 覆盖会被清理
        state = storage.read_crawler_settings()
        state.setdefault('overrides', {}).setdefault('tmdb', {})['provides'] = ['movie_by_code']
        storage.write_crawler_settings(state)
        self.assertEqual(scrapers.catalog_entry('tmdb')['provides'],
                         ['movie_by_title', 'tv_by_title'])
        scrapers.set_overrides('tmdb', {'display_name': 'X'})
        self.assertNotIn('provides', storage.read_crawler_settings().get('overrides', {}).get('tmdb', {}))

    # ---- 状态模型 ----

    def test_status_states_cover_error_分类(self):
        for state in ('ok', 'not_found', 'invalid_response', 'http_error', 'timeout',
                      'dns_error', 'tls_error', 'network_error', 'request_error', 'canceled', 'unknown'):
            self.assertIn(state, scrapers.STATUS_STATES)

    def test_status_from_error_classification(self):
        cases = [
            (TimeoutError('timed out'), 'timeout'),
            (OSError('getaddrinfo failed'), 'dns_error'),
            (ConnectionError('connection refused'), 'network_error'),
            (RuntimeError('not found'), 'not_found'),
            (ValueError('JSON decode error'), 'invalid_response'),
        ]
        for exc, expected in cases:
            self.assertEqual(scrapers.status_from_error(exc)['state'], expected, exc)
        http = scrapers.status_from_error(RuntimeError('boom'), http_status=403)
        self.assertEqual(http['state'], 'http_error')
        self.assertIn('访问被拒绝', http['message'])

    def test_make_status_clamps_unknown_state(self):
        status = scrapers.make_status('nonsense', 'x')
        self.assertEqual(status['state'], 'unknown')
        self.assertEqual(status['http_status'], 0)
        self.assertEqual(status['elapsed_ms'], -1)

    def test_evaluate_result_rejects_shell_responses(self):
        ok, _ = scrapers.evaluate_result({'title': '某影片', 'cover': 'https://x/1.jpg'}, 'SSIS-001', 'movie_by_code')
        self.assertTrue(ok)
        # 只拿到标题、没有封面/类型/演员 → 不可用
        ok, message = scrapers.evaluate_result({'title': '某影片'}, 'SSIS-001', 'movie_by_code')
        self.assertFalse(ok)
        # 标题等于查询词 → 可能是搜索页
        ok, _ = scrapers.evaluate_result({'title': 'SSIS-001', 'cover': 'x'}, 'SSIS-001', 'movie_by_code')
        self.assertFalse(ok)
        # 标题型：必须解析出标题，且至少要有一个详情字段佐证
        self.assertFalse(scrapers.evaluate_result({}, '流浪地球', 'movie_by_title')[0])
        self.assertFalse(scrapers.evaluate_result({'title': '流浪地球'}, '流浪地球', 'movie_by_title')[0])
        self.assertTrue(scrapers.evaluate_result(
            {'title': '流浪地球', 'plot': '太阳即将毁灭'}, '流浪地球', 'movie_by_title')[0])

    # ---- 预设保存的刮削器列表 ----

    def test_flat_scrapers_expand_to_every_category(self):
        """预设只维护一份扁平有序列表，任务构建时展开到引擎需要的分类结构。"""
        from movsp import tasks
        selection = tasks._expand_selection(['javbus', 'javdb'], ['normal', 'fc2', 'cid'])
        self.assertEqual(selection['normal'], ['javbus', 'javdb'])
        self.assertEqual(selection['fc2'], ['javbus', 'javdb'])
        self.assertEqual(selection['cid'], ['javbus', 'javdb'])
        # 空列表 -> 每个分类都是空，用户没选就不跑任何刮削器
        self.assertEqual(tasks._expand_selection([], ['normal']), {})

    def test_preset_scrapers_are_flat_and_default_empty(self):
        """新预设默认没有刮削器；旧的按分类 selection 会被压平成列表。"""
        self.assertEqual(tasks_flatten({'scrapers': []}), [])
        self.assertEqual(tasks_flatten({'selection': {'normal': ['javbus'], 'fc2': ['avsox']}}),
                         ['javbus', 'avsox'])
        # 去重且保留顺序
        self.assertEqual(tasks_flatten({'scrapers': ['javbus', 'javbus', 'javdb']}),
                         ['javbus', 'javdb'])


class ScraperApiTests(unittest.TestCase):
    """接口层：权限、开关、配置、编辑、体检、测试。"""

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='scrapers-api-', ignore_cleanup_errors=True))
        previous = storage.DATA_DIR
        for name, value in list(vars(storage).items()):
            if isinstance(value, Path) and value.is_relative_to(previous):
                self.enterContext(patch.object(storage, name, Path(directory) / value.relative_to(previous)))
        storage.ensure_seed_data()
        storage.write_config(yaml.safe_dump({'network': {'retry': 3}}))
        self.client = self.enterContext(TestClient(server.app))
        previous_overrides = dict(server.app.dependency_overrides)
        self.addCleanup(lambda: (server.app.dependency_overrides.clear(), server.app.dependency_overrides.update(previous_overrides)))

    def as_role(self, role):
        server.app.dependency_overrides[server.current_user] = lambda: {'username': role, 'role': role}
        server.app.dependency_overrides[server.require_admin] = lambda: {'username': role, 'role': role}

    def test_operator_sees_catalog_but_no_secret_or_source(self):
        # 管理员写入密钥
        self.as_role('admin')
        self.client.put('/api/scrapers/tmdb/settings', json={'settings': {'api_key': 'top-secret-123'}})
        # 操作员：目录可见，但密钥打码；源码接口需要管理员
        server.app.dependency_overrides[server.current_user] = lambda: {'username': 'operator', 'role': 'operator'}
        server.app.dependency_overrides.pop(server.require_admin, None)
        response = self.client.get('/api/scrapers')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn('tmdb', [item['name'] for item in payload['scrapers']])
        self.assertNotIn('top-secret-123', response.text)
        self.assertEqual(self.client.get('/api/scrapers/tmdb/source').status_code, 403)

    def test_admin_can_toggle_settings_and_meta(self):
        self.as_role('admin')
        response = self.client.post('/api/scrapers/tmdb/enable', json={'enabled': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(next(i for i in response.json()['scrapers'] if i['name'] == 'tmdb')['enabled'])
        response = self.client.put('/api/scrapers/tmdb/settings', json={'settings': {'api_key': 'abc123', 'language': 'en-US'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['settings']['api_key'], 'abc123')
        response = self.client.put('/api/scrapers/tmdb/meta', json={'display_name': 'TMDB 中文', 'groups': ['normal']})
        self.assertEqual(response.status_code, 200)
        item = next(i for i in response.json()['scrapers'] if i['name'] == 'tmdb')
        self.assertEqual(item['display_name'], 'TMDB 中文')

    def test_source_read_only_for_builtin_editable_for_plugin(self):
        self.as_role('admin')
        builtin = self.client.get('/api/scrapers/javbus/source')
        self.assertEqual(builtin.status_code, 200)
        self.assertFalse(builtin.json()['editable'])
        plugin = self.client.get('/api/scrapers/tmdb/source')
        self.assertEqual(plugin.status_code, 200)
        self.assertTrue(plugin.json()['editable'])
        self.assertTrue(plugin.json()['restorable'])
        self.assertIn('def parse_data', plugin.json()['source'])
        self.assertEqual(self.client.put('/api/scrapers/javbus/source', json={'source': 'x'}).status_code, 400)

    def test_save_source_validates_parse_data(self):
        self.as_role('admin')
        self.assertEqual(self.client.put('/api/scrapers/tmdb/source', json={'source': 'x = 1'}).status_code, 400)
        response = self.client.put('/api/scrapers/tmdb/source', json={'source': 'def parse_data(movie):\n    pass\n'})
        self.assertEqual(response.status_code, 200)

    def test_create_custom_and_delete(self):
        self.as_role('admin')
        response = self.client.post('/api/scrapers/custom', json={'name': 'my_scraper', 'display_name': '我的积木'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('my_scraper', [i['name'] for i in response.json()['scrapers']])
        self.assertEqual(self.client.post('/api/scrapers/custom', json={'name': 'my_scraper'}).status_code, 409)
        self.assertEqual(self.client.post('/api/scrapers/custom', json={'name': 'tmdb'}).status_code, 409)
        self.assertEqual(self.client.delete('/api/scrapers/custom/my_scraper').status_code, 200)
        self.assertNotIn('my_scraper', [i['name'] for i in self.client.get('/api/scrapers').json()['scrapers']])

    def test_check_all_records_status(self):
        self.as_role('admin')
        response = self.client.post('/api/scrapers/check-all', json={'names': ['tmdb', 'local_json']})
        self.assertEqual(response.status_code, 200)
        status = response.json()['status']
        # 两个插件都缺少必填配置，体检应报错并把状态写回
        self.assertEqual(status['tmdb']['state'], 'request_error')
        self.assertEqual(status['local_json']['state'], 'request_error')
        catalog = self.client.get('/api/scrapers').json()['scrapers']
        self.assertEqual(next(i for i in catalog if i['name'] == 'tmdb')['status']['state'], 'request_error')

    def test_legacy_endpoints_still_work(self):
        self.as_role('admin')
        names = self.client.get('/api/crawler-config/names')
        self.assertEqual(names.status_code, 200)
        # 默认停用的插件不会出现在旧接口（需先在刮削器管理中启用）
        listed = [i['name'] for i in names.json()['crawlers']]
        self.assertIn('javbus', listed)
        self.assertNotIn('tmdb', listed)
        self.assertEqual(self.client.delete('/api/crawler-config/built-in/javbus').status_code, 200)
        self.assertEqual(self.client.post('/api/crawler-config/built-in/javbus/restore').status_code, 200)
        # 启用插件后旧接口也能看到
        self.client.post('/api/scrapers/tmdb/enable', json={'enabled': True})
        self.assertIn('tmdb', [i['name'] for i in self.client.get('/api/crawler-config/names').json()['crawlers']])


if __name__ == '__main__':
    unittest.main()
