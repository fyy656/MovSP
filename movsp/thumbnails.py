"""视频预览缩略图：从影片里截一帧当封面。

未刮削的影片没有海报，卡片上只有番号文字。这里用 ffmpeg 从视频里截一帧，
让媒体库像播放器一样显示画面预览。

设计要点（参考 JavBoss 的做法）：
  * 时间点固定档位：128/63/32/16/8/4/2/1 秒，取不超过时长的最大者。
    档位固定才能用秒数当缓存文件名，同一影片永远同一张图，缓存不会失效。
  * 落盘缓存：`data/thumbnails/<film_id>/<秒>.jpg`，命中直接返回。
  * ffmpeg 不可用时静默降级，保持原有占位，不影响任何现有功能。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from .storage import DATA_DIR

# 截图时间点（秒）。从靠后往前取，尽量避开片头黑场与标题卡。
SCREENSHOT_SECONDS = (128, 63, 32, 16, 8, 4, 2, 1)

# 不知道时长时的默认档位：ffmpeg 超出时长会输出最后一帧，不会失败
FALLBACK_SECOND = 8

THUMBNAIL_DIR = DATA_DIR / "thumbnails"

# ffmpeg 的查找顺序（找到即用）：
#   1. 环境变量 MOVSP_FFMPEG —— 用户显式指定，优先级最高
#   2. PATH
#   3. 各平台常见安装目录（不要求用户改 PATH）
_WINDOWS = os.name == "nt"


def _extra_ffmpeg_dirs() -> list[Path]:
    """不依赖 PATH 的常见安装位置。"""
    dirs: list[Path] = []
    home = Path.home()
    if _WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData/Local"))
        dirs += [
            # winget install Gyan.FFmpeg
            local / "Microsoft/WinGet/Packages",
            # scoop install ffmpeg
            home / "scoop/shims",
            # choco install ffmpeg
            Path(os.environ.get("ProgramData") or "C:/ProgramData") / "chocolatey/bin",
            # 手动解压到常见位置
            Path("C:/ffmpeg/bin"),
            Path("C:/Program Files/ffmpeg/bin"),
            local / "Programs/ffmpeg/bin",
        ]
    else:
        dirs += [
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            Path("/snap/bin"),
        ]
    return dirs


_lookups: tuple[tuple[Path, ...], tuple[Path, ...]] = (tuple(), tuple())
_lookup_built = False


def _lookup_dirs() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """返回 (精确目录清单, 需要递归搜索的包目录)。

    递归只在 WinGet/Scoop 这类「版本号套在中间」的目录里做，
    避免每次启动扫描整个磁盘。
    """
    global _lookup_built, _lookups
    if _lookup_built:
        return _lookups
    exact: list[Path] = []
    recursive: list[Path] = []
    for path in _extra_ffmpeg_dirs():
        if not path.is_dir():
            continue
        # winget 的包目录下面还有 <包名>/<版本>/bin/ffmpeg.exe
        if path.name == "Packages" and "WinGet" in str(path):
            recursive.append(path)
        else:
            exact.append(path)
    _lookups = (tuple(exact), tuple(recursive))
    _lookup_built = True
    return _lookups


_lock = threading.Lock()
_resolved: dict[str, str | None] = {}
_busy: set[str] = set()

_EXE = "ffmpeg.exe" if _WINDOWS else "ffmpeg"


def _probe_dir(directory: Path) -> str | None:
    candidate = directory / _EXE
    try:
        if candidate.is_file():
            return str(candidate)
    except OSError:
        pass
    return None


def find_ffmpeg() -> str | None:
    """定位 ffmpeg，找不到返回 None（调用方据此降级）。

    找不到时下一次调用会重新查找，这样用户装完 ffmpeg 不必重启服务。
    """
    with _lock:
        if _resolved.get("ffmpeg"):
            return _resolved["ffmpeg"]

    found: str | None = None

    # 1) 用户显式指定
    custom = str(os.environ.get("MOVSP_FFMPEG") or "").strip()
    if custom:
        candidate = Path(custom)
        if candidate.is_file():
            found = str(candidate)
        elif candidate.is_dir():
            found = _probe_dir(candidate)

    # 2) PATH
    if not found:
        found = shutil.which("ffmpeg")

    # 3) 常见安装目录
    if not found:
        exact, recursive = _lookup_dirs()
        for directory in exact:
            found = _probe_dir(directory)
            if found:
                break
        if not found:
            for base in recursive:
                try:
                    for candidate in base.rglob(_EXE):
                        found = str(candidate)
                        break
                except OSError:
                    continue
                if found:
                    break

    with _lock:
        # 只缓存成功结果：失败留给下次重试（用户可能刚装完）
        _resolved["ffmpeg"] = found
    return found


def available() -> bool:
    """当前环境能否生成缩略图。"""
    return bool(find_ffmpeg())


def pick_second(duration_sec: float | int | None) -> int:
    """按 JavBoss 的档位策略选时间点。

    时长未知（未刮削的影片 runtime=0）时返回默认档位：
    ffmpeg 的 -ss 超出实际时长会输出最后一帧，仍然能拿到画面。
    """
    try:
        duration = int(float(duration_sec or 0))
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return FALLBACK_SECOND
    for candidate in SCREENSHOT_SECONDS:
        if duration >= candidate:
            return candidate
    return SCREENSHOT_SECONDS[-1]


def thumbnail_path(film_id: str, second: int) -> Path:
    return THUMBNAIL_DIR / str(film_id) / f"{int(second)}.jpg"


def existing_thumbnail(film_id: str, duration_sec: float | int | None) -> Path | None:
    """已有缓存就返回路径；否则 None。"""
    path = thumbnail_path(film_id, pick_second(duration_sec))
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        pass
    return None


def capture(film_id: str, video_path: str, duration_sec: float | int | None,
            second: int | None = None) -> Path | None:
    """从视频截一帧并缓存。失败返回 None，不抛异常。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    source = Path(str(video_path or ""))
    if not source.is_file():
        return None

    target_second = int(second) if second is not None else pick_second(duration_sec)
    target = thumbnail_path(film_id, target_second)
    if target.is_file() and target.stat().st_size > 0:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    # 输出文件必须带 .jpg 后缀：ffmpeg 靠扩展名推断封装格式，
    # 直接写 xxx.jpg.part 会报 "Unable to choose an output format"。
    temporary = target.with_name(target.stem + ".tmp.jpg")
    args = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        # -ss 放在 -i 之前：快速定位，不用解码前面所有帧
        "-ss", str(target_second),
        "-i", str(source),
        "-map", "0:v:0", "-frames:v", "1",
        # 统一缩到 720px 宽，卡片够用且省磁盘
        "-vf", "scale=720:-2",
        "-q:v", "3",
        str(temporary),
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            return None
        temporary.replace(target)
        return target
    except (OSError, subprocess.SubprocessError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def ensure_thumbnail(film_id: str, video_path: str,
                     duration_sec: float | int | None) -> Path | None:
    """取缩略图：命中缓存直接返回，否则现截一张。"""
    cached = existing_thumbnail(film_id, duration_sec)
    if cached:
        return cached
    return capture(film_id, video_path, duration_sec)


def ensure_async(film_id: str, video_path: str, duration_sec: float | int | None) -> None:
    """后台生成缩略图，不阻塞请求。

    同一部影片同时只跑一个任务，避免滚动列表时反复起 ffmpeg。
    """
    if not available():
        return
    if existing_thumbnail(film_id, duration_sec):
        return
    key = str(film_id)
    with _lock:
        if key in _busy:
            return
        _busy.add(key)

    def worker() -> None:
        try:
            capture(film_id, video_path, duration_sec)
        finally:
            with _lock:
                _busy.discard(key)

    threading.Thread(target=worker, name=f"thumb-{key}", daemon=True).start()


def probe_duration(video_path: str) -> int:
    """用 ffprobe 探测时长（秒）。探测失败返回 0。

    未刮削的影片 NFO 里没有 runtime，靠这个拿到真实时长，
    才能把截图点选在画面好看的位置。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return 0
    probe = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    if not probe.is_file():
        probe = Path(shutil.which("ffprobe") or "")
    if not probe.is_file():
        return 0
    source = Path(str(video_path or ""))
    if not source.is_file():
        return 0
    try:
        result = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(source)],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            return 0
        return max(0, int(float((result.stdout or "0").strip() or 0)))
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0
