"""阶段 3：候选面板 + needs_choice 状态 + 按选定 ID 重刮。"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

os.environ.setdefault('MOVSP_DATA_DIR', str(Path(tempfile.gettempdir()) / 'javsp-web-testdata-choice'))

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
    # 测试里不启动后台 worker：把入队改成「记下来，测试自己同步跑」，
    # 否则共享的 worker 线程会在用例之间消费别人排队的任务。
    from movsp import tasks as task_runner
    queued: list[dict] = []
    original_enqueue = task_runner._enqueue_task

    def fake_enqueue(task):
        queued.append(task)

    case.enterContext(patch.object(task_runner, '_enqueue_task', fake_enqueue))
    case.queued_tasks = queued
    case.original_enqueue = original_enqueue
    return root


def _drain_task_queue():
    """清掉上一个用例遗留的排队任务，避免后台 worker 抢当前用例的任务。"""
    from movsp import tasks as task_runner

    with task_runner._queue_condition:
        task_runner._pending_tasks.clear()
        task_runner._queue_condition.notify_all()


def _run_inline(case, task_id):
    """不依赖后台 worker：把排队的任务当场同步执行（测试专用）。"""
    from movsp import tasks as task_runner

    queued = getattr(case, 'queued_tasks', [])
    picked = None
    for index, task in enumerate(list(queued)):
        if task.get('id') == task_id:
            picked = task
            del queued[index]
            break
    if picked is not None:
        task_runner._run_task(picked)
    return task_id


class CandidateFlowTests(unittest.TestCase):
    """用一个会返回多条候选的假刮削器验证整条链路。"""

    def setUp(self):
        _isolate(self, 'choice-')
        self.root = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='choice-media-', ignore_cleanup_errors=True)))
        # 假刮削器：无外部 ID 时返回 3 条候选；指定 ID 时精确返回
        scrapers.save_source('fake_multi', (
            '__provides__ = ["movie_by_title"]\n'
            'def parse_data(movie):\n'
            '    ext = getattr(movie, "_query_external_id", {}) or {}\n'
            '    known = str(ext.get("tmdb") or "").strip()\n'
            '    if known:\n'
            '        movie.title = f"精确-{known}"\n'
            '        movie.tmdb_id = known\n'
            '        return\n'
            '    movie.title = "默认选中项"\n'
            '    movie.tmdb_id = "100"\n'
            '    movie._tmdb_candidates = [\n'
            '        {"tmdb_id": "100", "title": "默认选中项", "year": "1977", "score": 8.2},\n'
            '        {"tmdb_id": "200", "title": "第二个", "year": "2015", "score": 7.5},\n'
            '        {"tmdb_id": "300", "title": "第三个", "year": "2017", "score": 7.0},\n'
            '    ]\n'
        ))
        scrapers.set_enabled('fake_multi', True)
        storage.save_preset({
            'id': 'titlepreset', 'name': '电影', 'mode': 'form',
            'form': {'scanner': {'minimum_size': 0},
                     'crawler': {'scrapers': ['fake_multi']}},
            'task_concurrency': 1,
        })
        self.media = self.root / 'media'
        self.media.mkdir(parents=True)

    def _make_library(self):
        return library_store.add_library('电影', 'titlepreset',
                                         [{'path': str(self.media), 'recursive': True}])

    def _scan(self, library_id):
        scan = library_store.start_scan(library_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                return
            time.sleep(0.02)
        self.fail('扫描超时')

    def _wait_task(self, task_id):
        from movsp.task_store import record as task_record
        # 先尝试同步执行（移除后台 worker 的竞态），再读最终记录
        _run_inline(self, task_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            record = task_record(task_id, logs=False)
            if record and record.get('status') in ('succeeded', 'failed', 'needs_choice', 'cancelled'):
                return record
            time.sleep(0.05)
        return task_record(task_id, logs=False)

    def _wait_film_state(self, film_id, expected, timeout=20):
        """任务结束后媒体库回写是异步的，等影片状态落到目标值。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            film = library_store.get_film(film_id, reconcile=False)
            if film and film.get('scrape_state') == expected:
                return film
            time.sleep(0.05)
        return library_store.get_film(film_id, reconcile=False)

    def _scrape_and_wait(self, film_id, expected='needs_choice'):
        """发起刮削并同步执行，返回目标状态下的影片。"""
        result = library_store.start_scrape(film_id)
        self._wait_task(result['task_id'])
        return self._wait_film_state(film_id, expected)

    def test_multiple_candidates_mark_needs_choice(self):
        library = self._make_library()
        folder = self.media / 'Some Movie (1977)'
        folder.mkdir()
        (folder / 'Some.Movie.1977.mkv').write_bytes(b'x')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]

        result = library_store.start_scrape(film['id'])
        record = self._wait_task(result['task_id'])
        self.assertEqual(record.get('status'), 'needs_choice')
        self.assertEqual(len(record.get('scrape_candidates') or []), 3)
        # 影片已落盘最可能的一部，并标记待确认
        updated = self._wait_film_state(film['id'], 'needs_choice')
        self.assertEqual(updated['scrape_state'], 'needs_choice')
        self.assertEqual(updated['title'], '默认选中项')

    def test_choosing_candidate_requeries_by_id(self):
        library = self._make_library()
        folder = self.media / 'Some Movie (1977)'
        folder.mkdir()
        (folder / 'Some.Movie.1977.mkv').write_bytes(b'x')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]
        library_store.start_scrape(film['id'])
        self._wait_task(library_store.get_film(film['id'])['last_task_id'])

        result = library_store.start_scrape_with_external(film['id'], tmdb_id='200')
        record = self._wait_task(result['task_id'])
        self.assertEqual(record.get('status'), 'succeeded', record.get('error'))
        updated = self._wait_film_state(film['id'], 'idle')
        self.assertEqual(updated['tmdb_id'], '200')
        self.assertEqual(updated['title'], '精确-200')
        self.assertEqual(updated['scrape_state'], 'idle')

    def test_needs_choice_survives_rescan(self):
        """重新扫描不能把待确认状态冲掉。"""
        library = self._make_library()
        folder = self.media / 'Some Movie (1977)'
        folder.mkdir()
        (folder / 'Some.Movie.1977.mkv').write_bytes(b'x')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]
        self._scrape_and_wait(film['id'], 'needs_choice')
        self.assertEqual(library_store.get_film(film['id'])['scrape_state'], 'needs_choice')
        self._scan(library['id'])
        self.assertEqual(self._wait_film_state(film['id'], 'needs_choice')['scrape_state'], 'needs_choice')

    def test_filter_lists_pending_choice(self):
        library = self._make_library()
        folder = self.media / 'Some Movie (1977)'
        folder.mkdir()
        (folder / 'Some.Movie.1977.mkv').write_bytes(b'x')
        self._scan(library['id'])
        film = library_store.list_films(library_id=library['id'])['items'][0]
        self._scrape_and_wait(film['id'], 'needs_choice')
        pending = library_store.list_films(library_id=library['id'], status='choice')['items']
        self.assertEqual([item['id'] for item in pending], [film['id']])


class CandidateApiTests(unittest.TestCase):
    def setUp(self):
        _isolate(self, 'choice-api-')
        storage.write_config(yaml.safe_dump({'network': {'retry': 3}}))
        self.client = self.enterContext(TestClient(server.app))
        previous = dict(server.app.dependency_overrides)
        self.addCleanup(lambda: (server.app.dependency_overrides.clear(),
                                 server.app.dependency_overrides.update(previous)))
        server.app.dependency_overrides[server.current_user] = lambda: {'username': 'admin', 'role': 'admin'}
        server.app.dependency_overrides[server.require_admin] = lambda: {'username': 'admin', 'role': 'admin'}
        self.media = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix='choice-api-media-', ignore_cleanup_errors=True)))
        scrapers.save_source('fake_multi', (
            '__provides__ = ["movie_by_title"]\n'
            'def parse_data(movie):\n'
            '    ext = getattr(movie, "_query_external_id", {}) or {}\n'
            '    known = str(ext.get("tmdb") or "").strip()\n'
            '    if known:\n'
            '        movie.title = f"精确-{known}"\n'
            '        movie.tmdb_id = known\n'
            '        return\n'
            '    movie.title = "默认选中项"\n'
            '    movie.tmdb_id = "100"\n'
            '    movie._tmdb_candidates = [\n'
            '        {"tmdb_id": "100", "title": "默认选中项", "year": "1977"},\n'
            '        {"tmdb_id": "200", "title": "第二个", "year": "2015"},\n'
            '    ]\n'
        ))
        scrapers.set_enabled('fake_multi', True)
        storage.save_preset({
            'id': 'titlepreset', 'name': '电影', 'mode': 'form',
            'form': {'scanner': {'minimum_size': 0},
                     'crawler': {'scrapers': ['fake_multi']}},
            'task_concurrency': 1,
        })

    def _prepare_film(self):
        library = library_store.add_library('电影', 'titlepreset',
                                            [{'path': str(self.media), 'recursive': True}])
        folder = self.media / 'Some Movie (1977)'
        folder.mkdir(exist_ok=True)
        (folder / 'Some.Movie.1977.mkv').write_bytes(b'x')
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        film = library_store.list_films(library_id=library['id'])['items'][0]
        from movsp.task_store import record as task_record
        library_store.start_scrape(film['id'])
        task_id = library_store.get_film(film['id'])['last_task_id']
        _run_inline(self, task_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (task_record(task_id, logs=False) or {}).get('status') in ('succeeded', 'needs_choice', 'failed'):
                break
            time.sleep(0.05)
        return film

    def test_candidates_endpoint(self):
        film = self._prepare_film()
        response = self.client.get(f'/api/library/films/{film["id"]}/candidates')
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload['candidates']), 2)
        self.assertEqual(payload['candidates'][0]['tmdb_id'], '100')
        self.assertEqual(payload['status'], 'needs_choice')

    def test_choose_endpoint(self):
        film = self._prepare_film()
        response = self.client.post(f'/api/library/films/{film["id"]}/choose',
                                    json={'tmdb_id': '200'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIn('task_id', response.json())
        from movsp.task_store import record as task_record
        task_id = response.json()['task_id']
        _run_inline(self, task_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (task_record(task_id, logs=False) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.05)
        updated = library_store.get_film(film['id'])
        self.assertEqual(updated['tmdb_id'], '200')
        self.assertEqual(updated['title'], '精确-200')

    def test_choose_requires_an_id(self):
        film = self._prepare_film()
        response = self.client.post(f'/api/library/films/{film["id"]}/choose', json={})
        self.assertEqual(response.status_code, 400)

    def test_candidates_for_unknown_film(self):
        self.assertEqual(self.client.get('/api/library/films/nope/candidates').status_code, 404)


if __name__ == '__main__':
    unittest.main()
