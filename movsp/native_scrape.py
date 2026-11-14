"""标题路径的原生刮削执行器。

JAV 路径把任务交给 vendor 引擎的子进程；标题路径（普通电影 / 剧集）不经过
引擎，由这里在主进程内并发调用刮削器。

为什么复用 parse_data(movie) 契约：现有插件（tmdb、local_json）与用户自定义
刮削器都实现了它，不引入第二套 API。查询上下文通过临时属性传递：

    movie._query_title / _query_year / _query_external_id

这些属性只在本进程内存里存在，绝不写入 NFO。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from . import scrapers as scraper_registry

__all__ = ["run_path", "scrape_by_query", "PLACEHOLDER"]

logger = logging.getLogger("movsp.scrape")

# MovieInfo 不允许用空值构造，这里用一个不会被误认成真实番号的占位符，
# 构造完成后立即清掉 dvdid。
PLACEHOLDER = "__title_query__"

# 逐字段合并时按这个顺序取第一个非空值（与引擎 info_summary 语义一致）
MERGE_FIELDS = (
    "title", "ori_title", "plot", "cover", "big_cover", "genre", "score",
    "publish_date", "duration", "director", "actress", "producer",
    "publisher", "serial", "url", "magnet", "preview_pics", "preview_video",
)


def _load_vendor():
    """确保 vendor/JavSP 在 sys.path 上，并能导入 MovieInfo。"""
    import sys

    from .storage import VENDOR_DIR

    vendor = str(VENDOR_DIR)
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    from javsp.datatype import MovieInfo  # noqa: PLC0415 - 运行期导入，避免测试环境缺依赖
    return MovieInfo


def _make_movie(query: dict):
    """构造一个带查询上下文的 MovieInfo。"""
    MovieInfo = _load_vendor()
    movie = MovieInfo(PLACEHOLDER)
    movie.dvdid = query.get("code") or None
    movie.cid = None
    movie._query_title = str(query.get("title") or "")
    movie._query_year = query.get("year")
    movie._query_external_id = dict(query.get("external_id") or {})
    # kind: code（番号）/ title（电影）/ series（剧集）
    movie._query_kind = query.get("kind") or "title"
    # 剧集季号：决定 TMDb 取哪一季（0 表示未指定，走剧级信息）
    movie._query_season = int(query.get("season") or 0)
    # 候选里选定的 TMDb 条目类型（movie / tv）。
    # 同名作品可能在另一分类下（例如动漫在 tv、真人版在 tv、衍生在 movie），
    # 用户选哪条就按哪条的端点取详情。
    movie._query_media_type = str(query.get("media_type") or "")
    return movie


def scrape_season(tmdb_id: str, season: int, nodes: list[dict] | None = None) -> dict:
    """取一整季的分集资料（目前只有 TMDb 支持）。

    返回 {"ok": bool, "data": {...}, "error": str}
    """
    import os

    from . import scrapers as scraper_registry
    from .storage import CUSTOM_CRAWLERS_DIR

    if not str(tmdb_id or "").strip():
        return {"ok": False, "data": {}, "error": "缺少 TMDb ID，无法取分集资料"}
    names = [str((node or {}).get("name") or "").strip() for node in (nodes or [])]
    if not names:
        names = ["tmdb"]

    os.environ.update(scraper_registry.runtime_env())
    custom_dir = str(CUSTOM_CRAWLERS_DIR)
    paths = os.environ.get("PYTHONPATH", "").split(os.pathsep) if os.environ.get("PYTHONPATH") else []
    if custom_dir not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join([custom_dir, *paths]) if paths else custom_dir

    for name in names:
        entry = scraper_registry.catalog_entry(name)
        if "tv_by_title" not in (entry.get("provides") or []):
            continue
        module = _load_module(name)
        parse_season = getattr(module, "parse_season", None) if module else None
        if not callable(parse_season):
            continue
        settings = scraper_registry.get_settings(name) or {}
        try:
            data = parse_season(
                tmdb_id,
                int(season),
                language=str(settings.get("language") or "zh-CN"),
                image_base=settings.get("image_base"),
                api_key=settings.get("api_key"),
                base_url=settings.get("base_url"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "data": {}, "error": str(exc) or exc.__class__.__name__}
        return {"ok": True, "data": data, "error": ""}
    return {"ok": False, "data": {}, "error": "没有支持分集刮削的刮削器"}


def _load_module(name: str):
    """按名字取刮削器模块（需要访问 parse_data 以外的函数时用）。"""
    import importlib
    import sys

    from .storage import CUSTOM_CRAWLERS_DIR, VENDOR_DIR

    scraper_registry.ensure_plugin_files()
    for path in (str(CUSTOM_CRAWLERS_DIR), str(VENDOR_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)
    for module_name in (f"javsp.web.{name}", name):
        if module_name in sys.modules:
            try:
                importlib.reload(sys.modules[module_name])
            except Exception:  # noqa: BLE001
                pass
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
    return None


def _load_parse_data(name: str):
    """按名字取刮削器的 parse_data，兼容内置与自定义模块。"""
    import importlib
    import sys

    from .storage import CUSTOM_CRAWLERS_DIR, VENDOR_DIR

    # 插件源码可能还没物化到 data/crawlers/
    scraper_registry.ensure_plugin_files()
    for path in (str(CUSTOM_CRAWLERS_DIR), str(VENDOR_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)
    for module_name in (f"javsp.web.{name}", name):
        # 用户改过源码后要拿到最新实现，不能命中 sys.modules 里的旧版本
        if module_name in sys.modules:
            try:
                importlib.reload(sys.modules[module_name])
            except Exception:  # noqa: BLE001 - 重载失败时沿用旧模块
                pass
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        parse_data = getattr(module, "parse_data", None)
        if callable(parse_data):
            return parse_data
    raise ImportError(f"刮削器 {name} 没有可调用的 parse_data")


def _merge(results: list[tuple[str, Any]]) -> dict:
    """按顺序逐字段合并：靠前的刮削器优先提供字段。"""
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, info in results:
        if info is None:
            continue
        for field in MERGE_FIELDS:
            value = getattr(info, field, None)
            if value in (None, "", [], {}):
                continue
            if merged.get(field) in (None, "", [], {}):
                merged[field] = value
                sources[field] = name
        # 外部 ID 由刮削器写上；第一个提供者胜出
        for key in ("tmdb_id", "imdb_id"):
            value = getattr(info, key, None)
            if value and not merged.get(key):
                merged[key] = str(value)
                sources[key] = name
    merged["_field_sources"] = sources
    return merged


def scrape_by_query(query: dict, nodes: list[dict], timeout: float = 90.0) -> dict:
    """并发调用一组刮削器，返回合并后的资料。

    query: scrape_plan.detect_query() 的结果
    nodes: [{name, ...}, ...]，来自 preset 的 scrapers 列表

    返回 {"ok": bool, "data": {...}, "sources": {...}, "errors": {name: msg}}
    """
    if not nodes:
        return {"ok": False, "data": {}, "sources": {}, "errors": {}, "message": "没有可用的刮削器"}

    # 主进程内执行也要让插件读到配置（引擎子进程靠同一个环境变量）
    import os
    from .storage import CUSTOM_CRAWLERS_DIR
    runtime_env = scraper_registry.runtime_env()
    os.environ.update(runtime_env)
    custom_dir = str(CUSTOM_CRAWLERS_DIR)
    paths = os.environ.get("PYTHONPATH", "").split(os.pathsep) if os.environ.get("PYTHONPATH") else []
    if custom_dir not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join([custom_dir, *paths]) if paths else custom_dir

    results: list[tuple[str, Any]] = []
    errors: dict[str, str] = {}
    candidates: dict[str, list[dict]] = {}
    lock = threading.Lock()

    def worker(name: str):
        try:
            parse_data = _load_parse_data(name)
            movie = _make_movie(query)
            parse_data(movie)
            with lock:
                results.append((name, movie))
                # 刮削器可以自带候选列表（搜索结果多条且无法消歧时）
                found = getattr(movie, "_tmdb_candidates", None) or []
                if found:
                    candidates[name] = list(found)
        except Exception as exc:  # noqa: BLE001 - 单个刮削器失败不影响其它
            logger.debug("标题刮削失败: %s: %s", name, exc)
            with lock:
                errors[name] = str(exc) or exc.__class__.__name__

    threads = []
    for node in nodes:
        name = str(node.get("name") or "").strip()
        if not name:
            continue
        thread = threading.Thread(target=worker, args=(name,), name=f"scrape-{name}", daemon=True)
        thread.start()
        threads.append((name, thread))
    for _, thread in threads:
        thread.join(timeout=timeout)

    # 保持预设里的顺序，而不是线程完成的顺序
    order = {str(node.get("name")): index for index, node in enumerate(nodes)}
    results.sort(key=lambda item: order.get(item[0], 999))

    data = _merge(results)
    ok = any(value for key, value in data.items()
             if not key.startswith("_") and value not in (None, "", [], {}))
    # 有多条候选说明存在歧义：结果可用，但要让用户确认选的是哪一部
    flat_candidates = [item for group in candidates.values() for item in group]
    ambiguous = bool(flat_candidates) and len(flat_candidates) > 1
    return {
        "ok": ok,
        "data": data,
        "sources": data.pop("_field_sources", {}),
        "errors": errors,
        "candidates": flat_candidates,
        "ambiguous": ambiguous,
        "message": "" if ok else "所有刮削器都没有返回可用资料",
    }


def run_path(query: dict, nodes: list[dict]) -> dict:
    """执行一次标题路径刮削（当前仅标题型；剧集在阶段 4）。"""
    return scrape_by_query(query, nodes)
