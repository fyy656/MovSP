"""视频预览缩略图：档位选择、缓存与降级。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from movsp import thumbnails


class PickSecondTests(unittest.TestCase):
    """时间点档位：取不超过时长的最大值（与 JavBoss 一致的策略）。"""

    def test_long_video_uses_largest_bucket(self):
        self.assertEqual(thumbnails.pick_second(7200), 128)
        self.assertEqual(thumbnails.pick_second(300), 128)

    def test_medium_buckets(self):
        self.assertEqual(thumbnails.pick_second(120), 63)
        self.assertEqual(thumbnails.pick_second(60), 32)
        self.assertEqual(thumbnails.pick_second(30), 16)

    def test_short_video(self):
        self.assertEqual(thumbnails.pick_second(10), 8)
        self.assertEqual(thumbnails.pick_second(5), 4)
        self.assertEqual(thumbnails.pick_second(1), 1)

    def test_unknown_duration_uses_fallback(self):
        """未刮削的影片 runtime=0：用默认档位，ffmpeg 超时长会输出最后一帧。"""
        self.assertEqual(thumbnails.pick_second(0), thumbnails.FALLBACK_SECOND)
        self.assertEqual(thumbnails.pick_second(None), thumbnails.FALLBACK_SECOND)
        self.assertEqual(thumbnails.pick_second("bad"), thumbnails.FALLBACK_SECOND)

    def test_always_positive(self):
        for duration in (0, 1, 3, 7, 15, 31, 63, 127, 129, 99999):
            self.assertGreater(thumbnails.pick_second(duration), 0,
                               f'时长 {duration} 应选出正数时间点')


class FindFfmpegTests(unittest.TestCase):
    """ffmpeg 查找链：环境变量 > PATH > 常见安装目录。"""

    def _reset(self):
        thumbnails._resolved.clear()
        thumbnails._lookup_built = False

    def setUp(self):
        self.addCleanup(self._reset)
        self._reset()

    def test_env_var_points_to_file(self):
        if not thumbnails.find_ffmpeg():
            self.skipTest('环境没有 ffmpeg')
        real = thumbnails.find_ffmpeg()
        self._reset()
        with patch.dict(os.environ, {'MOVSP_FFMPEG': real}):
            self.assertEqual(thumbnails.find_ffmpeg(), real)

    def test_env_var_points_to_directory(self):
        if not thumbnails.find_ffmpeg():
            self.skipTest('环境没有 ffmpeg')
        real = Path(thumbnails.find_ffmpeg())
        self._reset()
        with patch.dict(os.environ, {'MOVSP_FFMPEG': str(real.parent)}):
            found = Path(thumbnails.find_ffmpeg())
            # Windows 下文件名大小写不敏感（ffmpeg.EXE / ffmpeg.exe 都合法）
            self.assertEqual(found.name.lower(), real.name.lower())
            self.assertEqual(found.parent, real.parent)

    def test_env_var_wins_over_path(self):
        """显式指定的优先级最高。"""
        fake_dir = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='ffmpeg-env-', ignore_cleanup_errors=True)))
        exe = fake_dir / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        exe.write_bytes(b'stub')
        self._reset()
        with patch.dict(os.environ, {'MOVSP_FFMPEG': str(exe)}):
            self.assertEqual(thumbnails.find_ffmpeg(), str(exe))

    def test_cli_is_matched_to_binary_directory(self):
        """ffprobe 必须与 ffmpeg 同目录，否则时长探测用不了。"""
        if not thumbnails.find_ffmpeg():
            self.skipTest('环境没有 ffmpeg')
        ffmpeg = Path(thumbnails.find_ffmpeg())
        probe = ffmpeg.with_name('ffprobe' + ffmpeg.suffix)
        self.assertTrue(probe.is_file(), '同目录下应有 ffprobe')

    def test_missing_binary_returns_none(self):
        self._reset()
        with patch.dict(os.environ, {'MOVSP_FFMPEG': ''}), \
                patch.object(thumbnails.shutil, 'which', return_value=None), \
                patch.object(thumbnails, '_lookup_dirs', return_value=((), ())):
            self.assertIsNone(thumbnails.find_ffmpeg())
            self.assertFalse(thumbnails.available())

    def test_failure_is_not_cached(self):
        """找不到时不能缓存失败：用户装完 ffmpeg 应该不重启就能用。"""
        self._reset()
        with patch.dict(os.environ, {'MOVSP_FFMPEG': ''}), \
                patch.object(thumbnails.shutil, 'which', return_value=None), \
                patch.object(thumbnails, '_lookup_dirs', return_value=((), ())):
            self.assertIsNone(thumbnails.find_ffmpeg())
        # 第二次模拟「已经装好了」
        fake = '/usr/local/bin/ffmpeg'
        with patch.dict(os.environ, {'MOVSP_FFMPEG': ''}), \
                patch.object(thumbnails.shutil, 'which', return_value=fake):
            self.assertEqual(thumbnails.find_ffmpeg(), fake)


class IsolationTests(unittest.TestCase):
    """缩略图只写 data 目录，绝不碰影片目录。

    这是与刮削共存的前提：Emby 认 `{stem}-poster.jpg` 这类文件名，
    缩略图若落到影片目录会被当成海报，导致「已有海报就跳过下载」。
    """

    def test_thumbnail_dir_lives_under_data(self):
        from movsp.storage import DATA_DIR
        self.assertTrue(str(thumbnails.THUMBNAIL_DIR).startswith(str(DATA_DIR)),
                        '缩略图必须存在 data 目录下')
        self.assertEqual(thumbnails.THUMBNAIL_DIR.name, 'thumbnails')

    def test_path_is_not_in_film_directory(self):
        path = thumbnails.thumbnail_path('film-abc', 16)
        self.assertEqual(path.name, '16.jpg')
        self.assertEqual(path.parent.name, 'film-abc')
        self.assertNotIn('-poster', path.name, '不能生成 Emby 海报名')
        self.assertNotIn('poster', path.name, '不能占用海报文件名')

    def test_filename_is_plain_second(self):
        """文件名只用秒数：档位固定，同一影片永远同一张图。"""
        for second in (1, 8, 128):
            self.assertEqual(thumbnails.thumbnail_path('x', second).name, f'{second}.jpg')


class CaptureTests(unittest.TestCase):
    """真实调用 ffmpeg，没有 ffmpeg 时整组跳过。"""

    def setUp(self):
        if not thumbnails.available():
            self.skipTest('环境没有 ffmpeg')
        self.media = Path(self.enterContext(
            tempfile.TemporaryDirectory(prefix='thumb-', ignore_cleanup_errors=True)))

    def _make_video(self, seconds=30):
        ffmpeg = thumbnails.find_ffmpeg()
        video = self.media / f'sample-{seconds}.mp4'
        subprocess.run([ffmpeg, '-y', '-loglevel', 'error',
                        '-f', 'lavfi', '-i',
                        f'testsrc=size=640x360:rate=25:duration={seconds}',
                        '-pix_fmt', 'yuv420p', str(video)], check=True)
        return video

    def test_capture_creates_jpeg(self):
        video = self._make_video()
        out = thumbnails.capture('film-1', str(video), 30)
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 1000)
        self.assertEqual(out.suffix, '.jpg')
        data = out.read_bytes()
        self.assertTrue(data.startswith(b'\xff\xd8'), '应是 JPEG（以 FFD8 开头）')

    def test_cache_is_reused(self):
        video = self._make_video()
        first = thumbnails.capture('film-2', str(video), 30)
        self.assertIsNotNone(first)
        again = thumbnails.existing_thumbnail('film-2', 30)
        self.assertEqual(again, first, '第二次应直接命中缓存')

    def test_probe_duration(self):
        video = self._make_video(seconds=15)
        self.assertEqual(thumbnails.probe_duration(str(video)), 15)

    def test_missing_source_returns_none(self):
        self.assertIsNone(thumbnails.capture('film-x', str(self.media / 'nope.mp4'), 10))

    def test_no_ffmpeg_degrades_silently(self):
        """没有 ffmpeg 时不报错、不生成文件，前端保持原占位。"""
        video = self._make_video()
        with patch.object(thumbnails, 'find_ffmpeg', return_value=None):
            self.assertFalse(thumbnails.available())
            self.assertIsNone(thumbnails.capture('film-3', str(video), 30))
            self.assertEqual(thumbnails.probe_duration(str(video)), 0)

    def test_output_format_not_inferred_from_part_suffix(self):
        """回归：临时文件必须带 .jpg 后缀。

        ffmpeg 靠扩展名推断封装格式，写成 xxx.jpg.part 会报
        "Unable to choose an output format" 导致截图永远失败。
        """
        video = self._make_video()
        out = thumbnails.capture('film-4', str(video), 30)
        self.assertIsNotNone(out, '截图不应失败')
        leftovers = list(out.parent.glob('*.part')) + list(out.parent.glob('*.tmp.jpg'))
        self.assertEqual(leftovers, [], '不应残留临时文件')


if __name__ == '__main__':
    unittest.main()
