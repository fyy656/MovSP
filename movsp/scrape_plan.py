"""刮削查询推导：从影片目录/文件/NFO 里得出「用什么去搜」。

JAV 路径用番号，标题路径用标题（+ 年份）。两者互斥，见 resolve_path()。

标题的取值优先级（D2 已确认）：
    NFO <title> → NFO <originaltitle> → 目录名 → 文件名（清洗后）

目录名只有在「该目录只承载一部影片」时才能用 —— 散装 mp4 直接放在
媒体库根目录或共享目录里时，父目录名是无意义的（"电影"、"待整理"）。
"""
from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "extract_title", "extract_year", "detect_query", "clean_filename",
    "directory_is_single_film", "detect_series_query", "NOISE_PATTERN",
]

# 技术噪声：分辨率、编码、音轨、片源、发布标记
NOISE_PATTERN = re.compile(
    r"(?ix)(?:"
    r"\b(?:"
    # 分辨率 / 动态范围
    r"2160p|1080p|720p|480p|576p|4k|uhd|8k|"
    r"hdr10\+|hdr10|hdr|dv|dovi|dolby[.\s]?vision|sdr|"
    # 片源与容器
    r"bluray|blu-?ray|bdrip|brrip|bdremux|remux|dvdrip|dvd5|dvd9|"
    r"web-?dl|webrip|web|hdtv|hdrip|hd-?dvd|"
    # 视频编码（含 H.264 / H 265 这类带点的写法由下面的点号处理）
    r"x264|x265|hevc|avc|xvid|divx|av1|vp9|"
    # 音频编码与声道
    r"aac|ac3|eac3|ddp?|dts(?:-?hd)?|truehd|atmos|flac|mp3|opus|lpcm|pcm|"
    # 帧率 / 位深 / 扫描方式
    r"\d{2,3}\s?fps|10bit|8bit|hi10p|interlaced|progressive|"
    # 发布标记
    r"proper|repack|extended|uncut|unrated|imax|remastered|remaster|directors?[.\s]?cut|"
    # 语言 / 字幕标记（Mandarin、Cantonese 等常出现在文件名尾部）
    r"multi[\s-]?sub|multi|dual[.\s]?audio|"
    r"chinese|chs|cht|eng|jpn|kor|"
    r"mandarin|cantonese|japanese|english|korean|taiwanese|"
    # 其它
    r"complete|limited|internal|retail|dubbed|subbed"
    r")\b"
    # 音轨格式的尾部声道标记：DTS-HD.MA.5.1 / AC3.2.0
    r"|(?<=[.\s])\d\.\d(?![\d.])"
    r")"
)

# 年份用括号或明确的分隔符包起来，避免把 1917、2012 这类片名当成年份。
# 注意：整段就是 4 位数字时**不算**年份（"1917" 是片名）。
_YEAR_PATTERNS = [
    # (2014) / [2014] / 2014 两侧都有分隔符
    re.compile(r"[\(\[\{]((?:19|20)\d{2})[\)\]\}]"),
    re.compile(r"[.\s_-]((?:19|20)\d{2})[.\s_-]"),
    # 结尾年份：Name.2019 / Name (2019) —— 前面必须有分隔符
    re.compile(r"[.\s_\-\(\[\{]((?:19|20)\d{2})$"),
]

# 分片后缀：CD1 / -cd2 / part3
_PART_SUFFIX = re.compile(r"(?i)[.\s_-]*(?:cd|part|disc|disk)\s*\d+$")


# 发布组后缀：文件名最后一段 `-XXX` 基本就是发布组（-ZmWeb / -QHstudIo / -RARBG）。
# 前面必须已有分隔符，避免把「Star-Wars」这种正文里的连字符吃掉。
_RELEASE_GROUP = re.compile(r"[\s._]+[-_][A-Za-z][A-Za-z0-9]{2,}$")
# 结尾的裸发布组（形如 [SubGroup]）
_TRAILING_BRACKET_GROUP = re.compile(r"\s*[\[\(【]\s*[^\]\)】]{1,20}\s*[\]\)】]\s*$")
# 首尾不成对的方括号/圆括号（PT 站常见「[中文名].English.Name」的写法）
_LEADING_OPEN_BRACKET = re.compile(r"^\s*[\[\(]\s*")
_TRAILING_CLOSE_BRACKET = re.compile(r"\s*[\]\)]\s*$")


def clean_filename(stem: str) -> str:
    """把文件名清洗成可搜索的标题。

    Star.Wars.1977.2160p.BluRay.x265.HDR.mkv -> Star Wars 1977
    飞驰人生2.Pegasus.2.2024.WEB-DL.2160p.HEVC.AAC-ZmWeb -> 飞驰人生2 Pegasus 2 2024
    """
    text = str(stem or "").strip()
    if not text:
        return ""
    text = _PART_SUFFIX.sub("", text)
    # PT 站常见 "[中文名].English.Name.2022..."：先把开头孤立的 "["
    # 转成空格，否则后面的中文名会和 "]" 连在一起留下残括号
    text = _LEADING_OPEN_BRACKET.sub(" ", text)
    text = _TRAILING_CLOSE_BRACKET.sub(" ", text)
    # H.264 / H.265 / H 264 这类带点的编码写法先统一掉（词表里无法写点号）
    text = re.sub(r"(?i)\bh[\s.]?(264|265)\b", " ", text)
    # 处理形如 "Title (2014)" / "Title [2014]"：保留年份，去掉括号本身
    text = re.sub(r"[\(\[\{]\s*((?:19|20)\d{2})\s*[\)\]\}]", r" \1 ", text)
    text = NOISE_PATTERN.sub(" ", text)
    # MA / DD+ / 5.1 这类音轨后缀单独清理（带边界的词表容易漏）
    text = re.sub(r"(?i)(?<![a-z])(?:ma|dd\+?|lpcm|pcm)(?![a-z])", " ", text)
    # 音轨规格：DDP2.0 / DDP5.1 / AC3.2.0 / TrueHD.7.1 —— 编码后紧跟数字的写法，
    # 词表里的 \b 边界对 "DDP2" 不成立，单独去掉
    text = re.sub(r"(?i)\b(?:ddp?|ac3|eac3|dts|truehd|aac|flac)(?:\d(?:\.\d)?)", " ", text)
    # 把点、下划线统一成空格（先做，发布组规则才能看到分隔符）
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    # 发布组后缀：-ZmWeb / -QHstudIo 之类（常见于 PT 站点资源名）
    text = _RELEASE_GROUP.sub(" ", text)
    # 反复去掉空的成对括号，直到稳定（形如 "[ ]" "()" ）
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", text)
        text = re.sub(r"\s+", " ", text)
    text = text.strip(" -_·[](){}")
    # 清掉首尾不配对的括号（例如 "Interstellar (2014" 的残余）
    text = re.sub(r"[\(\[\{]\s*$", "", text)
    text = re.sub(r"^\s*[\)\]\}]", "", text)
    # 正文里残留的孤立括号：中文名后紧跟 "]" 的情况（"[中文名] English"）
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s*[\]\)\}]", " ", text)
    text = re.sub(r"[\[\(\{]\s*(?=[\u4e00-\u9fff])", " ", text)
    # 剩下的孤立连字符（原发布组分隔符）
    text = re.sub(r"\s*-\s*$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_year(*candidates: str) -> int | None:
    """从若干候选文本里取一个合理的发行年份。

    支持：NFO 的 ISO 日期（2021-05-01）、纯年份字段（NFO <year>2021</year>）、
    以及目录/文件名里的 (2021) / .2021. 形式。

    纯 4 位数字（"1917"、"2012"）在文件名/目录名里通常是**片名**，
    因此裸年份只在 `bare_ok=True`（来自 NFO 的 <year> 字段）时才认。
    """
    return _extract_year(candidates, bare_ok=False)


def _extract_year(candidates, bare_ok: bool = False) -> int | None:
    for index, text in enumerate(candidates):
        if not text:
            continue
        raw = str(text)
        if bare_ok or index == 0:
            match = _ISO_DATE.match(raw)
            if match:
                year = int(match.group(1))
                if 1900 <= year <= 2100:
                    return year
        if bare_ok:
            match = _BARE_YEAR.match(raw)
            if match:
                year = int(match.group(1))
                if 1900 <= year <= 2100:
                    return year
        for pattern in _YEAR_PATTERNS:
            match = pattern.search(raw)
            if match:
                year = int(match.group(1))
                if 1900 <= year <= 2100:
                    return year
    return None


def directory_is_single_film(directory: Path, root_path: str | None = None) -> bool:
    """目录名能否当作影片标题。

    条件：不是媒体库根目录，且目录内只有一组视频（多分片合并后仍算一部）。
    """
    try:
        directory = Path(directory).resolve()
    except OSError:
        return False
    if root_path:
        try:
            if directory == Path(root_path).resolve():
                return False
        except OSError:
            pass
    groups = _video_groups(directory)
    return bool(groups) and len(groups) == 1


def _video_groups(directory: Path) -> list[list[Path]]:
    """目录直属视频按分片规则分组；无法枚举时返回空。"""
    try:
        from .library_store import _video_extensions
        extensions = _video_extensions()
    except Exception:  # noqa: BLE001
        extensions = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".flv", ".webm", ".strm"}
    try:
        videos = [item for item in sorted(directory.iterdir())
                  if item.is_file() and item.suffix.lower() in extensions]
    except OSError:
        return []
    if not videos:
        return []
    try:
        from .library_store import _group_videos
        return _group_videos(videos)
    except Exception:  # noqa: BLE001 - 分组失败时退回逐文件
        return [[item] for item in videos]


_TRAILING_YEAR = re.compile(r"[\s._\-\(\[\{]((?:19|20)\d{2})[\)\]\}]?\s*$")
# 标题中间带年份：飞驰人生2 Pegasus 2 2024 → 去掉 2024（年份已单独传参）
_ANY_YEAR = re.compile(r"[\s._\-\(\[\{]((?:19|20)\d{2})(?=[\s._\-\)\]\}]|$)")
# 剧集目录里常见的季/集标记：S01 / S01E02 / Season 1 / 第1季
_SEASON_MARKER = re.compile(
    r"(?i)(?:^|[\s._\-\[\(])"
    r"(?:s\d{1,2}(?:e\d{1,4})?|season[\s._\-]*\d{1,2}|第\s*[0-9一二三四五六七八九十]+\s*[季部])"
    r"(?=[\s._\-\)\]/]|$)")

# 纯年份（NFO <year>2021</year> 这类字段）
_BARE_YEAR = re.compile(r"^\s*((?:19|20)\d{2})\s*$")
# ISO 日期：2021-05-01 / 2021/05/01
_ISO_DATE = re.compile(r"^\s*((?:19|20)\d{2})[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])\s*$")


def strip_year(text: str) -> str:
    """去掉标题里的年份。

    年份已经单独提取成 `year` 参数，标题里再带一遍会让搜索命中率为零
    （TMDb 按标题精确匹配，"三体 2023" 搜不到 "三体"）。

    注意：整段就是 4 位数字时（"1917"、"2012"）那是**片名**，不能剥。
    """
    raw = str(text or "")
    stripped = raw.strip()
    if _BARE_YEAR.match(stripped):
        return stripped  # 纯数字片名，如 1917
    cleaned = _ANY_YEAR.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_.·[](){}")
    return cleaned or stripped


def extract_title(nfo: dict | None, directory: Path | None, video: Path,
                  root_path: str | None = None) -> tuple[str, str]:
    """按优先级推导搜索标题，返回 (title, source)。

    source 用于界面提示：nfo / nfo_original / directory / filename
    """
    nfo = nfo or {}
    candidates = [
        ("nfo", str(nfo.get("title") or "").strip()),
        ("nfo_original", str(nfo.get("original_title") or "").strip()),
    ]
    for source, value in candidates:
        if value:
            return value, source

    if directory is not None and directory_is_single_film(Path(directory), root_path):
        name = Path(directory).name.strip()
        cleaned = strip_year(clean_filename(name))
        if cleaned:
            return cleaned, "directory"

    stem = Path(video).stem if video else ""
    return strip_year(clean_filename(stem)), "filename"


def detect_query(*, nfo: dict | None = None, directory: Path | str | None = None,
                 video: Path | str | None = None, root_path: str | None = None,
                 code: str = "") -> dict:
    """给出一次刮削的查询上下文。

    返回:
        {
          "kind": "code" | "title",
          "code": "SSIS-001",              # kind=code 时
          "title": "流浪地球",              # kind=title 时
          "year": 2019,
          "title_source": "directory",
          "external_id": {"tmdb": "...", "imdb": "..."},
        }
    """
    nfo = nfo or {}
    external = {}
    for key in ("tmdb", "imdb"):
        value = str(nfo.get(key) or nfo.get(f"{key}_id") or "").strip()
        if value:
            external[key] = value

    code = str(code or nfo.get("dvdid") or "").strip()
    if code:
        return {"kind": "code", "code": code, "title": "", "year": None,
                "title_source": "", "external_id": external}

    directory_path = Path(directory) if directory else None
    video_path = Path(video) if video else (directory_path or Path("unknown"))
    title, source = extract_title(nfo, directory_path, video_path, root_path)
    # 年份：NFO 优先，其次目录名，最后文件名
    year = extract_year(
        str(nfo.get("release_date") or ""),
        directory_path.name if directory_path and source == "directory" else "",
        video_path.stem if source == "filename" else "",
    )
    return {
        "kind": "title",
        "code": "",
        "title": title,
        "year": year,
        "title_source": source,
        "external_id": external,
    }


def detect_series_query(*, nfo: dict | None = None, series_dir: Path | str | None = None,
                        root_path: str | None = None,
                        external_id: dict | None = None) -> dict:
    """剧集查询上下文。

    剧集条目本身没有视频文件，标题来自 tvshow.nfo 或剧集目录名。
    与电影的区别：`kind='series'`，且不要求年份（剧可能跨年播出）。
    """
    nfo = nfo or {}
    directory = Path(series_dir) if series_dir else None

    external: dict[str, str] = {}
    for key in ("tmdb", "imdb"):
        value = str(nfo.get(key) or nfo.get(f"{key}_id") or "").strip()
        if value:
            external[key] = value
    for key in ("tmdb", "imdb"):
        value = str((external_id or {}).get(key) or "").strip()
        if value:
            external[key] = value

    # 剧名优先级：tvshow.nfo 标题 → 目录名（清洗后）
    title = str(nfo.get("title") or "").strip()
    source = "nfo" if title else ""
    if not title:
        title = str(nfo.get("original_title") or "").strip()
        source = "nfo_original" if title else ""
    if not title and directory is not None:
        # 剧集目录名常带季标记（老舅.Uncle.2025.S01.2160p...），
        # 搜索时必须去掉：TMDb 按剧名匹配，"老舅 Uncle S01" 搜不到。
        raw_name = _SEASON_MARKER.sub(" ", directory.name)
        title = strip_year(clean_filename(raw_name))
        source = "directory"
    # 年份：NFO 首播日期 → NFO <year> → 目录名（premiered 是剧集 NFO 的标准字段）
    nfo_year = str(nfo.get("year") or "").strip()
    year = extract_year(
        str(nfo.get("premiered") or nfo.get("release_date") or ""),
        directory.name if directory is not None else "")
    if year is None and nfo_year:
        year = _extract_year((nfo_year,), bare_ok=True)
    return {
        "kind": "series",
        "code": "",
        "title": title,
        "year": year,
        "title_source": source,
        "external_id": external,
        "season": 0,
    }
