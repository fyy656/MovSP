"""关闭播放器必须真正中断本地直连流。

历史问题：流注册时用番号当 key，而停止接口用 film_id 找流，
两者不相等 → stop_stream 永远返回 False → 关掉页面后服务端继续读盘。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', tempfile.mkdtemp(prefix='stream-test-'))

with patch('threading.Thread.start'):
    from movsp import server  # noqa: F401

from movsp import library_store, storage, streaming


class StreamStopTests(unittest.TestCase):
    def setUp(self):
        storage.ensure_seed_data()
        library_store._reset_ready()
        self.addCleanup(library_store._reset_ready)
        self.addCleanup(streaming._ACTIVE_STREAMS.clear)
        storage.save_preset({'id': 'p', 'name': 't', 'mode': 'form',
                             'form': {'scanner': {'minimum_size': 0}},
                             'task_concurrency': 1})

    def _film(self):
        media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='stream-media-', ignore_cleanup_errors=True)))
        folder = media / 'ABC-123'
        folder.mkdir(parents=True)
        (folder / 'ABC-123.mp4').write_bytes(b'x' * 2048)
        library = library_store.add_library(
            f'库{os.urandom(4).hex()}', 'p', [{'path': str(media), 'recursive': True}])
        job = library_store.start_scan(library['id'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (library_store.get_scan(job['id']) or {}).get('status') in ('succeeded', 'failed'):
                break
            time.sleep(0.02)
        return library_store.list_films(library_id=library['id'])['items'][0]

    def test_stop_stream_matches_registered_key(self):
        """流用 film_id 注册时，stop_stream(film_id) 必须能叫停。"""
        film = self._film()
        film_id = film['id']
        event = streaming._stream_event(film_id)
        self.assertTrue(streaming.stop_stream(film_id), '应能叫停这条流')
        self.assertTrue(event.is_set(), '取消事件应被触发')
        self.assertNotIn(film_id, streaming._ACTIVE_STREAMS, '应从活动表移除')

    def test_stop_stream_returns_false_for_unknown(self):
        self.assertFalse(streaming.stop_stream('not-a-real-film'))

    def test_stream_key_is_film_id_not_code(self):
        """回归：不能再用番号当流 key。

        影片有番号时，若 key 取番号，停止接口用 film_id 就永远找不到。
        """
        source = (Path(__file__).resolve().parent.parent
                  / 'movsp' / 'server.py').read_text(encoding='utf8')
        # 定位到 stream 端点，确认用 film_id 作为 key
        idx = source.find('def library_film_stream')
        self.assertGreater(idx, 0, '应能找到 stream 端点')
        block = source[idx:idx + 1200]
        self.assertIn('key=film_id', block, 'stream 端点必须用 film_id 当 key')
        self.assertNotIn('key=tag', block, '不能再用 tag（番号）当 key')


if __name__ == '__main__':
    unittest.main()
