"""刮削器（爬虫）注册表 —— 「搭积木」模式的后端积木库。

设计目标
========
* 后台预先写好各种刮削器：内置站点爬虫（vendor/JavSP/javsp/web/*.py）+
  随本程序发布的「插件」刮削器（PLUGIN_SOURCES，例如带 API Key 的 TMDb）。
* 每块积木都有稳定的 id（= 引擎里的模块名）、展示名、说明、类型、
  推荐分类、以及可编辑的配置项（settings_schema）。
* 用户在管理页只做「启用 / 停用 + 编辑配置」，在预设页勾选要用的积木，
  不必再接触原始模块名。
* 持久化放在 data/crawler-settings.json（沿用旧的 crawler-settings 文件，
  向后兼容 disabled_built_ins）。该文件同时会作为运行时配置通过环境变量
  MOVSP_SCRAPER_SETTINGS 传给引擎子进程，插件刮削器直接读取。

新增一块积木只需两步：在 CATALOG 加一条定义，在 PLUGIN_SOURCES 加源码
（内置站点爬虫则无需源码，引擎自带）。插件源码遵循引擎约定：
模块级 ``parse_data(movie: MovieInfo)``，就地填充字段。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .timeutils import now_iso
from .storage import env_compat

__all__ = [
    "CATALOG", "PLUGIN_SOURCES", "CAPABILITIES", "PATHS",
    "STATUS_STATES", "HTTP_STATUS_HINTS",
    "catalog", "catalog_entry", "available_names", "resolve_path",
    "disabled_names", "is_enabled", "set_enabled", "get_settings",
    "public_settings", "update_settings", "get_overrides", "set_overrides",
    "status_of", "all_statuses", "set_status", "set_statuses",
    "make_status", "status_from_error", "evaluate_result",
    "plugin_source", "ensure_plugin_files", "save_source", "restore_source",
    "custom_source", "delete_source",
    "settings_file_path", "runtime_env", "reserved_names", "validate_name",
]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SECRET_KEYS = {"api_key", "apikey", "token", "password", "secret", "key"}


# ---------------------------------------------------------------------------
# 1. 积木定义
# ---------------------------------------------------------------------------

def _field(key: str, label: str, *, secret: bool = False, required: bool = False,
           default: str = "", placeholder: str = "", help_text: str = "") -> dict:
    return {
        "key": key, "label": label, "type": "password" if secret else "text",
        "secret": secret, "required": required, "default": default,
        "placeholder": placeholder, "help": help_text,
    }


TMDB_SETTINGS_SCHEMA = [
    _field("api_key", "TMDb API Key", secret=True, required=True,
           placeholder="v3 API Key（32 位十六进制）",
           help_text="在 TMDb 账号设置 → API 中申请。密钥只保存在本机 data/crawler-settings.json，不会写进预设或日志。"),
    _field("base_url", "API 地址", default="https://api.tmdb.org/3",
           placeholder="https://api.tmdb.org/3",
           help_text="可换成可用镜像。只填域名也可以（会自动补 https:// 与 /3），例如 api.tmdb.org。"),
    _field("image_base", "图片地址", default="https://image.tmdb.org/t/p",
           placeholder="https://image.tmdb.org/t/p",
           help_text="海报 / 背景图前缀。只填域名也可以，会自动补 https://。"),
    _field("language", "语言", default="zh-CN", placeholder="zh-CN / en-US / ja-JP"),
]

LOCAL_JSON_SETTINGS_SCHEMA = [
    _field("json_path", "资料 JSON 路径", required=True,
           placeholder="例如 D:\\metadata\\movies.json",
           help_text='本地 JSON，形如 {"ABC-123": {"title": "...", "cover": "https://..."}}。番号作为查询键。'),
]


# 能力（capability）—— 刮削器「能做什么」。
#
# 这是**后端内部概念**，用户不需要理解，也不能在界面上修改：
#   * 它只用于「可用性检测」挑样本、以及任务构建时的兼容层；
#   * 用户选刮削器时，看到的就是一个扁平列表，不按能力分组。
#
# 真实抓取时所有选中的刮削器会**并发**执行，结果按顺序逐字段合并
# （上游 info_summary 的既有行为：前一个来源拿到的字段优先）。
CAPABILITIES: dict[str, dict] = {
    "movie_by_code": {"label": "番号刮削", "description": "按番号取影片资料"},
    "movie_by_title": {"label": "标题刮削", "description": "按标题搜索影片"},
    "tv_by_title": {"label": "剧集刮削", "description": "按标题搜索电视剧"},
    "actress_by_code": {"label": "演员（番号）", "description": "按番号取演员信息"},
    "actress_by_name": {"label": "演员（姓名）", "description": "按演员名取资料"},
    "series_by_code": {"label": "系列", "description": "按番号取系列信息"},
    "genre_catalog": {"label": "分类目录", "description": "拉取站点分类列表"},
}


def _def(name: str, display_name: str, kind: str, scraper_type: str,
         provides: list[str], fields: list[str], description: str,
         homepage: str = "", sample: str = "", sample_capability: str = "",
         settings_schema: list[dict] | None = None,
         enabled_by_default: bool = True) -> dict:
    """登记一块积木。

    provides:  能力列表（CAPABILITIES 的键）；仅后端内部使用
    fields:    能产出哪些 MovieInfo 字段（仅用于展示）
    sample:    可用性检测使用的查询样本；为空表示无法检测
    sample_capability: 该样本验证的是哪一个能力（默认取 provides 的第一个）
    """
    for capability in provides:
        if capability not in CAPABILITIES:
            raise ValueError(f"未知能力 {capability!r}（{name}）")
    return {
        "name": name,
        "display_name": display_name,
        "kind": kind,                       # built_in | plugin | custom
        "type": scraper_type,               # site | api | local
        "provides": provides,               # 能力：调度依据
        "fields": fields,                   # 可产出的资料字段
        "description": description,
        "homepage": homepage,
        "sample": sample,
        "sample_capability": sample_capability or (provides[0] if provides else ""),
        "settings_schema": settings_schema or [],
        "enabled_by_default": enabled_by_default,
    }


# 内置站点爬虫：源码由 vendor/JavSP 提供，这里只登记展示信息、能力与默认开关。
# sample 是可用性检测的真实查询样本；不同站点的番号格式不同，必须分别指定。
CATALOG: list[dict] = [
    _def("airav", "AirAV", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress", "plot", "preview_pics"],
         description="AirAV 影片资料，JSON 接口，含剧情、演员与预览图。",
         homepage="https://www.airav.wiki", sample="SSIS-001"),
    _def("avsox", "AVSOX", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress", "duration"],
         description="AVSOX 镜像站，适合无码与 FC2 影片，封面质量较好。",
         homepage="https://avsox.click", sample="030919_047"),
    _def("avwiki", "AVWiki", "built_in", "site",
         provides=["actress_by_name", "movie_by_code"],
         fields=["title", "cover", "actress", "series"],
         description="AVWiki 聚合资料，收录演员与系列信息，可按演员名查询。",
         homepage="https://av-wiki.net", sample="三上悠亜", sample_capability="actress_by_name"),
    _def("dl_getchu", "DL Getchu", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="Getchu 同人 / 动画影片资料。", sample="GETCHU-123456"),
    _def("fanza", "FANZA", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "plot", "genre", "actress", "duration", "score"],
         description="FANZA（DMM）官方资料，字段最完整；按 CID 查询。",
         homepage="https://www.dmm.co.jp", sample="ssis00127"),
    _def("fc2", "FC2", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress", "score"],
         description="FC2 影片官方资料（JSON 接口）。",
         homepage="https://adult.contents.fc2.com", sample="FC2-1234567"),
    _def("fc2fan", "FC2Fan（本地镜像）", "built_in", "local",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="已关站，需要配置本地镜像目录才能使用；默认停用。",
         sample="FC2-1234567", enabled_by_default=False),
    _def("fc2ppvdb", "FC2PPVDB", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="FC2 PPV 资料库，补充 FC2 影片字段。", sample="FC2-1234567"),
    _def("gyutto", "Gyutto", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre"],
         description="Gyutto 同人影片资料。", sample="GYUTTO-123456"),
    _def("jav321", "Jav321", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress", "score"],
         description="Jav321 聚合站，含制作商与评分。", sample="SSIS-001"),
    _def("javbus", "JavBus", "built_in", "site",
         provides=["movie_by_code", "actress_by_code"],
         fields=["title", "cover", "genre", "actress", "preview_pics"],
         description="JavBus 影片资料，封面与剧照较全。",
         homepage="https://www.seedmm.help", sample="SSIS-001"),
    _def("javdb", "JavDB", "built_in", "site",
         provides=["movie_by_code", "actress_by_code", "series_by_code"],
         fields=["title", "cover", "genre", "actress", "score", "preview_pics"],
         description="JavDB 资料站，类型标签与评分较准，封面可能带水印。",
         homepage="https://javdb.com", sample="SSIS-001"),
    _def("javlib", "JavLibrary", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress", "director", "score"],
         description="JavLibrary 资料，含导演、发行商与用户评分。",
         homepage="https://www.y78k.com", sample="SSIS-001"),
    _def("javmenu", "JavMenu", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="JavMenu 聚合页，常用于 FC2 影片。", sample="FC2-1234567"),
    _def("mgstage", "MGStage", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress", "duration", "score"],
         description="MGStage 官方资料，含时长与评分。", sample="SSIS-001"),
    _def("njav", "NJav", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="NJav 影片资料站。", sample="SSIS-001"),
    _def("prestige", "Prestige", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="Prestige（蚊香社）官方资料。", sample="SSIS-001"),
    _def("arzon", "Arzon", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="Arzon 影片资料。", sample="SSIS-001"),
    _def("arzon_iv", "Arzon IV", "built_in", "site",
         provides=["movie_by_code"],
         fields=["title", "cover", "genre", "actress"],
         description="Arzon 独立影片资料。", sample="SSIS-001"),

    # 随程序发布的插件刮削器（源码见 PLUGIN_SOURCES）。
    _def("tmdb", "TMDb（API）", "plugin", "api",
         provides=["movie_by_title", "tv_by_title"],
         fields=["title", "cover", "big_cover", "plot", "genre", "score",
                 "publish_date", "director", "actress"],
         description="通用影视元数据，按标题查询 TMDb。需要 API Key；用于普通电影与剧集——"
                     "番号类影片请用「番号刮削」的站点爬虫。",
         homepage="https://www.themoviedb.org", sample="流浪地球",
         settings_schema=TMDB_SETTINGS_SCHEMA, enabled_by_default=False),
    _def("local_json", "本地资料表", "plugin", "local",
         provides=["movie_by_code"],
         fields=["title", "cover", "plot", "genre", "actress", "director", "publish_date"],
         description="离线积木：从本机 JSON 文件按番号读取资料，不需要联网，可用于手工补录或兜底。",
         sample="ABC-123", settings_schema=LOCAL_JSON_SETTINGS_SCHEMA,
         enabled_by_default=False),
]

CATALOG_BY_NAME = {item["name"]: item for item in CATALOG}


# ---------------------------------------------------------------------------
# 2. 插件源码（随程序发布，激活时物化到 data/crawlers/ 供引擎按模块名导入）
# ---------------------------------------------------------------------------

_PLUGIN_COMMON = '''
import json
import os

_NAME = {name!r}


def _env(name, default=""):
    """读环境变量，兼容改名前的 JAVSP_WEB_*。

    插件是独立文件，不能 import 主程序。
    """
    value = os.environ.get(name)
    if value:
        return value
    legacy = "JAVSP_WEB_" + name[len("MOVSP_"):] if name.startswith("MOVSP_") else ""
    if legacy:
        value = os.environ.get(legacy)
        if value:
            return value
    return default


def _settings():
    """读取管理页保存的配置（文件路径由 MOVSP_SCRAPER_SETTINGS 传入）。"""
    path = _env("MOVSP_SCRAPER_SETTINGS", "")
    if not path or not os.path.isfile(path):
        return {{}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        value = (data.get("settings") or {{}}).get(_NAME) or {{}}
        return value if isinstance(value, dict) else {{}}
    except (OSError, ValueError):
        return {{}}


def _proxies():
    try:
        from javsp.config import Cfg
        proxy = Cfg().network.proxy_server
        if proxy:
            return {{"http": str(proxy), "https": str(proxy)}}
    except Exception:
        pass
    return None


def _require(cfg, key, label):
    value = str(cfg.get(key) or "").strip()
    if not value:
        raise RuntimeError("%s 未配置%s，请在「刮削器管理」中填写" % (_NAME, label))
    return value


def _normalize_base(value, default, api_version="/3"):
    """把用户填的地址补全成可用的 URL。

    用户常见填法：api.tmdb.org / api.tmdb.org/3 / http://api.tmdb.org/3
    统一补 https:// 前缀；没写版本号的按需补上（api_version 为空则不补）。
    """
    text = str(value or "").strip()
    if not text:
        return default
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    text = text.rstrip("/")
    if api_version:
        version = api_version.strip("/")
        # 已经是 .../3 之类就不重复添加
        if not text.rstrip("/").endswith("/" + version) and not text.endswith("/" + version):
            text = text + "/" + version
    return text


def _query(movie):
    return str(movie.dvdid or movie.cid or "").strip()
'''

_TMDB_BODY = '''
def _image_url(image_base, size, path):
    """按 TMDb 的尺寸规则拼图片地址。

    不用 /original：原图动辄几 MB，下载时常被中断（实测 6 次里失败 1 次），
    而 w1280 只有约 1/4 大小、肉眼几乎无差，还更稳定。
    """
    if not path:
        return ""
    return "%s/%s%s" % (image_base, size, path)


def _pick_result(results, year, date_key="release_date"):
    """在搜索结果里挑最合适的一条。

    有年份时优先同年份；否则取第一条。返回 (item, ambiguous)。
    ambiguous=True 表示有多条且年份无法消歧，调用方可以据此提示用户手选。
    """
    if not results:
        return None, False
    if year:
        for item in results:
            date = str(item.get(date_key) or item.get("release_date") or item.get("first_air_date") or "")
            if date[:4] == str(year):
                return item, False
    return results[0], len(results) > 1


def parse_data(movie: "MovieInfo"):
    """TMDb API 刮削器。

    查询来源（按优先级）：
      1. movie._query_external_id["tmdb"]  —— 已确定 ID，直取，无歧义
      2. movie._query_title                —— 标题路径（普通电影 / 剧集）
      3. movie.dvdid / movie.cid           —— 兜底（番号型也能搜，命中率低）
    """
    import requests

    cfg = _settings()
    api_key = _require(cfg, "api_key", " API Key")
    base = _normalize_base(
        cfg.get("base_url"), default="https://api.themoviedb.org/3", api_version="/3")
    image_base = _normalize_base(
        cfg.get("image_base"), default="https://image.tmdb.org/t/p", api_version="")
    language = str(cfg.get("language") or "zh-CN")

    external = getattr(movie, "_query_external_id", None) or {}
    known_id = str(external.get("tmdb") or "").strip()
    title = str(getattr(movie, "_query_title", "") or "").strip()
    year = getattr(movie, "_query_year", None)
    # 剧集走 /tv 接口；电影走 /movie
    is_series = str(getattr(movie, "_query_kind", "") or "") == "series"
    endpoint = "tv" if is_series else "movie"
    search_path = "/search/tv" if is_series else "/search/movie"
    date_key = "first_air_date" if is_series else "release_date"
    year_param = "first_air_date_year" if is_series else "year"
    query = title or _query(movie)
    if not known_id and not query:
        return

    session = requests.Session()
    if known_id:
        movie_id = known_id
        summary = {}
    else:
        params = {"api_key": api_key, "query": query, "language": language,
                  "include_adult": "true"}
        if year:
            params[year_param] = str(year)
        resp = session.get(base + search_path, params=params, timeout=20, proxies=_proxies())
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or []
        if not results:
            raise RuntimeError("TMDb 未找到匹配%s: %s" % ("剧集" if is_series else "影片", query))
        picked, ambiguous = _pick_result(results, year, date_key=date_key)
        movie_id = picked.get("id")
        summary = picked
        if ambiguous:
            # 不用抛异常：仍然给出最可能的结果，同时把候选记下来供界面提示。
            # 候选带上海报/年份/评分，用户才能分辨同名影片。
            movie._tmdb_candidates = [
                {"tmdb_id": item.get("id"),
                 "title": item.get("title") or item.get("name") or item.get("original_title") or "",
                 "original_title": item.get("original_title") or item.get("original_name") or "",
                 "year": str(item.get(date_key) or "")[:4],
                 "release_date": item.get(date_key) or "",
                 "score": round(float(item.get("vote_average") or 0), 1),
                 "poster": (image_base + "/w200" + item["poster_path"]) if item.get("poster_path") else "",
                 "overview": (item.get("overview") or "")[:160]}
                for item in results[:10] if item.get("id")
            ]

    append = "credits,external_ids,aggregate_credits" if is_series else "credits,external_ids"
    detail = session.get(
        base + "/" + endpoint + "/" + str(movie_id),
        params={"api_key": api_key, "language": language, "append_to_response": append},
        timeout=20, proxies=_proxies(),
    )
    detail.raise_for_status()
    data = detail.json() or {}

    # 剧集指定了季号时，取该季资料覆盖剧级信息。
    # TMDb 一个剧 ID 含全部季，不指定季只会拿到第 1 季的内容。
    season_no = 0
    if is_series:
        try:
            season_no = int(getattr(movie, "_query_season", 0) or 0)
        except (TypeError, ValueError):
            season_no = 0
    season_data = {}
    if is_series and season_no > 0:
        try:
            resp = session.get(
                "%s/tv/%s/season/%s" % (base, movie_id, season_no),
                params={"api_key": api_key, "language": language},
                timeout=20, proxies=_proxies())
            resp.raise_for_status()
            season_data = resp.json() or {}
        except Exception:  # noqa: BLE001 - 季资料取不到时退回剧级信息
            season_data = {}

    # 回写外部 ID：下次按 ID 直取，不再搜索
    movie.tmdb_id = str(movie_id)
    imdb_id = str((data.get("external_ids") or {}).get("imdb_id") or "").strip()
    if imdb_id:
        movie.imdb_id = imdb_id

    movie.url = "https://www.themoviedb.org/%s/%s" % (endpoint, movie_id)
    movie.title = (data.get("name") if is_series else data.get("title")) \
        or summary.get("title") or summary.get("name") or movie.title
    movie.ori_title = data.get("original_name" if is_series else "original_title") or movie.ori_title
    movie.plot = data.get("overview") or movie.plot
    movie.publish_date = data.get(date_key) or movie.publish_date

    # 用季资料覆盖：季名（如「爱情公寓 第五季」）、季简介、季海报、季首播
    if season_data:
        # 季名常是「第 5 季」这种裸季号，单用会丢失剧名，拼上剧名更可读
        season_name = str(season_data.get("name") or "").strip()
        if season_name:
            show_name = str(data.get("name") or "").strip()
            if show_name and season_name not in show_name and show_name not in season_name:
                movie.title = "%s %s" % (show_name, season_name)
            else:
                movie.title = season_name
        season_plot = str(season_data.get("overview") or "").strip()
        if season_plot:
            movie.plot = season_plot
        season_air = str(season_data.get("air_date") or "").strip()
        if season_air:
            movie.publish_date = season_air
        season_poster = season_data.get("poster_path")
        if season_poster:
            movie.cover = _image_url(image_base, "w500", season_poster)
        # 季资料里没有演员，用剧级的补齐（下面仍会走 credits）
    if data.get("vote_average") is not None:
        try:
            movie.score = "%.2f" % float(data["vote_average"])
        except (TypeError, ValueError):
            pass
    if data.get("runtime"):
        movie.duration = str(data["runtime"])
    elif is_series:
        # 剧集没有单集时长；用每集平均时长
        runs = data.get("episode_run_time") or []
        if runs:
            movie.duration = str(runs[0])
    poster = data.get("poster_path") or summary.get("poster_path")
    if poster:
        movie.cover = _image_url(image_base, "w500", poster)
    backdrop = data.get("backdrop_path") or summary.get("backdrop_path")
    if backdrop:
        movie.big_cover = _image_url(image_base, "w1280", backdrop)
    genres = [g.get("name") for g in (data.get("genres") or []) if g.get("name")]
    if genres:
        movie.genre = genres
    credits = data.get("credits") or {}
    directors = [c.get("name") for c in (credits.get("crew") or [])
                 if c.get("job") == "Director" and c.get("name")]
    if directors:
        movie.director = directors[0]
    if is_series:
        # 剧集的演员在 aggregate_credits 里
        series_cast = data.get("aggregate_credits") or {}
        cast = [c.get("name") for c in (series_cast.get("cast") or []) if c.get("name")][:20]
    else:
        cast = [c.get("name") for c in (credits.get("cast") or []) if c.get("name")][:20]
    if cast:
        movie.actress = cast
    companies = [c.get("name") for c in (data.get("production_companies") or []) if c.get("name")]
    if companies and not movie.producer:
        movie.producer = companies[0]


def parse_season(tmdb_id, season, language="zh-CN", image_base=None,
                 api_key=None, base_url=None):
    """取一整季的分集资料（标题 / 简介 / 播出日期 / 时长 / 剧照）。

    一次请求就能拿回整季，写入每集的 <episodedetails> NFO。
    返回 {"season": int, "name": str, "overview": str, "poster": str,
          "episodes": [{episode, title, plot, air_date, runtime, still}, ...]}
    """
    import requests

    if not api_key:
        raise RuntimeError("TMDb 未配置 API Key，请在「刮削器管理」中填写")
    base = _normalize_base(base_url, default="https://api.themoviedb.org/3", api_version="/3")
    image_base = _normalize_base(image_base, default="https://image.tmdb.org/t/p", api_version="")
    resp = requests.get(
        "%s/tv/%s/season/%s" % (base, tmdb_id, season),
        params={"api_key": api_key, "language": language},
        timeout=30, proxies=_proxies())
    resp.raise_for_status()
    data = resp.json() or {}
    episodes = []
    for item in data.get("episodes") or []:
        number = item.get("episode_number")
        if number is None:
            continue
        still = item.get("still_path")
        episodes.append({
            "episode": int(number),
            "title": item.get("name") or "",
            "plot": item.get("overview") or "",
            "air_date": item.get("air_date") or "",
            "runtime": int(item.get("runtime") or 0),
            "still": _image_url(image_base, "w780", still),
        })
    poster = data.get("poster_path")
    return {
        "season": int(season),
        "name": data.get("name") or "",
        "overview": data.get("overview") or "",
        "poster": _image_url(image_base, "w500", poster),
        "episodes": episodes,
    }
'''

_LOCAL_JSON_BODY = '''
def parse_data(movie: "MovieInfo"):
    """离线积木：按番号从本地 JSON 读取资料。"""
    cfg = _settings()
    path = _require(cfg, "json_path", " JSON 路径")
    query = _query(movie)
    if not query:
        return
    with open(path, "r", encoding="utf-8") as fh:
        table = json.load(fh)
    if not isinstance(table, dict):
        raise RuntimeError("本地资料表必须是 {番号: {字段: 值}} 形式")
    record = table.get(query) or table.get(query.upper()) or table.get(query.lower())
    if not isinstance(record, dict):
        raise RuntimeError("本地资料表中没有 %s" % query)
    allowed = {
        "url", "plot", "cover", "big_cover", "genre", "score", "title",
        "ori_title", "magnet", "serial", "actress", "director", "duration",
        "producer", "publisher", "uncensored", "publish_date", "preview_pics",
        "preview_video",
    }
    for key, value in record.items():
        if key in allowed and value not in (None, "", []):
            setattr(movie, key, value)
'''

PLUGIN_SOURCES: dict[str, str] = {
    name: (_PLUGIN_COMMON.format(name=name) + body)
    for name, body in (("tmdb", _TMDB_BODY), ("local_json", _LOCAL_JSON_BODY))
}


# ---------------------------------------------------------------------------
# 3. 存储（data/crawler-settings.json，向后兼容旧的 disabled_built_ins）
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    from .storage import CRAWLER_SETTINGS_FILE
    return CRAWLER_SETTINGS_FILE


def _custom_dir() -> Path:
    from .storage import CUSTOM_CRAWLERS_DIR
    return CUSTOM_CRAWLERS_DIR


def _read_state() -> dict:
    from .storage import read_crawler_settings
    return read_crawler_settings()


def _write_state(state: dict) -> None:
    from .storage import write_crawler_settings
    write_crawler_settings(state)


def settings_file_path() -> str:
    return str(_state_file())


def runtime_env() -> dict[str, str]:
    """传给引擎 / 测试子进程的环境变量（只传路径，不把密钥放进环境）。"""
    return {"MOVSP_SCRAPER_SETTINGS": settings_file_path()}


def validate_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(str(name or "")))


def reserved_names() -> set[str]:
    """不允许用户自定义爬虫占用的名字。"""
    return set(CATALOG_BY_NAME) | set(PLUGIN_SOURCES)


def plugin_source(name: str) -> str | None:
    """返回插件当前生效的源码（优先用户编辑过的文件）。"""
    if name not in PLUGIN_SOURCES:
        return None
    path = _custom_dir() / f"{name}.py"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return PLUGIN_SOURCES[name]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_plugin_files() -> None:
    """把随程序发布的插件源码物化到 data/crawlers/，让引擎可按模块名导入。

    用 <name>.py.revision 记录「上次写入的源码哈希」：
      文件不存在            → 写入源码 + revision
      文件与 revision 一致  → 用户没改过，可安全更新
      文件被用户改过        → 保留用户版本，不覆盖
    """
    directory = _custom_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in PLUGIN_SOURCES.items():
        path = directory / f"{name}.py"
        revision = directory / f"{name}.py.revision"
        shipped = _sha(source)
        try:
            if not path.is_file():
                path.write_text(source, encoding="utf-8")
                revision.write_text(shipped, encoding="utf-8")
                continue
            if not revision.is_file():
                continue  # 用户自建或手工修改过，永不覆盖
            if revision.read_text(encoding="utf-8").strip() != _sha(path.read_text(encoding="utf-8")):
                continue  # 用户编辑过
            current = path.read_text(encoding="utf-8")
            if current != source:
                path.write_text(source, encoding="utf-8")
                revision.write_text(shipped, encoding="utf-8")
        except OSError:
            continue


def save_source(name: str, source: str) -> None:
    """保存用户编辑的源码，并标记为「用户所有」，避免后续同步覆盖。"""
    if not validate_name(name):
        raise ValueError("刮削器名称无效")
    directory = _custom_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(source, encoding="utf-8")
    revision = directory / f"{name}.py.revision"
    try:
        revision.unlink(missing_ok=True)  # 无 revision = 用户所有
    except OSError:
        pass


def restore_source(name: str) -> str:
    """恢复插件随程序发布的原始源码。"""
    if name not in PLUGIN_SOURCES:
        raise ValueError("该刮削器没有随程序发布的源码")
    source = PLUGIN_SOURCES[name]
    directory = _custom_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(source, encoding="utf-8")
    (directory / f"{name}.py.revision").write_text(_sha(source), encoding="utf-8")
    return source


def custom_source(name: str) -> str | None:
    """读取自定义积木的源码（插件优先返回当前生效源码）。"""
    if not validate_name(name):
        return None
    plugin = plugin_source(name)
    if plugin is not None:
        return plugin
    try:
        path = _custom_dir() / f"{name}.py"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def delete_source(name: str) -> bool:
    """删除自定义积木的源码与 revision 文件，返回是否真的删掉了文件。"""
    if not validate_name(name):
        return False
    directory = _custom_dir()
    path = (directory / f"{name}.py").resolve()
    if path.parent != directory.resolve() or not path.is_file():
        return False
    try:
        path.unlink()
        (directory / f"{name}.py.revision").unlink(missing_ok=True)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# 4. 目录 / 开关 / 配置 / 状态
# ---------------------------------------------------------------------------

def _discovered_custom_names() -> list[str]:
    directory = _custom_dir()
    names = set()
    try:
        for path in directory.glob("*.py"):
            if path.stem.startswith("_"):
                continue
            if path.stem in CATALOG_BY_NAME:
                continue  # 插件 / 内置同名文件不重复登记
            names.add(path.stem)
    except OSError:
        pass
    return sorted(names)


def available_names() -> list[str]:
    return list(CATALOG_BY_NAME) + _discovered_custom_names()


def disabled_names() -> set[str]:
    state = _read_state()
    names = set()
    for key in ("disabled", "disabled_built_ins"):
        value = state.get(key, [])
        if isinstance(value, list):
            names.update(str(item).strip() for item in value if str(item).strip())
    # 默认停用的积木：没有显式记录时按 enabled_by_default 处理
    explicit = state.get("enabled") if isinstance(state.get("enabled"), dict) else {}
    for item in CATALOG:
        name = item["name"]
        if name in explicit:
            if not explicit[name]:
                names.add(name)
            else:
                names.discard(name)
        elif not item["enabled_by_default"] and name not in names:
            names.add(name)
    return names


def is_enabled(name: str) -> bool:
    return name not in disabled_names() and name in set(available_names())


def set_enabled(name: str, enabled: bool) -> None:
    if name not in set(available_names()):
        raise KeyError(name)
    state = _read_state()
    value = state.get("enabled")
    if not isinstance(value, dict):
        value = {}
    value[name] = bool(enabled)
    state["enabled"] = value
    _write_state(state)


def get_settings(name: str) -> dict:
    state = _read_state()
    value = (state.get("settings") or {}).get(name) if isinstance(state.get("settings"), dict) else None
    return dict(value) if isinstance(value, dict) else {}


def public_settings(name: str) -> dict:
    """给非管理员看的配置：密钥打码，但保留「是否已填」的信息。"""
    entry = catalog_entry(name)
    secret_keys = {f["key"] for f in (entry.get("settings_schema") or []) if f.get("secret")}
    result = {}
    for key, value in get_settings(name).items():
        if key in secret_keys and value:
            result[key] = "********"
        else:
            result[key] = value
    return result


def update_settings(name: str, values: dict) -> None:
    if not isinstance(values, dict):
        raise ValueError("配置必须是对象")
    entry = catalog_entry(name)
    allowed = {f["key"] for f in (entry.get("settings_schema") or [])}
    state = _read_state()
    settings = state.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    current = dict(settings.get(name) or {})
    for key, value in values.items():
        if key not in allowed:
            continue
        text = "" if value is None else str(value)
        # 打码值表示「保留原值」
        if text == "********":
            continue
        if not text.strip():
            current.pop(key, None)
        else:
            current[key] = text
    if current:
        settings[name] = current
    else:
        settings.pop(name, None)
    state["settings"] = settings
    _write_state(state)


def get_overrides(name: str) -> dict:
    state = _read_state()
    value = (state.get("overrides") or {}).get(name) if isinstance(state.get("overrides"), dict) else None
    return dict(value) if isinstance(value, dict) else {}


def set_overrides(name: str, values: dict) -> None:
    """只允许改展示信息。能力由后端注册表决定，用户不可编辑。"""
    allowed = {"display_name", "description", "homepage"}
    state = _read_state()
    overrides = state.get("overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    current = overrides.get(name) or {}
    # 清掉历史版本可能写入的能力覆盖，避免它继续影响调度
    current.pop("provides", None)
    for key in allowed:
        if key not in values:
            continue
        text = str(values[key] or "").strip()
        if text:
            current[key] = text
        else:
            current.pop(key, None)
    if current:
        overrides[name] = current
    else:
        overrides.pop(name, None)
    state["overrides"] = overrides
    _write_state(state)


_PROVIDES_RE = re.compile(r"^\s*__provides__\s*=\s*\[([^\]]*)\]", re.MULTILINE)


def _declared_capabilities(source: str | None) -> list[str]:
    """从刮削器源码里读 `__provides__ = [...]` 声明。

    自定义刮削器只有在源码里显式声明能力时才知道它走哪条路径；
    没声明的一律按「番号刮削」处理，保证旧自定义刮削器行为不变。
    """
    if not source:
        return []
    match = _PROVIDES_RE.search(source)
    if not match:
        return []
    return [item for item in re.findall(r"['\"]([a-z_]+)['\"]", match.group(1))
            if item in CAPABILITIES]


def catalog_entry(name: str) -> dict:
    base = CATALOG_BY_NAME.get(name)
    if base:
        entry = json.loads(json.dumps(base))  # 深拷贝，避免改动全局定义
    else:
        # 自定义刮削器：能力由源码里的 __provides__ 决定（默认按番号刮削）
        declared = _declared_capabilities(custom_source(name)) or ["movie_by_code"]
        entry = _def(name, name, "custom", "site",
                     provides=declared, fields=[],
                     description="用户自定义刮削器，源码保存在 data/crawlers/%s.py。" % name)
        entry["enabled_by_default"] = True
    override = get_overrides(name)
    for key in ("display_name", "description", "homepage"):
        if override.get(key):
            entry[key] = override[key]
    # provides 始终来自注册表 / 源码声明，不受 overrides 影响
    return entry


# 检测状态 —— 对齐 JavBoss 的细粒度枚举，前端逐条翻译。
STATUS_STATES: dict[str, str] = {
    "ok": "可用",
    "not_found": "未获取到测试数据",
    "invalid_response": "数据解析失败或不完整",
    "http_error": "HTTP 响应异常",
    "timeout": "连接超时",
    "dns_error": "域名解析失败",
    "tls_error": "证书验证失败",
    "network_error": "网络或代理连接失败",
    "request_error": "请求异常",
    "canceled": "已取消",
    "unknown": "未检测",
}

# HTTP 状态码的进一步细分（仅用于展示，state 仍是 http_error）。
HTTP_STATUS_HINTS: dict[int, str] = {
    401: "需要认证", 403: "访问被拒绝", 407: "代理需要认证", 429: "请求过于频繁",
}

_EMPTY_STATUS = {"state": "unknown", "message": "尚未检测", "checked_at": "",
                 "http_status": 0, "elapsed_ms": -1, "mode": ""}


def make_status(state: str, message: str = "", *, http_status: int = 0,
                elapsed_ms: int = -1, mode: str = "", checked_at: str = "") -> dict:
    """构造一条标准化状态记录。"""
    if state not in STATUS_STATES:
        state = "unknown"
    return {
        "state": state,
        "message": str(message or STATUS_STATES.get(state, "")),
        "http_status": int(http_status or 0),
        "elapsed_ms": int(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else -1,
        "checked_at": checked_at or now_iso(),
        "mode": str(mode or ""),
    }


def status_from_error(exc: BaseException, *, http_status: int = 0, elapsed_ms: int = -1,
                      mode: str = "test") -> dict:
    """把检测过程中的异常归类成细粒度状态。"""
    name = exc.__class__.__name__
    text = str(exc) or name
    lowered = text.lower()
    # requests 异常族
    if name == "Timeout" or "timed out" in lowered or "timeout" in lowered:
        state = "timeout"
    elif name == "SSLError" or "certificate" in lowered or "ssl" in lowered:
        state = "tls_error"
    elif "nameresolutionerror" in lowered or "nodename nor servname" in lowered \
            or "getaddrinfo failed" in lowered or "name or service not known" in lowered:
        state = "dns_error"
    elif name in ("ConnectionError", "ProxyError") or "connection" in lowered \
            or "proxy" in lowered or "refused" in lowered:
        state = "network_error"
    elif http_status >= 400:
        state = "http_error"
    elif "not found" in lowered or "未找到" in text or "找不到" in text:
        state = "not_found"
    elif "json" in lowered or "decode" in lowered or "parse" in lowered or "解析" in text:
        state = "invalid_response"
    else:
        state = "request_error"
    if state == "http_error" and http_status in HTTP_STATUS_HINTS:
        text = f"{HTTP_STATUS_HINTS[http_status]}（HTTP {http_status}）"
    return make_status(state, text, http_status=http_status,
                       elapsed_ms=elapsed_ms, mode=mode)


def evaluate_result(data: dict, sample: str, capability: str) -> tuple[bool, str]:
    """判断一次检测是否算「可用」：必须真的解析出数据，且关键字段非空。

    只返回空壳响应、或把查询词原样当成标题返回的站点，都不算可用。
    """
    def filled(key):
        value = (data or {}).get(key)
        return bool(value) and value not in ("", [], {})

    if capability in ("actress_by_name", "actress_by_code"):
        name = str((data or {}).get("actress") or "").strip()
        if not name:
            return False, "未解析出演员姓名"
        return True, "演员资料解析正常"

    if capability == "movie_by_title":
        title = str((data or {}).get("title") or "").strip()
        if not title:
            return False, "未解析出标题"
        # 标题型查询命中同名作品是正常结果；用其它字段佐证「确实拿到了详情」。
        if filled("cover") or filled("plot") or filled("genre") or filled("publish_date"):
            return True, "标题资料解析正常"
        if title.casefold() == str(sample or "").strip().casefold():
            return False, "只拿到标题，缺少封面 / 剧情等详情字段"
        return True, "标题资料解析正常"

    # movie_by_code 及其它
    title = str((data or {}).get("title") or "").strip()
    if not title:
        return False, "未解析出标题"
    if title.casefold() == str(sample or "").strip().casefold():
        return False, "返回的标题与查询词相同，可能是搜索页而非详情"
    if not (filled("cover") or filled("genre") or filled("actress")):
        return False, "只拿到标题，缺少封面 / 类型 / 演员"
    return True, "资料解析正常"


def catalog() -> list[dict]:
    """完整目录，附加运行态字段（enabled / settings / status / editable）。"""
    disabled = disabled_names()
    state = _read_state()
    statuses = state.get("status") if isinstance(state.get("status"), dict) else {}
    result = []
    for name in available_names():
        entry = catalog_entry(name)
        entry["enabled"] = name not in disabled
        entry["settings"] = get_settings(name)
        entry["status"] = statuses.get(name) or dict(_EMPTY_STATUS)
        entry["editable_source"] = entry["kind"] in ("plugin", "custom")
        entry["has_source"] = True
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# 5. 刮削路径推导
# ---------------------------------------------------------------------------

# 刮削路径：由「预设里选了哪些刮削器」自动推导，不需要用户声明。
PATHS: dict[str, dict] = {
    "jav": {"label": "JAV", "description": "按番号刮削；影片必须有番号"},
    "title": {"label": "电影 / 剧集", "description": "按标题刮削；不需要番号"},
    "hybrid": {"label": "JAV + 电影", "description": "有番号按番号刮，没有则按标题刮"},
    "none": {"label": "未配置", "description": "该预设还没有选择刮削器"},
}

_JAV_CAPABILITIES = {"movie_by_code"}
_TITLE_CAPABILITIES = {"movie_by_title", "tv_by_title"}

# 预设类型：决定建库时走哪条刮削链路，也决定设置面板显示哪些项。
PRESET_KINDS = {
    "jav": {
        "label": "JAV",
        "description": "按番号刮削日本影片；使用完整 JavSP 整理流程（番号识别、分类、封面裁剪、文件整理）。",
    },
    "media": {
        "label": "电影 / 剧集",
        "description": "按标题或 TMDb/Imdb ID 刮削普通影视；不认番号，不做整理与裁剪。",
    },
}

# 「电影 / 剧集」预设真正生效的设置。不在列表里的一律不读取、不展示。
MEDIA_PRESET_KEEP = {
    # 扫描器：只保留筛选文件的那几项
    "scanner": ("minimum_size", "ignore_folders", "extensions",
                "strm_ignore_minimum_size"),
    # 刮削器：只有一份有序列表
    "crawler": ("scrapers",),
}


def preset_kind(preset: dict) -> str:
    """预设类型；老预设没有 kind 时按刮削器能力推断。"""
    declared = str((preset or {}).get("kind") or "").strip().lower()
    if declared in PRESET_KINDS:
        return declared
    try:
        resolved = resolve_path(str((preset or {}).get("id") or "default"))
        return "media" if resolved.get("path") in ("title", "hybrid") else "jav"
    except Exception:  # noqa: BLE001 - 推断失败按 JAV 处理，保守
        return "jav"


def trim_media_preset(form: dict) -> dict:
    """把影视预设裁剪成「只有真正生效的设置」。

    JavSP 的整理 / 裁剪 / 剧照 / 番号规则对标题路径完全不起作用，
    留着只会让人以为那些设置坏了，所以保存时直接丢掉。
    """
    form = form if isinstance(form, dict) else {}
    trimmed: dict = {}
    for section, keys in MEDIA_PRESET_KEEP.items():
        source = form.get(section)
        if not isinstance(source, dict):
            continue
        kept = {key: source[key] for key in keys if key in source}
        if kept:
            trimmed[section] = kept
    return trimmed


def resolve_path(preset_id: str) -> dict:
    """根据预设里启用的刮削器推导刮削路径。

    返回 {"path": jav|title|hybrid|none, "scrapers": [...], "label": "...",
          "description": "...", "warnings": [...]}
    """
    from .storage import get_preset

    preset = get_preset(preset_id or "default") or get_preset("default") or {}
    crawler = preset.get("form") if isinstance(preset.get("form"), dict) else {}
    crawler = crawler.get("crawler") if isinstance(crawler.get("crawler"), dict) else {}
    # 与 tasks._flatten_preset_scrapers 保持同一读取口径
    chosen = crawler.get("scrapers")
    if not isinstance(chosen, list):
        selection = crawler.get("selection")
        chosen = []
        if isinstance(selection, dict):
            keys = ["normal"] + [key for key in selection if key != "normal"]
            for key in keys:
                names = selection.get(key)
                if isinstance(names, list):
                    chosen.extend(str(item).strip() for item in names if str(item).strip())
    disabled = disabled_names()
    names = [str(item).strip() for item in chosen
             if str(item).strip() and str(item).strip() not in disabled]

    capabilities: set[str] = set()
    available = set(available_names())
    warnings: list[str] = []
    for name in names:
        if name not in available:
            warnings.append("刮削器 %s 不存在，将被忽略" % name)
            continue
        capabilities.update(catalog_entry(name).get("provides") or [])

    has_jav = bool(capabilities & _JAV_CAPABILITIES)
    has_title = bool(capabilities & _TITLE_CAPABILITIES)
    # 预设里显式声明的类型优先：用户建的是「影视」预设，就一定是标题路径，
    # 不会因为顺手加了个番号型刮削器就被判成 hybrid 而走 JAV 引擎。
    declared = str(preset.get("kind") or "").strip().lower()
    if declared == "media":
        path = "title" if names else "none"
    elif declared == "jav":
        path = "jav" if names else "none"
    elif has_jav and has_title:
        path = "hybrid"
    elif has_jav:
        path = "jav"
    elif has_title:
        path = "title"
    else:
        path = "none"
    if path == "none" and not names:
        warnings.append("该预设还没有选择刮削器")
    elif path == "none":
        warnings.append("所选刮削器都不提供影片资料（缺少番号或标题能力）")

    meta = PATHS[path]
    return {
        "path": path,
        "scrapers": names,
        "label": meta["label"],
        "description": meta["description"],
        "warnings": warnings,
    }


def status_of(name: str) -> dict:
    state = _read_state()
    statuses = state.get("status") if isinstance(state.get("status"), dict) else {}
    return statuses.get(name) or dict(_EMPTY_STATUS)


def all_statuses() -> dict:
    state = _read_state()
    statuses = state.get("status") if isinstance(state.get("status"), dict) else {}
    return dict(statuses)


def set_status(name: str, status: dict) -> None:
    set_statuses({name: status})


def set_statuses(updates: dict) -> None:
    state = _read_state()
    statuses = state.get("status")
    if not isinstance(statuses, dict):
        statuses = {}
    for name, value in updates.items():
        if not isinstance(value, dict):
            continue
        current = statuses.get(str(name)) or {}
        merged = {**current, **value}
        merged["state"] = merged.get("state") if merged.get("state") in STATUS_STATES else "unknown"
        statuses[str(name)] = make_status(
            merged["state"], merged.get("message", ""),
            http_status=merged.get("http_status", 0),
            elapsed_ms=merged.get("elapsed_ms", -1),
            mode=merged.get("mode", ""),
            checked_at=merged.get("checked_at", ""),
        )
    state["status"] = statuses
    _write_state(state)
