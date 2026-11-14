"""视频预览帧接口：电影 / 剧集 / 分集都能取到画面。"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from movsp import thumbnails

if not thumbnails.available():
    raise unittest.SkipTest('环境没有 ffmpeg，跳过预览帧接口测试')

from fastapi.testclient import TestClient

os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='thumb-api-'))

with patch('threading.Thread.start'):
    from movsp import server

from movsp import library_store, storage


class ThumbnailApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix='thumb-api-data-')
        storage.ensure_seed_data()
        library_store._reset_ready()
        cls.client = TestClient(server.app)
        server.app.dependency_overrides[server.current_user] = lambda: {
            'username': 'admin', 'role': 'admin'}
        server.app.dependency_overrides[server.require_admin] = lambda: {
            'username': 'admin', 'role': 'admin'}

    def setUp(self):
        storage.ensure_seed_data()
        library_store._reset_ready()
        storage.save_preset({'id': 'p', 'name': 't', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})

    def _make_video(self, path, seconds=20):
        ffmpeg = thumbnails.find_ffmpeg()
        subprocess.run([ffmpeg, '-y', '-loglevel', 'error',
                        '-f', 'lavfi', '-i',
                        f'testsrc=size=640x360:rate=25:duration={seconds}',
                        '-pix_fmt', 'yuv420p', str(path)], check=True)

    def _scan(self, media, kind=None):
        kwargs = {'kind': kind} if kind else {}
        library = library_store.add_library(
            f'库{time.time_ns()}', 'p', [{'path': str(media), 'recursive': True}], **kwargs)
        scan = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (library_store.get_scan(scan['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library

    def test_movie_thumbnail(self):
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='thumb-movie-', ignore_cleanup_errors=True)))
        folder = media / 'ABCD-123'
        folder.mkdir(parents=True)
        self._make_video(folder / 'ABCD-123.mp4')
        library = self._scan(media)
        film = library_store.list_films(library_id=library['id'])['items'][0]

        response = self.client.get(f"/api/library/films/{film['id']}/thumbnail")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'\xff\xd8'), '应返回 JPEG')

    def test_series_uses_first_episode(self):
        """剧集本身没有视频，必须借用第一集才能出画面。"""
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='thumb-series-', ignore_cleanup_errors=True)))
        show = media / '某剧.S01'
        show.mkdir(parents=True)
        for n in (1, 2):
            self._make_video(show / f'E{n:02d}.mp4')
        library = self._scan(media, kind='series')
        series = next(f for f in library_store.list_films(library_id=library['id'])['items']
                      if f['kind'] == 'series')
        self.assertEqual(series['video_path'], '', '剧集条目本身没有视频')

        response = self.client.get(f"/api/library/films/{series['id']}/thumbnail")
        self.assertEqual(response.status_code, 200, '剧集应借用第一集出预览')
        self.assertTrue(response.content.startswith(b'\xff\xd8'))

    def test_episode_thumbnail(self):
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='thumb-ep-', ignore_cleanup_errors=True)))
        show = media / '某剧.S01'
        show.mkdir(parents=True)
        self._make_video(show / 'E01.mp4')
        library = self._scan(media, kind='series')
        series = next(f for f in library_store.list_films(library_id=library['id'])['items']
                      if f['kind'] == 'series')
        episodes = library_store.list_films(library_id=library['id'], kind='episode',
                                           parent=series['id'])['items']
        self.assertTrue(episodes, '应有分集条目')
        response = self.client.get(f"/api/library/films/{episodes[0]['id']}/thumbnail")
        self.assertEqual(response.status_code, 200)

    def test_missing_ffmpeg_returns_404(self):
        """没有 ffmpeg 时返回 404，前端保持原有占位，不报错。"""
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='thumb-noff-', ignore_cleanup_errors=True)))
        folder = media / 'NOFF-001'
        folder.mkdir(parents=True)
        self._make_video(folder / 'NOFF-001.mp4')
        library = self._scan(media)
        film = library_store.list_films(library_id=library['id'])['items'][0]

        with patch.object(thumbnails, 'find_ffmpeg', return_value=None), \
                patch.object(thumbnails, 'available', return_value=False), \
                patch.object(thumbnails, 'existing_thumbnail', return_value=None):
            response = self.client.get(f"/api/library/films/{film['id']}/thumbnail")
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
