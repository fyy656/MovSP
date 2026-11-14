"""换片后封面必须重新下载。

场景：用户在资料页把 TMDb ID 从真人版改成动漫版（或清空后重填），
NFO 更新了但海报还是上一部作品——因为「已有图就不再下载」。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('MOVSP_DATA_DIR', os.path.join(
    os.environ.get('TEMP', '.'), 'artwork-overwrite-test'))

from movsp.library_store import _artwork_should_overwrite


class ArtworkOverwriteTests(unittest.TestCase):
    def _check(self, film, task):
        return _artwork_should_overwrite(film, task)

    def test_id_changed_triggers_overwrite(self):
        """真人版换动漫版：ID 不同就必须重下封面。"""
        self.assertTrue(self._check(
            {'kind': 'series', 'tmdb_id': '243224', 'season_number': 0},
            {'scrape_query': {'external_id': {'tmdb': '106449'}}}))

    def test_same_id_keeps_artwork(self):
        """同一部重刮：不重复下载。"""
        self.assertFalse(self._check(
            {'kind': 'series', 'tmdb_id': '106449', 'season_number': 0},
            {'scrape_query': {'external_id': {'tmdb': '106449'}}}))

    def test_cleared_then_refilled(self):
        """「清空输入」后重填 ID：库里是空的，目录里还有旧图，必须换。"""
        self.assertTrue(self._check(
            {'kind': 'series', 'tmdb_id': '', 'imdb_id': '', 'season_number': 0},
            {'scrape_query': {'external_id': {'tmdb': '106449'}}}))

    def test_first_scrape_without_existing_id(self):
        """首次刮削（没有显式 ID）：走默认不覆盖。"""
        self.assertFalse(self._check(
            {'kind': 'series', 'tmdb_id': '', 'season_number': 0},
            {'scrape_query': {}}))

    def test_movie_id_changed(self):
        self.assertTrue(self._check(
            {'kind': 'movie', 'tmdb_id': '100', 'season_number': 0},
            {'scrape_query': {'external_id': {'tmdb': '200'}}}))

    def test_movie_same_id(self):
        self.assertFalse(self._check(
            {'kind': 'movie', 'tmdb_id': '100', 'season_number': 0},
            {'scrape_query': {'external_id': {'tmdb': '100'}}}))

    def test_imdb_changed(self):
        self.assertTrue(self._check(
            {'kind': 'series', 'tmdb_id': '1', 'imdb_id': 'tt111', 'season_number': 0},
            {'scrape_query': {'external_id': {'imdb': 'tt222'}}}))

    def test_season_changed_same_id(self):
        """同 ID 换季：一季一个目录时也要换图。"""
        self.assertTrue(self._check(
            {'kind': 'series', 'tmdb_id': '106449', 'season_number': 1},
            {'scrape_query': {'external_id': {'tmdb': '106449'}, 'season': 2}}))

    def test_same_season_keeps(self):
        self.assertFalse(self._check(
            {'kind': 'series', 'tmdb_id': '106449', 'season_number': 2},
            {'scrape_query': {'external_id': {'tmdb': '106449'}, 'season': 2}}))

    def test_engine_path_without_query(self):
        """引擎路径（没有 scrape_query）：保持默认不覆盖。"""
        self.assertFalse(self._check({'kind': 'series', 'tmdb_id': '1', 'season_number': 0}, {}))


if __name__ == '__main__':
    unittest.main()
