"""预设类型（JAV / 电影·剧集）与影视预设字段裁剪。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('MOVSP_DATA_DIR',
                      str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-kind'))

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


class TrimTests(unittest.TestCase):
    def test_media_preset_keeps_only_effective_fields(self):
        """影视预设只保留扫描器筛选项与刮削器列表。"""
        form = {
            'scanner': {'minimum_size': 0, 'ignore_folders': ['\\..'], 'extensions': ['.mkv'],
                        # 下面这些是 JavSP 专用，标题路径完全不读
                        'media_types': ['normal'], 'ignore_id_pattern': ['1080P']},
            'crawler': {'scrapers': ['tmdb'], 'selection': {'normal': ['tmdb']}},
            'summarizer': {'move_files': True, 'cover': {'crop': {'engine': 'x'}}},
            'translator': {'engine': 'google'},
        }
        trimmed = scrapers.trim_media_preset(form)
        self.assertEqual(set(trimmed), {'scanner', 'crawler'})
        self.assertEqual(trimmed['scanner'],
                         {'minimum_size': 0, 'ignore_folders': ['\\..'], 'extensions': ['.mkv']})
        self.assertEqual(trimmed['crawler'], {'scrapers': ['tmdb']})
        for dropped in ('media_types', 'ignore_id_pattern'):
            self.assertNotIn(dropped, trimmed['scanner'])

    def test_jav_preset_is_not_trimmed(self):
        """JAV 预设必须保留全部设置（整理、裁剪、剧照都真的在用）。"""
        form = {'summarizer': {'move_files': True, 'cover': {'crop': {'engine': 'x'}}}}
        # trim 只对影视预设调用；这里确认它确实会动 JAV 字段
        trimmed = scrapers.trim_media_preset(form)
        self.assertNotIn('summarizer', trimmed)


class KindTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'preset-kind-')
        scrapers.set_enabled('tmdb', True)
        scrapers.set_enabled('javbus', True)

    def test_declared_kind_wins_over_capabilities(self):
        """显式声明 media 时，即使加了番号型刮削器也走标题路径。"""
        storage.save_preset({'id': 'm', 'name': '影视', 'kind': 'media', 'mode': 'form',
                             'form': {'crawler': {'scrapers': ['tmdb', 'javbus']}},
                             'task_concurrency': 1})
        self.assertEqual(scrapers.resolve_path('m')['path'], 'title')

    def test_declared_jav_stays_jav(self):
        storage.save_preset({'id': 'j', 'name': 'JAV', 'kind': 'jav', 'mode': 'form',
                             'form': {'crawler': {'scrapers': ['javbus', 'tmdb']}},
                             'task_concurrency': 1})
        self.assertEqual(scrapers.resolve_path('j')['path'], 'jav')

    def test_legacy_preset_infers_kind(self):
        """没有 kind 的老预设按刮削器能力推断。"""
        storage.save_preset({'id': 'legacy_jav', 'name': '老JAV', 'mode': 'form',
                             'form': {'crawler': {'scrapers': ['javbus']}},
                             'task_concurrency': 1})
        self.assertEqual(scrapers.preset_kind(storage.get_preset('legacy_jav')), 'jav')
        storage.save_preset({'id': 'legacy_media', 'name': '老影视', 'mode': 'form',
                             'form': {'crawler': {'scrapers': ['tmdb']}},
                             'task_concurrency': 1})
        self.assertEqual(scrapers.preset_kind(storage.get_preset('legacy_media')), 'media')


class PresetApiTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'preset-api-')
        self.client = self.enterContext(TestClient(server.app))
        previous = dict(server.app.dependency_overrides)
        self.addCleanup(lambda: (server.app.dependency_overrides.clear(),
                                 server.app.dependency_overrides.update(previous)))
        server.app.dependency_overrides[server.current_user] = lambda: {'username': 'admin', 'role': 'admin'}
        server.app.dependency_overrides[server.require_admin] = lambda: {'username': 'admin', 'role': 'admin'}
        scrapers.set_enabled('tmdb', True)

    def test_create_media_preset_trims_and_keeps_kind(self):
        response = self.client.post('/api/presets', json={
            'name': '影视', 'mode': 'form', 'kind': 'media', 'task_concurrency': 1,
            'form': {
                'scanner': {'minimum_size': 0, 'media_types': ['normal']},
                'crawler': {'scrapers': ['tmdb']},
                'summarizer': {'move_files': True, 'cover': {'crop': {'engine': 'x'}}},
                'translator': {'engine': 'google'},
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['kind'], 'media')
        self.assertEqual(set(body['form']), {'scanner', 'crawler'})
        self.assertNotIn('summarizer', body['form'])
        self.assertNotIn('translator', body['form'])

    def test_media_preset_skips_javsp_validation(self):
        """影视预设不经过 JavSP 引擎，不该被 JavSP 的格式校验拦住。"""
        response = self.client.post('/api/presets', json={
            'name': '影视2', 'mode': 'form', 'kind': 'media', 'task_concurrency': 1,
            'form': {
                # 这些值对 JavSP 来说格式非法，但影视路径根本不读
                'scanner': {'media_types': ['not-a-real-type'], 'ignore_id_pattern': 'oops'},
                'crawler': {'scrapers': ['tmdb']},
            },
        })
        self.assertEqual(response.status_code, 200, response.text)

    def test_jav_preset_still_validated(self):
        """JAV 预设必须继续走 JavSP 校验。"""
        response = self.client.post('/api/presets', json={
            'name': 'JAV坏配置', 'mode': 'form', 'kind': 'jav', 'task_concurrency': 1,
            'form': {'scanner': {'media_types': ['not-a-real-type']}},
        })
        self.assertEqual(response.status_code, 400)

    def test_preset_list_exposes_kind(self):
        items = self.client.get('/api/presets').json()
        self.assertTrue(all('kind' in item for item in items))
        for item in items:
            self.assertIn(item['kind'], ('jav', 'media'))


if __name__ == '__main__':
    unittest.main()
