"""Filesystem-first media library index.

The library treats the files on disk (video, NFO, artwork) as the source of
truth.  Task records only describe how a film was processed; deleting them must
never remove a film from the library.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from . import series_scan
from .storage import DATA_DIR, VENDOR_DIR, read_config
from .timeutils import now_iso

LIBRARY_DB_FILE = DATA_DIR / "library.db"
# 重新刮削前的 NFO 快照：引擎写 NFO 是整份重建，用它把旧资料补回去
NFO_SNAPSHOT_DIR = DATA_DIR / "library-nfo-snapshots"

VIDEO_EXTENSIONS = {'.3gp', '.avi', '.f4v', '.flv', '.iso', '.m2ts', '.m4v', '.mkv', '.mov',
                    '.mp4', '.mpeg', '.mpg', '.rm', '.rmvb', '.ts', '.vob', '.webm', '.wmv', '.strm'}
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
IGNORED_DIRS = {'.git', '.svn', '#recycle', '#整理完成', '#不要扫描', '@eadir', 'system volume information',
                '$recycle.bin', 'lost+found'}
PART_PATTERN = re.compile(r'(?i)[\s._-]*(?:cd|part|p|disc|disk)[\s._-]*\d{1,2}$')
HARD_FIELDS = ('dvdid', 'title', 'cover')

_lock = threading.RLock()
_ready: set[str] = set()
_scanner_cache: dict[str, object] = {}
_scan_state: dict[str, dict] = {}
_scan_lock = threading.Lock()


@contextmanager
def connection():
    LIBRARY_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(LIBRARY_DB_FILE, timeout=30)
    db.row_factory = sqlite3.Row
    # 与 task_store 保持一致：SQL 里用 casefold() 做不区分大小写的匹配
    db.create_function('casefold', 1, lambda value: str(value or '').casefold(), deterministic=True)
    db.execute('PRAGMA foreign_keys=ON')
    key = str(LIBRARY_DB_FILE.resolve())
    try:
        with _lock:
            if key not in _ready:
                _ensure_schema(db)
                _ready.add(key)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_ready() -> None:
    """启动时先把 schema 迁移跑完。

    老库第一次打开要补列（kind / parent_id / season / episode），如果拖到用户点「扫描」
    时才做，扫描线程会和迁移抢同一把写锁，偶发 "attempt to write a readonly database"。
    """
    with connection():
        pass


def _ensure_schema(db) -> None:
    key = str(LIBRARY_DB_FILE.resolve())
    if key in _ready:
        return
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript("""
    CREATE TABLE IF NOT EXISTS libraries (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, preset_id TEXT NOT NULL DEFAULT 'default',
        kind TEXT NOT NULL DEFAULT 'movie',
        created_at TEXT NOT NULL, last_scan_at TEXT, scan_status TEXT NOT NULL DEFAULT '',
        sort_order REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS roots (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
        preset_id TEXT NOT NULL DEFAULT 'default', recursive INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, last_scan_at TEXT, scan_status TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS library_roots (
        library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
        root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        added_at TEXT NOT NULL,
        PRIMARY KEY (library_id, root_id)
    );
    CREATE TABLE IF NOT EXISTS films (
        id TEXT PRIMARY KEY,
        root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'movie',
        parent_id TEXT NOT NULL DEFAULT '',
        season INTEGER NOT NULL DEFAULT 0,
        episode INTEGER NOT NULL DEFAULT 0,
        dir_path TEXT NOT NULL, video_path TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]',
        size_bytes INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
        dvdid TEXT NOT NULL DEFAULT '', dvdid_source TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '', original_title TEXT NOT NULL DEFAULT '',
        plot TEXT NOT NULL DEFAULT '', release_date TEXT NOT NULL DEFAULT '',
        studio TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '',
        director TEXT NOT NULL DEFAULT '', runtime INTEGER NOT NULL DEFAULT 0,
        actress_text TEXT NOT NULL DEFAULT '',
        nfo_path TEXT NOT NULL DEFAULT '', nfo_mtime REAL NOT NULL DEFAULT 0,
        cover_path TEXT NOT NULL DEFAULT '', fanart_path TEXT NOT NULL DEFAULT '',
        fanart_count INTEGER NOT NULL DEFAULT 0,
        extrafanart_dir TEXT NOT NULL DEFAULT '',
        season_number INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'unscraped', missing_fields TEXT NOT NULL DEFAULT '[]',
        scrape_state TEXT NOT NULL DEFAULT 'idle', last_task_id TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS film_actress (
        film_id TEXT NOT NULL REFERENCES films(id) ON DELETE CASCADE,
        name TEXT NOT NULL, ord INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (film_id, name)
    );
    CREATE TABLE IF NOT EXISTS film_progress (
        film_id TEXT PRIMARY KEY REFERENCES films(id) ON DELETE CASCADE,
        position REAL NOT NULL DEFAULT 0,
        duration REAL NOT NULL DEFAULT 0,
        finished INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS scans (
        id TEXT PRIMARY KEY, root_id TEXT NOT NULL, status TEXT NOT NULL,
        discovered INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS films_root ON films(root_id, updated_at DESC, id);
    CREATE INDEX IF NOT EXISTS films_status ON films(root_id, status);
    CREATE INDEX IF NOT EXISTS films_dvdid ON films(dvdid);
    CREATE INDEX IF NOT EXISTS films_release ON films(release_date);
    CREATE INDEX IF NOT EXISTS actress_name ON film_actress(name, film_id);
    CREATE INDEX IF NOT EXISTS library_roots_root ON library_roots(root_id);
    """)
    _ensure_scan_library_column(db)
    _ensure_scan_skipped_column(db)
    _ensure_film_detached_column(db)
    _ensure_library_kind_column(db)
    _ensure_library_order_column(db)
    _ensure_film_series_columns(db)
    _migrate_legacy_roots(db)
    _ready.add(key)


def _table_columns(db, table: str) -> set[str]:
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _ensure_scan_library_column(db) -> None:
    """扫描记录归属到媒体库（旧库按文件夹扫描，这里追加列并保留旧行）"""
    if 'library_id' in _table_columns(db, 'scans'):
        return
    db.execute("ALTER TABLE scans ADD COLUMN library_id TEXT NOT NULL DEFAULT ''")


def _ensure_scan_skipped_column(db) -> None:
    if 'skipped' in _table_columns(db, 'scans'):
        return
    db.execute('ALTER TABLE scans ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0')


def _ensure_film_detached_column(db) -> None:
    """整理把影片移出库目录后，索引会跟着搬家；detached 标记这类“已整理”的影片，
    避免后续扫描把它当作丢失。"""
    if 'detached' in _table_columns(db, 'films'):
        return
    db.execute('ALTER TABLE films ADD COLUMN detached INTEGER NOT NULL DEFAULT 0')


def _ensure_library_kind_column(db) -> None:
    """媒体库类型：movie（一个目录一部片）/ series（底层目录一部剧，按季集拆分）。"""
    if 'kind' not in _table_columns(db, 'libraries'):
        db.execute("ALTER TABLE libraries ADD COLUMN kind TEXT NOT NULL DEFAULT 'movie'")
    # 早期版本写过 'adult' 之类的值，统一归一到 movie（读取时也会归一，这里顺手把库里的值理顺）
    db.execute("UPDATE libraries SET kind='movie' WHERE kind NOT IN ('movie','series')")
    db.commit()


def _ensure_library_order_column(db) -> None:
    """媒体库在顶部标签栏里的顺序；老库按原来的「创建时间」回填，观感不变。"""
    if 'sort_order' in _table_columns(db, 'libraries'):
        return
    db.execute('ALTER TABLE libraries ADD COLUMN sort_order REAL NOT NULL DEFAULT 0')
    rows = db.execute('SELECT id FROM libraries ORDER BY created_at, name').fetchall()
    for index, row in enumerate(rows):
        db.execute('UPDATE libraries SET sort_order=? WHERE id=?', (float(index), row['id']))
    db.commit()


def _ensure_film_series_columns(db) -> None:
    """剧集库需要的列：条目类型、所属剧集、季、集。老库补列，默认值等于“普通影片”。

    注意：`films_parent` 索引必须等列补齐之后再建，否则老库（还没有 parent_id 列）
    会在建索引这一步直接报 "no such column"，整个媒体库都打不开。
    """
    columns = _table_columns(db, 'films')
    for name, ddl in (('kind', "TEXT NOT NULL DEFAULT 'movie'"),
                      ('parent_id', "TEXT NOT NULL DEFAULT ''"),
                      ('season', 'INTEGER NOT NULL DEFAULT 0'),
                      ('episode', 'INTEGER NOT NULL DEFAULT 0'),
                      # 普通电影的身份键：JAV 影片这两列为空，行为不变
                      ('tmdb_id', "TEXT NOT NULL DEFAULT ''"),
                      ('imdb_id', "TEXT NOT NULL DEFAULT ''"),
                      # 标题来源：nfo / suggested（由文件名推测，仅供预填）/ manual
                      ('title_source', "TEXT NOT NULL DEFAULT ''"),
                      # 横版图（用户可视需要在媒体库里切换竖版/横版展示）
                      ('fanart_path', "TEXT NOT NULL DEFAULT ''"),
                      # 剧集条目「本条目属于第几季」：一季一个目录时由用户手动填写，
                      # 决定刮削时向 TMDb 取哪一季（tv/{id}/season/{n}）
                      ('season_number', 'INTEGER NOT NULL DEFAULT 0')):
        if name in columns:
            continue
        db.execute(f'ALTER TABLE films ADD COLUMN {name} {ddl}')
    db.execute('CREATE INDEX IF NOT EXISTS films_parent ON films(parent_id, season, episode)')
    db.commit()


def _migrate_legacy_roots(db) -> None:
    """旧版本里一个文件夹就是一个“媒体库”，迁移成“库 + 单个文件夹”。

    用文件夹 id 作为库 id，已有影片、任务和外链都不需要改。
    """
    rows = db.execute('SELECT * FROM roots WHERE id NOT IN (SELECT root_id FROM library_roots)').fetchall()
    for row in rows:
        existing = db.execute('SELECT id FROM libraries WHERE id=?', (row['id'],)).fetchone()
        if existing is None:
            db.execute('INSERT INTO libraries(id,name,preset_id,created_at,last_scan_at,scan_status) '
                       'VALUES(?,?,?,?,?,?)',
                       (row['id'], row['name'], row['preset_id'], row['created_at'],
                        row['last_scan_at'], row['scan_status']))
        db.execute('INSERT OR IGNORE INTO library_roots(library_id,root_id,added_at) VALUES(?,?,?)',
                   (row['id'], row['id'], now_iso()))


def _reset_ready() -> None:
    """Tests swap the database path; production never calls this."""
    _ready.clear()
    _scanner_cache.clear()


def _normalize_path(value: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(value or '').strip())))


def _video_extensions() -> set[str]:
    try:
        scanner = (read_config() or {}).get('scanner') or {}
        values = scanner.get('filename_extensions') or []
        configured = {str(item).lower() for item in values if str(item).startswith('.')}
        return configured or VIDEO_EXTENSIONS
    except Exception:
        return VIDEO_EXTENSIONS


def _ignored_dir(name: str) -> bool:
    return name.casefold() in IGNORED_DIRS or name.startswith('.')


def _part_key(stem: str) -> str:
    return PART_PATTERN.sub('', stem).strip() or stem


def _scanner_config():
    """刮削引擎的分片规则；校验后的 scanner 模型会被缓存（整库扫描逐目录复用）"""
    with _lock:
        if 'scanner' in _scanner_cache:
            return _scanner_cache['scanner']
    value = None
    try:
        import yaml
        from javsp.config import Cfg
        config = yaml.safe_load(read_config()) or {}
        value = Cfg.model_validate(config).scanner
    except Exception:  # noqa: BLE001 - 配置不可用或不合法时退回简单的后缀规则
        value = None
    with _lock:
        _scanner_cache['scanner'] = value
    return value


# 剧集不套用电影的「最小匹配文件大小」：一集几十 MB 很常见，电影级阈值会把整部剧过滤光
SERIES_MIN_SIZE = 1 << 20  # 1 MiB，只挡掉明显是垃圾的文件

_preset_scanner_cache: dict[str, tuple[dict, int]] = {}


def invalidate_preset_cache() -> None:
    """预设改了最小文件大小/分片规则后，下一次扫描必须重新读取。"""
    with _lock:
        _preset_scanner_cache.clear()


def preset_scan_limits(preset_id: str) -> tuple[dict, int]:
    """媒体库所属预设的扫描器配置与最小文件大小（字节），与新建刮削任务口径一致。"""
    key = str(preset_id or '')
    with _lock:
        if key in _preset_scanner_cache:
            return _preset_scanner_cache[key]
    scanner = None
    try:
        from . import storage
        preset = storage.get_preset(key or 'default') or {}
        form = preset.get('form') if isinstance(preset.get('form'), dict) else {}
        candidate = form.get('scanner') if isinstance(form, dict) else None
        if isinstance(candidate, dict):
            scanner = dict(candidate)
        elif preset.get('content'):
            import yaml
            loaded = yaml.safe_load(str(preset['content'])) or {}
            scanner = dict(loaded.get('scanner') or {}) if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001 - 预设缺失或损坏时退回基础配置
        scanner = None
    if scanner is None:
        try:
            import yaml
            base = yaml.safe_load(read_config()) or {}
            scanner = dict(base.get('scanner') or {}) if isinstance(base, dict) else {}
        except Exception:  # noqa: BLE001
            scanner = {}
    try:
        from .tasks import _minimum_size_bytes
        minimum = _minimum_size_bytes({'scanner': scanner})
    except Exception:  # noqa: BLE001
        minimum = 0
    value = (scanner, max(0, minimum))
    with _lock:
        _preset_scanner_cache[key] = value
    return value


def _passes_size(path: Path, minimum: int, scanner: dict) -> bool:
    """按预设的最小文件大小过滤；STRM 可依预设豁免。"""
    if minimum <= 0:
        return True
    try:
        from .tasks import _passes_minimum_size
        return _passes_minimum_size(path, minimum, {'scanner': scanner})
    except Exception:  # noqa: BLE001
        try:
            return path.stat().st_size >= minimum
        except OSError:
            return False


def _group_videos(paths: list[Path]) -> list[list[Path]]:
    """同一目录内的视频按引擎规则分组：数字分片、CD/part 与字母分片都合并成一部影片"""
    scanner = _scanner_config()
    if scanner is not None:
        try:
            from javsp.multipart import group_files
            groups = group_files(paths, scanner)
            if groups:
                return groups
        except Exception:  # noqa: BLE001 - 编号有歧义时退回逐文件分组，不中断整库扫描
            pass
    grouped: dict[str, list[Path]] = {}
    for path in paths:
        grouped.setdefault(_part_key(path.stem).casefold(), []).append(path)
    return list(grouped.values())


def _film_id(root_id: str, dir_path: str, part_key: str) -> str:
    digest = hashlib.sha1(f'{root_id}|{dir_path.casefold()}|{part_key.casefold()}'.encode('utf-8')).hexdigest()
    return digest[:12]


# ---------------------------------------------------------------------------
# Libraries（一个媒体库 = 一个名字 + 多个文件夹）
# ---------------------------------------------------------------------------


def _root_rows(db, library_id: str) -> list[sqlite3.Row]:
    # 分集不算「条目」：剧集库的 film_count 是剧的数量
    return db.execute(
        """SELECT r.*, (SELECT count(*) FROM films f WHERE f.root_id=r.id AND f.kind<>'episode') AS film_count
           FROM roots r JOIN library_roots lr ON lr.root_id=r.id
           WHERE lr.library_id=? ORDER BY lr.added_at, r.path""", (library_id,)).fetchall()


LIBRARY_KINDS = ('movie', 'series')


def _normalize_kind(value) -> str:
    text = str(value or '').strip().lower()
    return text if text in LIBRARY_KINDS else 'movie'


def _library_from_row(db, row: sqlite3.Row) -> dict:
    folders = [{'id': item['id'], 'path': item['path'], 'name': item['name'],
                'recursive': bool(item['recursive']), 'film_count': item['film_count'],
                'last_scan_at': item['last_scan_at'], 'scan_status': item['scan_status']}
               for item in _root_rows(db, row['id'])]
    kind = _normalize_kind(row['kind'] if 'kind' in row.keys() else 'movie')
    return {
        'id': row['id'], 'name': row['name'], 'preset_id': row['preset_id'], 'kind': kind,
        'created_at': row['created_at'], 'last_scan_at': row['last_scan_at'],
        'scan_status': row['scan_status'], 'folders': folders,
        'film_count': sum(item['film_count'] for item in folders),
        'minimum_size': preset_scan_limits(row['preset_id'])[1],
        'scrape_path': _library_scrape_path(row['preset_id']),
        'stats': _stats(db, library_id=row['id']),
    }


def _library_scrape_path(preset_id: str) -> dict:
    """媒体库的刮削路径 —— 由预设里选中的刮削器自动推导，用户无需声明。"""
    try:
        from . import scrapers
        return scrapers.resolve_path(preset_id or 'default')
    except Exception:  # noqa: BLE001 - 推导失败不应影响媒体库列表
        return {'path': 'none', 'scrapers': [], 'label': '未知',
                'description': '', 'warnings': ['无法推导刮削路径']}


def list_libraries() -> list[dict]:
    with connection() as db:
        rows = db.execute('SELECT * FROM libraries ORDER BY sort_order, created_at, name').fetchall()
        return [_library_from_row(db, row) for row in rows]


def get_library(library_id: str) -> dict | None:
    with connection() as db:
        row = db.execute('SELECT * FROM libraries WHERE id=?', (library_id,)).fetchone()
        return _library_from_row(db, row) if row else None


def add_library(name: str, preset_id: str = 'default', folders: list | None = None,
                kind: str = 'movie') -> dict:
    clean_name = str(name or '').strip()[:80]
    if not clean_name:
        raise ValueError('请填写媒体库名称')
    with connection() as db:
        duplicate = db.execute('SELECT id FROM libraries WHERE lower(name)=lower(?)', (clean_name,)).fetchone()
        if duplicate:
            raise ValueError('已有同名媒体库')
        library_id = uuid.uuid4().hex[:12]
        # 新库排在最后：拖动排序把顺序存在 sort_order 里
        next_order = db.execute('SELECT coalesce(max(sort_order), -1) + 1 AS value FROM libraries').fetchone()['value']
        db.execute('INSERT INTO libraries(id,name,preset_id,kind,created_at,sort_order) VALUES(?,?,?,?,?,?)',
                   (library_id, clean_name, preset_id or 'default', _normalize_kind(kind), now_iso(), next_order))
    for folder in folders or []:
        add_folder(library_id, folder.get('path') or '', bool(folder.get('recursive', True)))
    return get_library(library_id)


def reorder_libraries(ids: list[str]) -> list[dict]:
    """按给定顺序保存媒体库顺序（拖拽排序）。

    只认库里已有的 id；没出现在列表里的库保持原有相对顺序排到最后，
    这样即便客户端拿着过期列表也不会把库弄丢。
    """
    wanted = [str(item) for item in ids if str(item or '').strip()]
    with connection() as db:
        existing = [row['id'] for row in db.execute(
            'SELECT id FROM libraries ORDER BY sort_order, created_at, name').fetchall()]
        if not existing:
            return []
        ordered = [item for item in dict.fromkeys(wanted) if item in existing]
        ordered += [item for item in existing if item not in ordered]
        for index, library_id in enumerate(ordered):
            db.execute('UPDATE libraries SET sort_order=? WHERE id=?', (float(index), library_id))
        db.commit()
    return list_libraries()


def update_library(library_id: str, *, name: str | None = None, preset_id: str | None = None,
                   kind: str | None = None) -> dict | None:
    fields, params = [], []
    if name is not None:
        clean_name = name.strip()[:80]
        if not clean_name:
            raise ValueError('媒体库名称不能为空')
        fields.append('name=?')
        params.append(clean_name)
    if preset_id is not None:
        fields.append('preset_id=?')
        params.append(preset_id or 'default')
    if kind is not None:
        fields.append('kind=?')
        params.append(_normalize_kind(kind))
    if fields:
        with connection() as db:
            db.execute('UPDATE libraries SET ' + ','.join(fields) + ' WHERE id=?', [*params, library_id])
    return get_library(library_id)


def delete_library(library_id: str) -> bool:
    """删除媒体库及其索引（只删数据库记录，不动磁盘文件）"""
    with connection() as db:
        folders = [row['root_id'] for row in db.execute(
            'SELECT root_id FROM library_roots WHERE library_id=?', (library_id,)).fetchall()]
        db.execute('DELETE FROM libraries WHERE id=?', (library_id,))
        for root_id in folders:
            shared = db.execute('SELECT 1 FROM library_roots WHERE root_id=?', (root_id,)).fetchone()
            if not shared:
                db.execute('DELETE FROM roots WHERE id=?', (root_id,))
        return bool(folders) or True


# ---------------------------------------------------------------------------
# Folders（库里的文件夹）
# ---------------------------------------------------------------------------


def list_folders(library_id: str = '') -> list[dict]:
    with connection() as db:
        if library_id:
            rows = _root_rows(db, library_id)
        else:
            rows = db.execute(
                """SELECT r.*, (SELECT count(*) FROM films f WHERE f.root_id=r.id) AS film_count
                   FROM roots r ORDER BY r.created_at, r.path""").fetchall()
    return [{'id': row['id'], 'path': row['path'], 'name': row['name'],
             'recursive': bool(row['recursive']), 'film_count': row['film_count'],
             'last_scan_at': row['last_scan_at'], 'scan_status': row['scan_status']} for row in rows]


def get_folder(root_id: str) -> dict | None:
    with connection() as db:
        row = db.execute('SELECT * FROM roots WHERE id=?', (root_id,)).fetchone()
    return {**dict(row), 'recursive': bool(row['recursive'])} if row else None


def add_folder(library_id: str, path: str, recursive: bool = True, name: str = '') -> dict:
    """把文件夹加入媒体库；同一个文件夹可以属于多个库，但同一个库内不重复"""
    with connection() as db:
        if not db.execute('SELECT id FROM libraries WHERE id=?', (library_id,)).fetchone():
            raise ValueError('媒体库不存在')
    normalized = _normalize_path(path)
    if not os.path.isdir(normalized):
        raise ValueError('文件夹不存在或不可访问')
    with connection() as db:
        row = db.execute('SELECT * FROM roots WHERE lower(path)=lower(?)', (normalized,)).fetchone()
        if row is None:
            root_id = uuid.uuid4().hex[:12]
            db.execute('INSERT INTO roots(id,name,path,preset_id,recursive,created_at) VALUES(?,?,?,?,?,?)',
                       (root_id, (name or Path(normalized).name or '文件夹').strip()[:80], normalized,
                        'default', 1 if recursive else 0, now_iso()))
        else:
            root_id = row['id']
            if db.execute('SELECT 1 FROM library_roots WHERE library_id=? AND root_id=?',
                          (library_id, root_id)).fetchone():
                raise ValueError('该文件夹已经在这个媒体库里')
            db.execute('UPDATE roots SET recursive=? WHERE id=?', (1 if recursive else 0, root_id))
        db.execute('INSERT OR IGNORE INTO library_roots(library_id,root_id,added_at) VALUES(?,?,?)',
                   (library_id, root_id, now_iso()))
    return get_folder(root_id)


def update_folder(root_id: str, *, recursive: bool | None = None, name: str | None = None) -> dict | None:
    fields, params = [], []
    if recursive is not None:
        fields.append('recursive=?')
        params.append(1 if recursive else 0)
    if name is not None:
        fields.append('name=?')
        params.append(name.strip()[:80] or '文件夹')
    if fields:
        with connection() as db:
            db.execute('UPDATE roots SET ' + ','.join(fields) + ' WHERE id=?', [*params, root_id])
    return get_folder(root_id)


def remove_folder(library_id: str, root_id: str) -> bool:
    """从媒体库移除文件夹；没有其他库引用时连同索引一起删除（不动磁盘）"""
    with connection() as db:
        removed = db.execute('DELETE FROM library_roots WHERE library_id=? AND root_id=?',
                             (library_id, root_id)).rowcount > 0
        shared = db.execute('SELECT 1 FROM library_roots WHERE root_id=?', (root_id,)).fetchone()
        if removed and not shared:
            db.execute('DELETE FROM roots WHERE id=?', (root_id,))
        return removed


# ---------------------------------------------------------------------------
# NFO / artwork discovery


def _folder_videos(directory: Path) -> list[Path]:
    extensions = _video_extensions()
    try:
        return [item for item in directory.iterdir() if item.is_file() and item.suffix.lower() in extensions]
    except OSError:
        return []


def canonical_nfo_path(directory: Path, video: Path) -> Path:
    """每部影片唯一的 NFO 路径。

    多视频目录用 `<视频文件名>.nfo`（Emby/Jellyfin 只认这个，movie.nfo 会互相打架）；
    单视频目录用 movie.nfo（引擎默认命名，Emby 也认），但目录里已经有同名 NFO 时沿用。
    """
    named = directory / f'{video.stem}.nfo'
    if len(_folder_videos(directory)) > 1:
        return named
    return named if named.is_file() else directory / 'movie.nfo'


def canonical_nfo_basename(directory: Path, video: Path) -> str:
    """同一规则的文件名（不含扩展名），用于让引擎写到同一个 NFO。"""
    return canonical_nfo_path(directory, video).stem


def _nfo_candidates(directory: Path, video: Path) -> list[Path]:
    canonical = canonical_nfo_path(directory, video)
    candidates = [canonical] if canonical.is_file() else []
    # 多视频目录里的 movie.nfo 是多部影片共用的，必须忽略（Emby 也不会用它）
    if len(_folder_videos(directory)) <= 1:
        other = directory / ('movie.nfo' if canonical.name != 'movie.nfo' else f'{video.stem}.nfo')
        if other.is_file() and other != canonical:
            candidates.append(other)
    return candidates


def _parse_nfo(path: Path) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    if str(root.tag).lower() != 'movie':
        raise ValueError('NFO 根节点不是 movie')

    def text(tag: str) -> str:
        element = root.find(tag)
        return (element.text or '').strip() if element is not None else ''

    dvdid = ''
    tmdb_id = ''
    imdb_id = ''
    for item in root.findall('uniqueid'):
        kind = (item.get('type') or '').lower()
        value = (item.text or '').strip()
        if not value:
            continue
        if kind in ('num', 'dvdid') and not dvdid:
            dvdid = value
        elif kind == 'tmdb' and not tmdb_id:
            tmdb_id = value
        elif kind == 'imdb' and not imdb_id:
            imdb_id = value
    actress = []
    for actor in root.findall('actor'):
        name = (actor.findtext('name') or '').strip()
        if name:
            actress.append(name)
    runtime = text('runtime')
    return {
        'title': text('title'), 'original_title': text('originaltitle'), 'plot': text('plot'),
        'release_date': text('premiered'), 'studio': text('studio'), 'publisher': text('publisher'),
        'director': text('director'), 'dvdid': dvdid, 'tmdb_id': tmdb_id, 'imdb_id': imdb_id,
        'actress': list(dict.fromkeys(actress)),
        'runtime': int(runtime) if runtime.isdigit() else 0,
    }


def _find_image(directory: Path, stems: list[str], names: list[str]) -> str:
    for name in names:
        for stem in stems:
            for extension in IMAGE_EXTENSIONS:
                candidate = directory / f'{stem}{extension}' if stem else directory / f'{name}{extension}'
                if candidate.is_file():
                    return str(candidate)
    return ''


# 按 Emby / Jellyfin 官方命名规则区分的图片类型见 _find_cover / _find_fanart。
# 规范来源：https://jellyfin.org/docs/general/server/media/movies/#metadata-images
#   Primary  : poster / folder / cover / default / movie        （竖版海报）
#   Backdrop : backdrop / fanart / background / art             （横版背景）
#   Thumb    : landscape / thumb                                （横版缩略图）
# 所有名字都可以纯用（poster.jpg）或作为视频名后缀（ABC-123-poster.jpg）。

# 竖版主图（Primary）：后缀与纯名等价
# 同一层内按这个顺序取第一个存在的
_COVER_SUFFIXES = ('poster', 'cover', 'default', 'movie', 'folder')
_COVER_BARE = ('poster', 'folder', 'cover', 'default')
# 横版（Backdrop + Thumb）：后缀与纯名等价
# fanart 排最前：它是 Emby 最常用的横版名，也最可能被第三方工具写入
_FANART_SUFFIXES = ('fanart', 'backdrop', 'background', 'art', 'landscape', 'thumb')
_FANART_BARE = ('fanart', 'backdrop', 'background', 'landscape')


def _first_image(basenames: list[str]) -> str:
    """按顺序找第一个存在的图片文件（basenames 不含扩展名）。"""
    for base in basenames:
        for extension in IMAGE_EXTENSIONS:
            candidate = Path(base + extension)
            if candidate.is_file():
                return str(candidate)
    return ''


def _find_cover(directory: Path, stem: str) -> str:
    """竖版海报（Primary）。

    顺序分三层，靠前的层整体优先：
      1. 与视频同名的图片（`{stem}.jpg`）—— Emby 最精确的主图约定
      2. Primary 纯名（`poster` / `folder` / `cover` / `default`）
      3. `{stem}` + Primary 后缀（`-poster` / `-cover` / `-default` / `-movie` / `-folder`）
    """
    # 1) 与视频同名的图片
    for extension in IMAGE_EXTENSIONS:
        candidate = directory / f'{stem}{extension}'
        if candidate.is_file():
            return str(candidate)
    # 2) 纯名主图
    found = _first_image([str(directory / name) for name in _COVER_BARE])
    if found:
        return found
    # 3) 视频名 + Primary 后缀
    return _first_image([str(directory / f'{stem}-{suffix}') for suffix in _COVER_SUFFIXES])


def _find_fanart(directory: Path, stem: str) -> tuple[str, int]:
    """横版图（Backdrop / Thumb）。

    只认 Emby 的横版命名，**不把 `-poster` 当横版**：
    `ABC-123-poster.jpg` 在 Emby 里是竖版 Primary，误当横版会显示错图。

    顺序分三层，靠前的层整体优先：
      1. Backdrop 后缀（`-fanart` / `-backdrop` / `-background` / `-art`）
      2. Backdrop 纯名（`fanart` / `backdrop` / `background`）
      3. Thumb 类（`-landscape` / `-thumb` / `landscape`）—— 兜底，非 Backdrop
    """
    tiers = [
        [f'{stem}-{s}' for s in ('fanart', 'backdrop', 'background', 'art')],
        ['fanart', 'backdrop', 'background', 'landscape'],
        [f'{stem}-landscape', f'{stem}-thumb', 'thumb'],
    ]
    for tier in tiers:
        found = _first_image([str(directory / name) for name in tier])
        if found:
            return found, 1
    return '', 0


def _find_extrafanart(directory: Path) -> str:
    try:
        for item in directory.iterdir():
            if item.is_dir() and item.name.casefold().endswith('extrafanart'):
                return str(item)
    except OSError:
        pass
    return ''


def film_gallery(film_id: str) -> list[dict]:
    """列出这部影片的全部本地图片（竖版海报、横版图、剧照）。

    返回 [{name, kind, url}]，kind 用于界面区分：
      * poster   —— 竖版主图
      * fanart   —— 横版背景
      * still    —— extrafanart 目录里的剧照
    只读取磁盘，不生成图片。
    """
    film = get_film(film_id, reconcile=False)
    if not film:
        return []
    directory = Path(str(film.get('dir_path') or ''))
    if not directory.is_dir():
        return []
    items: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, kind: str) -> None:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        items.append({
            'name': path.name,
            'kind': kind,
            'url': f"/api/library/films/{film_id}/asset?path={quote(path.name)}",
        })

    # 竖版主图与横版图优先放最前
    cover = str(film.get('cover_path') or '')
    if cover and not cover.lower().startswith(('http://', 'https://')):
        add(Path(cover), 'poster')
    fanart = str(film.get('fanart_path') or '')
    if fanart and not fanart.lower().startswith(('http://', 'https://')):
        add(Path(fanart), 'fanart')

    # 剧照目录里的图片（JavSP 引擎下载的 extrafanart）
    stills_dir = str(film.get('extrafanart_dir') or '')
    if stills_dir and Path(stills_dir).is_dir():
        for item in sorted(Path(stills_dir).iterdir()):
            if item.suffix.lower() in IMAGE_EXTENSIONS:
                add(item, 'still')

    # 影片目录里其它图片：海报、横版、logo 等，避免漏掉用户自己放的
    try:
        for item in sorted(directory.iterdir()):
            if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS:
                add(item, 'other')
    except OSError:
        pass
    return items


def film_asset_path(film_id: str, name: str) -> Path | None:
    """把图片文件名解析成绝对路径（限制在影片目录/剧照目录内，防目录穿越）。"""
    film = get_film(film_id, reconcile=False)
    if not film:
        return None
    target_name = Path(str(name or '')).name
    if not target_name:
        return None
    candidates = [Path(str(film.get('dir_path') or ''))]
    stills = str(film.get('extrafanart_dir') or '')
    if stills:
        candidates.append(Path(stills))
    for base in candidates:
        if not base or not base.is_dir():
            continue
        candidate = base / target_name
        if candidate.is_file() and candidate.suffix.lower() in IMAGE_EXTENSIONS:
            return candidate
    return None


def _guess_dvdid(path: Path) -> str:
    """从文件名识别番号；文件名没有时，再看目录名是否**整体就是一个番号**。

    绝不把完整路径交给引擎：引擎会扫描整条路径，媒体库路径里带 `-数字`
    （例如 `media-api-179`、`D:\\movies-2024`）时会把目录名当成番号，
    导致整库影片都被误判成同一部片。
    """
    try:
        if str(VENDOR_DIR) not in sys.path:
            sys.path.insert(0, str(VENDOR_DIR))
        from javsp.avid import get_id
    except Exception:  # noqa: BLE001 - 引擎不可用时走兜底正则
        get_id = None

    def _lookup(text: str) -> str:
        if not text:
            return ''
        try:
            if get_id is not None:
                return str(get_id(text) or '').strip().upper()
        except Exception:  # noqa: BLE001
            pass
        return ''

    # 1) 文件名优先
    found = _lookup(path.stem)
    if found:
        return found

    # 2) 目录名：只有「目录名整体就是一个番号」时才采用，
    #    避免 media-api-179 / movies-2024 这类路径片段被误判。
    #    番号惯例是全大写（SSIS-001），小写目录名一律不算。
    parent = path.parent.name
    if parent:
        whole = re.fullmatch(r'\s*([A-Z]{2,10})[-_ ](\d{2,8})\s*', parent)
        if whole:
            return f'{whole.group(1)}-{whole.group(2)}'

    # 3) 兜底：严格匹配文件名里的番号形态
    stem = path.stem
    match = re.search(r'(?i)(?<![a-z0-9])(FC2)[-_ ]?(?:PPV[-_ ]?)?(\d{5,7})(?![0-9])', stem)
    if match:
        return f'FC2-{match.group(2)}'
    match = re.search(r'(?<![A-Za-z0-9])([A-Za-z]{2,10})[-_ ](\d{2,8})(?![0-9])', stem)
    return f'{match.group(1).upper()}-{match.group(2)}' if match else ''


def _guess_title(directory: Path, video: Path) -> str:
    """没有 NFO 时，从目录名/文件名推导一个可读标题（仅用于展示）。"""
    try:
        from . import scrape_plan
        title, _ = scrape_plan.extract_title(None, directory, video)
        return title
    except Exception:  # noqa: BLE001 - 推导失败就不要标题
        return ''


def _preset_needs_code(preset_id: str) -> bool:
    """这个预设是否只按番号刮削（jav 路径）。

    标题路径（电影/剧集）不要求番号：缺失列表里不能出现「缺番号」，
    否则界面会一直提示「缺少：番号」，而这类影片本来就没有番号。
    """
    try:
        from . import scrapers
        return scrapers.resolve_path(preset_id or 'default')['path'] == 'jav'
    except Exception:  # noqa: BLE001 - 推导失败时按旧行为处理
        return True


def _status_for(dvdid: str, title: str, cover: str, has_nfo: bool, nfo_error: str,
                actress: list[str], studio: str, release_date: str,
                *, needs_code: bool = True, external_id: str = '') -> tuple[str, list[str]]:
    """计算影片资料完整度。

    needs_code=False 表示这是标题路径（普通电影/剧集）：没有番号是正常的，
    不能把「缺番号」当成缺失项，也不能因此判成不完整。
    """
    missing = []
    if needs_code:
        if not dvdid:
            missing.append('dvdid')
    elif not external_id:
        # 标题路径：没有 tmdb/imdb id 才算缺身份
        missing.append('external_id')
    if not title:
        missing.append('title')
    if not cover:
        missing.append('cover')
    if not actress:
        missing.append('actress')
    if not studio:
        missing.append('studio')
    if not release_date:
        missing.append('release_date')
    if nfo_error:
        return 'conflict', missing
    identity = dvdid if needs_code else external_id
    if identity and title and cover:
        return 'complete', missing
    if has_nfo or identity or title or cover:
        return 'partial', missing
    return 'unscraped', missing


def _record_for_files(root: dict, directory: Path, files: list[Path], single_group: bool,
                      preset_id: str | None = None) -> dict:
    files = sorted(files, key=lambda item: str(item).casefold())
    video = files[0]
    stem = _part_key(video.stem)
    nfo_path = ''
    nfo_error = ''
    metadata = {}
    best_score = -1
    for candidate in _nfo_candidates(directory, video):
        try:
            parsed = _parse_nfo(candidate)
        except (OSError, ET.ParseError, ValueError) as exc:
            nfo_error = f'NFO 解析失败：{exc}'
            nfo_path = str(candidate)
            break
        # 多个 NFO 都在时挑资料最全的那个，避免被只写了番号的 stub 抢走
        score = sum(1 for key in ('title', 'dvdid', 'studio', 'release_date', 'runtime')
                    if parsed.get(key)) + (1 if parsed.get('actress') else 0)
        if score > best_score:
            best_score, metadata, nfo_path = score, parsed, str(candidate)
    cover = _find_cover(directory, video.stem)
    fanart_path, fanart_count = _find_fanart(directory, video.stem)
    dvdid = str(metadata.get('dvdid') or '')
    source = 'nfo' if dvdid else ''
    if not dvdid:
        dvdid = _guess_dvdid(video)
        source = 'filename' if dvdid else ''
    title = str(metadata.get('title') or '')
    actress = metadata.get('actress') or []
    tmdb_id = str(metadata.get('tmdb_id') or '')
    imdb_id = str(metadata.get('imdb_id') or '')
    needs_code = _preset_needs_code(preset_id or root.get('preset_id') or 'default')
    title_source = 'nfo' if title else ''
    if not needs_code and not title:
        # 标题路径且 NFO 里没有标题：用清洗过的文件名/目录名做**预填建议**。
        # 这不是「自动刮削」，只是让资料页有内容可看、可改；
        # 真正刮削仍要用户点按钮，且用户改过之后以用户输入为准。
        title = _guess_title(directory, video)
        title_source = 'suggested' if title else ''
    status, missing = _status_for(
        dvdid, title, cover, bool(nfo_path), nfo_error,
        actress, str(metadata.get('studio') or ''), str(metadata.get('release_date') or ''),
        needs_code=needs_code,
        external_id=tmdb_id or imdb_id)
    mtime = 0.0
    size = 0
    for item in files:
        try:
            stat = item.stat()
            size += stat.st_size
            mtime = max(mtime, stat.st_mtime)
        except OSError:
            continue
    nfo_mtime = Path(nfo_path).stat().st_mtime if nfo_path and Path(nfo_path).exists() else 0
    return {
        'id': _film_id(root['id'], str(directory), stem),
        'root_id': root['id'], 'dir_path': str(directory), 'video_path': str(video),
        'files': [str(item) for item in files],
        'size_bytes': size,
        'mtime': mtime,
        'dvdid': dvdid, 'dvdid_source': source,
        'tmdb_id': tmdb_id,
        'imdb_id': imdb_id,
        'title_source': title_source,
        'title': title, 'original_title': str(metadata.get('original_title') or ''),
        'plot': str(metadata.get('plot') or ''), 'release_date': str(metadata.get('release_date') or ''),
        'studio': str(metadata.get('studio') or ''), 'publisher': str(metadata.get('publisher') or ''),
        'director': str(metadata.get('director') or ''), 'runtime': int(metadata.get('runtime') or 0),
        'actress': actress, 'actress_text': '、'.join(actress),
        'nfo_path': nfo_path, 'nfo_mtime': nfo_mtime,
        'cover_path': cover, 'fanart_path': fanart_path, 'fanart_count': fanart_count, 'extrafanart_dir': _find_extrafanart(directory),
        'status': status, 'missing_fields': missing, 'nfo_error': nfo_error,
    }


def _series_record(root: dict, series_dir: Path, entries: list[dict]) -> dict:
    """一个剧集目录 → series 条目。它本身没有视频，播放的是它的分集。"""
    show_nfo = series_dir / 'tvshow.nfo'
    meta = series_scan.parse_tvshow_nfo(show_nfo) if show_nfo.is_file() else {}
    title = str(meta.get('title') or '') or series_scan.clean_series_title(series_dir.name) or series_dir.name
    cover = series_scan.find_poster(series_dir)
    if not cover:
        poster = str(meta.get('poster_path') or '')
        cover = poster if poster and not poster.startswith(('http://', 'https://')) else ''
    size = 0
    mtime = 0.0
    for entry in entries:
        try:
            stat = entry['video'].stat()
            size += stat.st_size
            mtime = max(mtime, stat.st_mtime)
        except OSError:
            continue
    actors = [item['name'] for item in meta.get('actors') or []]
    if title and cover:
        status = 'complete'
    elif title or cover or meta:
        status = 'partial'
    else:
        status = 'unscraped'
    return {
        'id': _film_id(root['id'], str(series_dir), 'series'),
        'root_id': root['id'], 'kind': 'series', 'parent_id': '', 'season': 0, 'episode': 0,
        'dir_path': str(series_dir), 'video_path': '',
        'files': [str(entry['video']) for entry in entries],
        'size_bytes': size, 'mtime': mtime,
        'dvdid': '', 'dvdid_source': '',
        'tmdb_id': str(meta.get('tmdb_id') or ''),
        'imdb_id': str(meta.get('imdb_id') or ''),
        'title': title,
        'original_title': str(meta.get('original_title') or ''),
        'plot': str(meta.get('plot') or ''),
        'release_date': str(meta.get('premiered') or meta.get('year') or ''),
        'studio': str(meta.get('studio') or ''), 'publisher': '', 'director': '',
        'runtime': 0,
        'actress': actors, 'actress_text': '、'.join(actors),
        'nfo_path': str(show_nfo) if meta else '',
        'nfo_mtime': show_nfo.stat().st_mtime if meta and show_nfo.exists() else 0,
        'cover_path': cover,
        'fanart_path': str(series_scan.find_fanart(series_dir) or ''),
        'fanart_count': 1 if series_scan.find_fanart(series_dir) else 0,
        'extrafanart_dir': '',
        'status': status, 'missing_fields': [], 'nfo_error': '',
    }


def _episode_record(root: dict, series_record: dict, entry: dict) -> dict:
    """一集 → episode 条目（季集号、剧照、NFO 资料都来自本地）。"""
    video: Path = entry['video']
    meta = entry.get('meta') or {}
    nfo = entry.get('nfo')
    title = str(meta.get('title') or '') or video.stem
    # NFO 里的 <thumb> 可能是网络 URL；磁盘上按约定命名的本地图片优先，URL 兜底。
    nfo_thumb = str(meta.get('thumb_path') or '')
    local_thumb = series_scan.find_thumb(video)
    if nfo_thumb and not nfo_thumb.lower().startswith(('http://', 'https://')):
        thumb = nfo_thumb
    else:
        thumb = local_thumb or nfo_thumb
    size = 0
    mtime = 0.0
    try:
        stat = video.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        pass
    actors = [item['name'] for item in meta.get('actors') or []]
    if meta.get('title') and thumb:
        status = 'complete'
    elif title or meta:
        status = 'partial'
    else:
        status = 'unscraped'
    return {
        'id': _film_id(root['id'], str(video), 'episode'),
        'root_id': root['id'], 'kind': 'episode', 'parent_id': series_record['id'],
        'season': int(entry.get('season') or 0), 'episode': int(entry.get('episode') or 0),
        'dir_path': str(video.parent), 'video_path': str(video), 'files': [str(video)],
        'size_bytes': size, 'mtime': mtime,
        'dvdid': '', 'dvdid_source': '',
        'title': title,
        'original_title': '',
        'plot': str(meta.get('plot') or ''),
        'release_date': str(meta.get('premiered') or ''),
        'studio': str(meta.get('studio') or ''),
        'publisher': '',
        'director': '、'.join(meta.get('directors') or []),
        'runtime': int(meta.get('runtime') or 0),
        'actress': actors, 'actress_text': '、'.join(actors),
        'nfo_path': str(nfo) if nfo else '',
        'nfo_mtime': nfo.stat().st_mtime if nfo and nfo.exists() else 0,
        'cover_path': thumb,
        'fanart_count': 0,
        'extrafanart_dir': '',
        'status': status, 'missing_fields': [], 'nfo_error': '',
    }


def _series_records_for(root: dict, series_dir: Path, scanner: dict, minimum: int) -> tuple[list[dict], int]:
    """单个剧集目录 → [series, episode...]。刷新单部剧时复用。

    剧集的最小文件大小取 `min(预设, SERIES_MIN_SIZE)`：预设的阈值是按整部影片定的
    （默认 232MiB），而一集短剧/番剧只有几十 MB，直接套上去会把整部剧过滤光。
    """
    extensions = _video_extensions()
    limit = min(int(minimum or 0), SERIES_MIN_SIZE)
    entries = []
    skipped = 0
    for entry in series_scan.episode_entries(series_dir, extensions):
        if _passes_size(entry['video'], limit, scanner):
            entries.append(entry)
        else:
            skipped += 1
    series_record = _series_record(root, series_dir, entries)
    records = [series_record, *(_episode_record(root, series_record, entry) for entry in entries)]
    return records, skipped


def _series_records(root: dict, scanner: dict, minimum: int) -> tuple[list[dict], int]:
    """把一个剧集库文件夹展开成 series + episode 记录。"""
    extensions = _video_extensions()
    records: list[dict] = []
    skipped = 0
    for series_dir in series_scan.series_dirs(Path(root['path']), extensions):
        part, part_skipped = _series_records_for(root, series_dir, scanner, minimum)
        records.extend(part)
        skipped += part_skipped
    return records, skipped


def _write_film(db, record: dict, existing: sqlite3.Row | None, preserve_manual: bool = True,
                detached: int = 0) -> bool:
    kind = str(record.get('kind') or 'movie')
    if existing is not None:
        if preserve_manual and kind == 'movie' and existing['dvdid_source'] == 'manual':
            if record.get('dvdid') and record['dvdid'].casefold() == existing['dvdid'].casefold():
                record['dvdid_source'] = 'nfo'
            else:
                record['dvdid'] = existing['dvdid']
                record['dvdid_source'] = 'manual'
        # NFO 被 Emby/Jellyfin 改写成只剩标题的 stub 时，别让一次重扫把已经刮好的资料冲掉
        if kind == 'movie' and not record.get('title') and existing['title']:
            record['title'] = existing['title']
            record['title_source'] = existing['title_source'] if 'title_source' in existing.keys() else ''
            for field in ('original_title', 'plot', 'release_date', 'studio', 'publisher', 'director'):
                if not record.get(field):
                    record[field] = existing[field]
            if not record.get('runtime'):
                record['runtime'] = existing['runtime']
            if not record.get('actress'):
                kept = [item['name'] for item in db.execute(
                    'SELECT name FROM film_actress WHERE film_id=? ORDER BY rowid', (record['id'],)).fetchall()]
                if kept:
                    record['actress'] = kept
                    record['actress_text'] = '、'.join(kept)
            record['status'], record['missing_fields'] = _status_for(
                record.get('dvdid') or '', record.get('title') or '', record.get('cover_path') or '',
                bool(record.get('nfo_path')), '', record.get('actress') or [],
                str(record.get('studio') or ''), str(record.get('release_date') or ''),
                needs_code=_preset_needs_code(''),
                external_id=str(record.get('tmdb_id') or record.get('imdb_id') or ''))
        # 用户手动改过的标题优先于文件名推测值：重扫时推测值不能覆盖用户输入
        existing_source = existing['title_source'] if 'title_source' in existing.keys() else ''
        if existing_source in ('manual', 'nfo') and record.get('title_source') == 'suggested':
            record['title'] = existing['title']
            record['title_source'] = existing_source
        # 待确认（needs_choice）要跨扫描保留，否则重新扫描后用户的选择提示就丢了
        prior = existing['scrape_state']
        if prior in ('queued', 'running', 'needs_choice'):
            record['scrape_state'] = prior
        else:
            record['scrape_state'] = (
                'failed' if prior == 'failed' and record['status'] != 'complete' else 'idle')
        record['last_task_id'] = existing['last_task_id']
        record['last_error'] = existing['last_error']
    record.setdefault('scrape_state', 'idle')
    record.setdefault('last_task_id', '')
    record.setdefault('last_error', '')
    record.setdefault('tmdb_id', '')
    record.setdefault('imdb_id', '')
    record.setdefault('title_source', '')
    record.setdefault('fanart_path', '')
    # 重扫时保留用户手填的季号：它只来自资料页，磁盘上没有任何推断依据
    if existing is not None and 'season_number' in existing.keys():
        record['season_number'] = int(existing['season_number'] or 0)
    record.setdefault('season_number', 0)
    record['kind'] = kind
    record.setdefault('parent_id', '')
    record.setdefault('season', 0)
    record.setdefault('episode', 0)
    record['detached'] = detached
    record['updated_at'] = now_iso()
    changed = existing is None or any(existing[key] != record.get(key) for key in (
        'video_path', 'dvdid', 'title', 'status', 'cover_path', 'nfo_path', 'nfo_mtime', 'mtime',
        'kind', 'parent_id', 'season', 'episode', 'tmdb_id', 'imdb_id', 'title_source'))
    db.execute("""INSERT INTO films(id,root_id,kind,parent_id,season,episode,dir_path,video_path,files,
        size_bytes,mtime,dvdid,dvdid_source,tmdb_id,imdb_id,title_source,
        title,original_title,plot,release_date,studio,publisher,director,runtime,actress_text,nfo_path,nfo_mtime,
        cover_path,fanart_path,fanart_count,extrafanart_dir,season_number,status,missing_fields,scrape_state,last_task_id,last_error,detached,updated_at)
        VALUES(:id,:root_id,:kind,:parent_id,:season,:episode,:dir_path,:video_path,:files,
        :size_bytes,:mtime,:dvdid,:dvdid_source,:tmdb_id,:imdb_id,:title_source,:title,
        :original_title,:plot,:release_date,:studio,:publisher,:director,:runtime,:actress_text,:nfo_path,
        :nfo_mtime,:cover_path,:fanart_path,:fanart_count,:extrafanart_dir,:season_number,:status,:missing_fields,:scrape_state,
        :last_task_id,:last_error,:detached,:updated_at)
        ON CONFLICT(id) DO UPDATE SET root_id=excluded.root_id,kind=excluded.kind,
        parent_id=excluded.parent_id,season=excluded.season,episode=excluded.episode,
        dir_path=excluded.dir_path,
        video_path=excluded.video_path,files=excluded.files,size_bytes=excluded.size_bytes,mtime=excluded.mtime,
        dvdid=excluded.dvdid,dvdid_source=excluded.dvdid_source,
        tmdb_id=excluded.tmdb_id,imdb_id=excluded.imdb_id,title_source=excluded.title_source,title=excluded.title,
        original_title=excluded.original_title,plot=excluded.plot,release_date=excluded.release_date,
        studio=excluded.studio,publisher=excluded.publisher,director=excluded.director,runtime=excluded.runtime,
        actress_text=excluded.actress_text,nfo_path=excluded.nfo_path,nfo_mtime=excluded.nfo_mtime,
        cover_path=excluded.cover_path,fanart_path=excluded.fanart_path,fanart_count=excluded.fanart_count,extrafanart_dir=excluded.extrafanart_dir,season_number=excluded.season_number,
        status=excluded.status,missing_fields=excluded.missing_fields,scrape_state=excluded.scrape_state,
        last_task_id=excluded.last_task_id,last_error=excluded.last_error,detached=excluded.detached,
        updated_at=excluded.updated_at""",
               {**record, 'files': json.dumps(record['files'], ensure_ascii=False),
                'missing_fields': json.dumps(record['missing_fields'], ensure_ascii=False)})
    db.execute('DELETE FROM film_actress WHERE film_id=?', (record['id'],))
    db.executemany('INSERT OR IGNORE INTO film_actress(film_id,name,ord) VALUES(?,?,?)',
                   [(record['id'], name, index) for index, name in enumerate(record['actress'])])
    return changed


def _film_from_row(db, row: sqlite3.Row, progress: dict | None = None) -> dict:
    item = dict(row)
    item['files'] = json.loads(item.get('files') or '[]')
    item['missing_fields'] = json.loads(item.get('missing_fields') or '[]')
    item['actress'] = [entry['name'] for entry in db.execute(
        'SELECT name FROM film_actress WHERE film_id=? ORDER BY ord', (row['id'],))]
    item['progress'] = (progress or {}).get(row['id']) or _empty_progress()
    return item


# ---------------------------------------------------------------------------
# 播放进度（电影与分集共用一张表，跨设备共享）


def _empty_progress() -> dict:
    return {'position': 0.0, 'duration': 0.0, 'finished': False, 'percent': 0.0, 'updated_at': ''}


def _progress_from_row(row: sqlite3.Row) -> dict:
    position = float(row['position'] or 0)
    duration = float(row['duration'] or 0)
    return {
        'position': position, 'duration': duration, 'finished': bool(row['finished']),
        'percent': round(position / duration * 100, 1) if duration > 0 else 0.0,
        'updated_at': row['updated_at'] or '',
    }


def _progress_map(db, film_ids: list[str]) -> dict[str, dict]:
    ids = [item for item in film_ids if item]
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    rows = db.execute(f'SELECT * FROM film_progress WHERE film_id IN ({marks})', ids).fetchall()
    return {row['film_id']: _progress_from_row(row) for row in rows}


def save_progress(film_id: str, position: float, duration: float = 0) -> dict:
    """记录播放进度；看到 95% 以上（或离结尾不足 60 秒）就算看完。"""
    with connection() as db:
        if not db.execute('SELECT 1 FROM films WHERE id=?', (film_id,)).fetchone():
            raise ValueError('条目不存在')
        position = max(0.0, float(position or 0))
        duration = max(0.0, float(duration or 0))
        finished = 1 if duration > 0 and (position >= duration * 0.95 or duration - position <= 60) else 0
        db.execute(
            'INSERT INTO film_progress(film_id,position,duration,finished,updated_at) VALUES(?,?,?,?,?) '
            'ON CONFLICT(film_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,'
            'finished=excluded.finished,updated_at=excluded.updated_at',
            (film_id, position, duration, finished, now_iso()))
        row = db.execute('SELECT * FROM film_progress WHERE film_id=?', (film_id,)).fetchone()
    return _progress_from_row(row)


def clear_progress(film_id: str) -> bool:
    with connection() as db:
        return db.execute('DELETE FROM film_progress WHERE film_id=?', (film_id,)).rowcount > 0


def series_progress(series_id: str) -> dict:
    """整部剧的观看情况：已看几集、下一集从哪开始（给剧集页的「继续观看」用）。"""
    with connection() as db:
        rows = db.execute(
            "SELECT f.id, f.season, f.episode, f.title, p.position, p.duration, p.finished "
            "FROM films f LEFT JOIN film_progress p ON p.film_id=f.id "
            "WHERE f.parent_id=? AND f.kind='episode' ORDER BY f.season, f.episode",
            (series_id,)).fetchall()
    total = len(rows)
    watched = sum(1 for row in rows if row['finished'])
    in_progress = next((row for row in rows if (row['position'] or 0) > 0 and not row['finished']), None)
    unwatched = in_progress or next((row for row in rows if not row['finished']), None)
    resume = None
    if unwatched is not None:
        resume = {'film_id': unwatched['id'], 'season': int(unwatched['season'] or 0),
                  'episode': int(unwatched['episode'] or 0), 'title': unwatched['title'] or '',
                  'position': float(unwatched['position'] or 0),
                  'duration': float(unwatched['duration'] or 0)}
    return {'total': total, 'watched': watched, 'resume': resume}


# ---------------------------------------------------------------------------
# Scanning


def _collect(root: dict, scanner: dict | None = None, minimum: int = 0) -> tuple[dict[tuple[str, str], list[Path]], int]:
    """枚举目录里的视频并分片分组；小于预设最小文件大小的文件会被跳过。"""
    extensions = _video_extensions()
    scanner = scanner or {}
    groups: dict[tuple[str, str], list[Path]] = {}
    skipped = 0
    base_depth = root['path'].rstrip('\\/').count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root['path']):
        dirnames[:] = [name for name in dirnames if not _ignored_dir(name)]
        if not root.get('recursive') and dirpath.rstrip('\\/').count(os.sep) > base_depth:
            dirnames[:] = []
            continue
        videos = []
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in extensions:
                continue
            candidate = Path(dirpath) / name
            if _passes_size(candidate, minimum, scanner):
                videos.append(name)
            else:
                skipped += 1
        if not videos:
            continue
        directory = Path(dirpath)
        for files in _group_videos([directory / name for name in videos]):
            key = _part_key(Path(files[0]).stem).casefold()
            groups.setdefault((dirpath, key), []).extend(files)
    return groups, skipped


def _update_scan(db, scan: dict, **values) -> None:
    """先写库再更新内存：客户端看到「已完成」时，数据一定已经落盘。"""
    fields = ','.join(f'{key}=?' for key in values)
    db.execute(f'UPDATE scans SET {fields} WHERE id=?', [*values.values(), scan['id']])
    db.commit()
    scan.update(values)


def _run_scan(scan: dict, library: dict) -> None:
    started = time.monotonic()
    try:
        with connection() as db:
            _update_scan(db, scan, status='running')
            folders = [{**dict(row), 'recursive': bool(row['recursive'])} for row in _root_rows(db, library['id'])]
            scanner, minimum = preset_scan_limits(library.get('preset_id') or 'default')
            added = updated = removed = discovered = skipped = 0
            for root in folders:
                # 文件夹本身不可访问时（例如移动硬盘未挂载）跳过清点，避免误删整库记录
                if not os.path.isdir(root['path']):
                    db.execute('UPDATE roots SET scan_status=? WHERE id=?', ('failed', root['id']))
                    continue
                if library.get('kind') == 'series':
                    # 剧集库：一个目录一部剧，再拆成分集；不分「分片」，每集独立一条
                    records, root_skipped = _series_records(root, scanner, minimum)
                    skipped += root_skipped
                else:
                    groups, root_skipped = _collect(root, scanner, minimum)
                    skipped += root_skipped
                    records = [
                        _record_for_files(root, Path(directory), files,
                                          sum(1 for key in groups if key[0] == directory) == 1,
                                          preset_id=library.get('preset_id') or 'default')
                        for (directory, _), files in groups.items()
                    ]
                seen = set()
                for record in records:
                    existing = db.execute('SELECT * FROM films WHERE id=?', (record['id'],)).fetchone()
                    if _write_film(db, record, existing):
                        if existing is None:
                            added += 1
                        else:
                            updated += 1
                    seen.add(record['id'])
                    discovered += 1
                    if discovered % 25 == 0 or time.monotonic() - started > 0.5:
                        _update_scan(db, scan, discovered=discovered, added=added, updated=updated)
                        started = time.monotonic()
                rows = (db.execute('SELECT id,video_path FROM films WHERE root_id=? AND detached=0 '
                                   f'AND id NOT IN ({",".join("?" for _ in seen)})',
                                   [root['id'], *seen]).fetchall()
                        if seen else
                        db.execute('SELECT id,video_path FROM films WHERE root_id=? AND detached=0',
                                   (root['id'],)).fetchall())
                # 这次没扫到、又没有“已整理”标记的影片：源文件已被删除或不再符合扫描条件，
                # 直接从索引移除（用户期望媒体库与磁盘保持一致，而不是留一堆“文件已丢失”）。
                for row in rows:
                    db.execute('DELETE FROM films WHERE id=?', (row['id'],))
                    removed += 1

            # 已整理（文件搬到整理目录）的影片：文件被删掉时也要从库里移除
            stale = db.execute('SELECT id,files FROM films WHERE root_id IN '
                               '(SELECT root_id FROM library_roots WHERE library_id=?) AND detached=1',
                               (library['id'],)).fetchall()
            for row in stale:
                files = [Path(item) for item in json.loads(row['files'] or '[]')]
                if files and all(not item.is_file() for item in files):
                    db.execute('DELETE FROM films WHERE id=?', (row['id'],))
                    removed += 1
                db.execute('UPDATE roots SET last_scan_at=?, scan_status=? WHERE id=?',
                           (now_iso(), 'succeeded', root['id']))
            _update_scan(db, scan, status='succeeded', discovered=discovered, added=added,
                         updated=updated, removed=removed, skipped=skipped, finished_at=now_iso())
            db.execute('UPDATE libraries SET last_scan_at=?, scan_status=? WHERE id=?',
                       (now_iso(), 'succeeded', library['id']))
    except Exception as exc:
        with connection() as db:
            _update_scan(db, scan, status='failed', error=str(exc), finished_at=now_iso())
            db.execute('UPDATE libraries SET scan_status=? WHERE id=?', ('failed', library['id']))
    finally:
        with _scan_lock:
            _scan_state.pop(scan['id'], None)


def start_scan(library_id: str) -> dict:
    library = get_library(library_id)
    if not library:
        raise ValueError('媒体库不存在')
    if not library['folders']:
        raise ValueError('媒体库还没有文件夹')
    unavailable = [folder['path'] for folder in library['folders'] if not os.path.isdir(folder['path'])]
    if len(unavailable) == len(library['folders']):
        raise ValueError('媒体库中的文件夹都不可访问')
    with _scan_lock:
        for item in _scan_state.values():
            if item.get('library_id') == library_id:
                return dict(item)
        scan = {'id': uuid.uuid4().hex[:12], 'library_id': library_id, 'root_id': '', 'status': 'queued',
                'discovered': 0, 'added': 0, 'updated': 0, 'removed': 0, 'error': '', 'started_at': now_iso()}
        _scan_state[scan['id']] = scan
    with connection() as db:
        db.execute('INSERT INTO scans(id,root_id,library_id,status,started_at) VALUES(?,?,?,?,?)',
                   (scan['id'], '', library_id, 'queued', scan['started_at']))
        db.execute('UPDATE libraries SET scan_status=? WHERE id=?', ('running', library_id))
    threading.Thread(target=_run_scan, args=(scan, library), name=f'library-scan-{library_id}', daemon=True).start()
    return scan


def get_scan(scan_id: str) -> dict | None:
    with _scan_lock:
        if scan_id in _scan_state:
            return dict(_scan_state[scan_id])
    with connection() as db:
        row = db.execute('SELECT * FROM scans WHERE id=?', (scan_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Querying


STATUS_FILTERS = {
    'complete': "status='complete'",
    'partial': "status IN ('partial','conflict')",
    'unscraped': "status='unscraped'",
    'failed': "scrape_state='failed'",
    'scraping': "scrape_state IN ('queued','running')",
    'missing': "status='missing'",
    # 标题路径搜到多条结果，等用户确认选哪一部
    'choice': "scrape_state='needs_choice'",
}


def _conditions(library_id: str = '', folder_id: str = '', status: str = '', query: str = '',
                group_by: str = '', group: str = '', kind: str = '', parent: str = '',
                season: int | None = None):
    conditions, params = [], []
    if folder_id:
        conditions.append('f.root_id=?')
        params.append(folder_id)
    elif library_id:
        conditions.append('f.root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)')
        params.append(library_id)
    if kind:
        conditions.append('f.kind=?')
        params.append(kind)
    elif not parent:
        # 分集不混进影片墙：只有明确按 parent 看某部剧时才带出来
        conditions.append("f.kind<>'episode'")
        if not library_id and not folder_id:
            # 「全部影片」是跨库的影片墙，剧集由各自的剧集库浏览
            conditions.append("f.kind<>'series'")
    if parent:
        conditions.append('f.parent_id=?')
        params.append(parent)
    if season is not None:
        conditions.append('f.season=?')
        params.append(int(season))
    if status and status in STATUS_FILTERS:
        conditions.append(STATUS_FILTERS[status])
    if query:
        needle = query.casefold()
        conditions.append("(instr(casefold(f.dvdid),?)>0 OR instr(casefold(f.title),?)>0 "
                          "OR instr(casefold(f.video_path),?)>0 OR instr(casefold(f.actress_text),?)>0)")
        params.extend([needle, needle, needle, needle])
    if group_by == 'actress' and group:
        conditions.append('EXISTS (SELECT 1 FROM film_actress a WHERE a.film_id=f.id AND a.name=?)')
        params.append(group)
    elif group_by == 'actress' and group == '':
        conditions.append('NOT EXISTS (SELECT 1 FROM film_actress a WHERE a.film_id=f.id)')
    elif group_by == 'studio':
        conditions.append('f.studio=?')
        params.append(group)
    elif group_by == 'year':
        conditions.append("substr(f.release_date,1,4)=?")
        params.append(group)
    return conditions, params


# 待确认状态的最长保留时间。
# 多结果时不自动采用任何一条，等用户挑；但用户可能一直不回来，
# 超时后撤销待确认，让这部片回到「未刮削」可以重新刮。
CHOICE_TTL_SECONDS = 2 * 60 * 60  # 2 小时


def _expire_pending_choices() -> None:
    """撤销超时未选择的待确认状态。

    只是把状态放回 idle，不动已写入的资料（本来也没有写入），
    用户重新刮削即可再次拿到候选。
    """
    from .timeutils import local_now

    # now_iso() 用的是本地时区，这里必须同样用本地时间比较
    cutoff = (local_now() - _dt.timedelta(seconds=CHOICE_TTL_SECONDS)).isoformat()
    try:
        with connection() as db:
            db.execute(
                "UPDATE films SET scrape_state='idle', last_error=?, updated_at=? "
                "WHERE scrape_state='needs_choice' AND updated_at<>'' AND updated_at<?",
                ('待确认超时，已撤销；需要时重新刮削即可再次选择',
                 now_iso(), cutoff))
    except sqlite3.Error:
        # 清理失败不能影响正常的列表查询
        pass


def list_films(library_id: str = '', folder_id: str = '', status: str = '', query: str = '',
               group_by: str = '', group: str = '', sort: str = 'updated', direction: str = 'desc',
               limit: int = 48, offset: int = 0, kind: str = '', parent: str = '',
               season: int | None = None) -> dict:
    _expire_pending_choices()
    conditions, params = _conditions(library_id, folder_id, status, query, group_by, group,
                                     kind, parent, season)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    order = {
        'release': "coalesce(f.release_date,'')",
        'dvdid': 'f.dvdid',
        'title': 'f.title',
        'size': 'f.size_bytes',
    }.get(sort, 'f.updated_at')
    if parent:
        # 分集按季集号排，和影片墙的排序互不影响
        order = 'f.season, f.episode, f.title'
    order += ' ASC' if direction == 'asc' else ' DESC'
    with connection() as db:
        total = db.execute('SELECT count(*) FROM films f' + where, params).fetchone()[0]
        offset = min(offset, max(0, ((total - 1) // limit) * limit)) if total else 0
        rows = db.execute(f'SELECT f.* FROM films f{where} ORDER BY {order},f.id LIMIT ? OFFSET ?',
                          [*params, limit, offset]).fetchall()
        progress = _progress_map(db, [row['id'] for row in rows])
        items = [_film_from_row(db, row, progress) for row in rows]
        stats = _stats(db, library_id=library_id, folder_id=folder_id, query=query,
                       group_by=group_by, group=group, kind=kind, parent=parent, season=season)
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'stats': stats}


def _film_scope(library_id: str = '', folder_id: str = '') -> tuple[str, list]:
    if folder_id:
        return ' WHERE root_id=?', [folder_id]
    if library_id:
        return ' WHERE root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)', [library_id]
    return '', []


def _stats(db, library_id: str = '', folder_id: str = '', query: str = '',
           group_by: str = '', group: str = '', kind: str = '', parent: str = '',
           season: int | None = None) -> dict:
    """统计当前筛选范围（媒体库/文件夹/搜索/分类）的状态分布。

    状态本身是统计维度，所以这里不按 status 过滤；分类和搜索会跟随。
    """
    conditions, params = _conditions(library_id, folder_id, '', query, group_by, group,
                                     kind, parent, season)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = dict(db.execute('SELECT f.status,count(*) FROM films f' + where + ' GROUP BY f.status',
                           params).fetchall())
    scrape = dict(db.execute('SELECT f.scrape_state,count(*) FROM films f' + where +
                             ' GROUP BY f.scrape_state', params).fetchall())
    return {
        'total': sum(rows.values()),
        'complete': rows.get('complete', 0),
        'partial': rows.get('partial', 0) + rows.get('conflict', 0),
        'unscraped': rows.get('unscraped', 0),
        'missing': rows.get('missing', 0),
        'failed': scrape.get('failed', 0),
        'scraping': scrape.get('queued', 0) + scrape.get('running', 0),
    }


def series_stats(library_id: str = '', parent: str = '') -> dict[str, dict]:
    """每部剧的季数与集数（series id → {seasons, episodes}），用于剧集卡片。"""
    conditions, params = ["kind='episode'"], []
    if parent:
        conditions.append('parent_id=?')
        params.append(parent)
    elif library_id:
        conditions.append('root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)')
        params.append(library_id)
    where = ' WHERE ' + ' AND '.join(conditions)
    with connection() as db:
        rows = db.execute(
            f'SELECT parent_id, count(*), count(DISTINCT season) FROM films{where} GROUP BY parent_id',
            params).fetchall()
    return {str(row[0]): {'episodes': int(row[1]), 'seasons': int(row[2])} for row in rows if row[0]}


def series_seasons(series_id: str) -> list[dict]:
    """一部剧的季列表（季号 + 集数），用于剧集页的季切换标签。"""
    with connection() as db:
        rows = db.execute(
            "SELECT season, count(*) FROM films WHERE parent_id=? AND kind='episode' "
            'GROUP BY season ORDER BY season', (series_id,)).fetchall()
    return [{'season': int(row[0] or 0), 'episodes': int(row[1])} for row in rows]


def refresh_series(film_id: str) -> dict:
    """重新读取某部剧的 tvshow.nfo 与分集 NFO。

    剧集库不接在线元数据源（18+ 内容在公开库里也查不到），所以「刷新」= 按磁盘现状重建索引：
    补了新一集、改了目录名、加了 NFO，点一下即可同步。
    """
    with connection() as db:
        row = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        if not row:
            raise ValueError('条目不存在')
        if row['kind'] != 'series':
            raise ValueError('只有剧集条目可以刷新')
        root_row = db.execute('SELECT * FROM roots WHERE id=?', (row['root_id'],)).fetchone()
        if not root_row:
            raise ValueError('剧集所在文件夹已移除')
        root = {**dict(root_row), 'recursive': bool(root_row['recursive'])}
        library = db.execute('SELECT * FROM libraries WHERE id IN '
                             '(SELECT library_id FROM library_roots WHERE root_id=?)',
                             (root['id'],)).fetchone()
    series_dir = Path(row['dir_path'])
    if not series_dir.is_dir():
        raise ValueError('剧集目录不存在，无法刷新')
    scanner, minimum = preset_scan_limits((library['preset_id'] if library else 'default') or 'default')
    records, _ = _series_records_for(root, series_dir, scanner, minimum)
    keep = {record['id'] for record in records}
    with connection() as db:
        for record in records:
            existing = db.execute('SELECT * FROM films WHERE id=?', (record['id'],)).fetchone()
            _write_film(db, record, existing)
        stale = db.execute("SELECT id FROM films WHERE parent_id=? AND kind='episode'", (film_id,)).fetchall()
        for item in stale:
            if item['id'] not in keep:
                db.execute('DELETE FROM films WHERE id=?', (item['id'],))
        db.execute('UPDATE films SET updated_at=? WHERE id=?', (now_iso(), film_id))
    return get_film(film_id) or {}


def get_film(film_id: str, reconcile: bool = True) -> dict | None:
    if reconcile:
        _reconcile(film_id)
    with connection() as db:
        row = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        if not row:
            return None
        return _film_from_row(db, row, _progress_map(db, [film_id]))


def _refresh_film(film_id: str, preserve_manual: bool = True) -> None:
    with connection() as db:
        row = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        if not row:
            return
        root = db.execute('SELECT * FROM roots WHERE id=?', (row['root_id'],)).fetchone()
        if not root:
            return
        files = [Path(item) for item in json.loads(row['files'] or '[]') if Path(item).is_file()]
        if not files:
            db.execute("UPDATE films SET status='missing',updated_at=? WHERE id=?", (now_iso(), film_id))
            return
        single_group = len({_part_key(item.stem).casefold() for item in files}) == 1
        # 用媒体库的预设判断是否需要番号（roots.preset_id 是历史遗留字段，不可信）
        library_row = db.execute(
            'SELECT l.preset_id FROM libraries l JOIN library_roots lr ON lr.library_id=l.id '
            'WHERE lr.root_id=? ORDER BY l.created_at LIMIT 1', (row['root_id'],)).fetchone()
        preset_id = (library_row['preset_id'] if library_row else '') or 'default'
        record = _record_for_files({**dict(root), 'recursive': bool(root['recursive'])},
                                   Path(row['dir_path']), files, single_group, preset_id=preset_id)
        record['id'] = film_id
        existing = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        # 刷新时保持原有的“已整理”标记：文件不在库目录是正常的
        _write_film(db, record, existing, preserve_manual=preserve_manual,
                    detached=int(existing['detached']) if existing is not None else 0)


def _reconcile(film_id: str) -> None:
    from . import task_store
    with connection() as db:
        row = db.execute('SELECT scrape_state,last_task_id,kind FROM films WHERE id=?', (film_id,)).fetchone()
    if not row or row['scrape_state'] not in ('queued', 'running', 'needs_choice') or not row['last_task_id']:
        return
    state = task_store.statuses([row['last_task_id']]).get(row['last_task_id'])
    if state in ('queued', 'running'):
        return
    if state == 'succeeded':
        # 剧集绝对不能走电影刷新：_record_for_files 生成的新记录 kind='movie'，
        # 会把整部剧静默降级成一部电影。
        if row['kind'] not in ('series', 'episode'):
            _refresh_film(film_id)
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif state == 'needs_choice':
        # 任务停在「等你确认选哪一部」：不要把它当成已结束
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='needs_choice',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif state in ('failed', 'cancelled'):
        from . import task_store as store
        record = store.record(row['last_task_id'], logs=False) or {}
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='failed',last_error=?,updated_at=? WHERE id=?",
                       (str(record.get('error') or ('任务已取消' if state == 'cancelled' else '刮削失败')), now_iso(), film_id))
    else:
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',updated_at=? WHERE id=?", (now_iso(), film_id))


def _organized_videos(task: dict, film: dict) -> list[Path]:
    """任务整理后的视频文件。

    只认引擎上报的 `organized_files`；退回扫描 save_dir 时必须文件名与本片一致，
    否则会把同一目录下的其它影片当成这部影片的分片（原地整理时尤其危险）。
    """
    extensions = _video_extensions()
    organizer = task.get('file_organizer') or {}
    candidates = [Path(item) for item in (organizer.get('organized_files') or []) if str(item)]
    videos = [path for path in candidates if path.is_file() and path.suffix.lower() in extensions]
    if videos:
        return videos
    save_dir = str(((task.get('progress') or {}).get('output') or {}).get('save_dir') or '')
    if not save_dir or not Path(save_dir).is_dir():
        return []
    stems = {Path(item).stem.casefold() for item in json.loads(film.get('files') or '[]')}
    current_stem = Path(str(film.get('video_path') or '')).stem.casefold()
    try:
        return sorted(path for path in Path(save_dir).iterdir()
                      if path.is_file() and path.suffix.lower() in extensions
                      and (path.stem.casefold() in stems
                           or (current_stem and path.stem.casefold() == current_stem)))
    except OSError:
        return []


def _relocate_film(film_id: str, task: dict) -> bool:
    """整理会把文件移出媒体库目录；把索引搬到新位置，影片就不会变成“文件已丢失”。

    只有在原文件确实不在时才搬家：整理目录和源目录重合（原地整理）时，
    绝不能把目录里的其它视频当成本片的文件。
    """
    film = get_film(film_id, reconcile=False)
    if not film:
        return False
    current = Path(str(film.get('video_path') or ''))
    if current.is_file():
        if film.get('detached'):
            with connection() as db:
                db.execute('UPDATE films SET detached=0 WHERE id=?', (film_id,))
        return False
    videos = _organized_videos(task, film)
    if not videos:
        return False
    video = videos[0]
    with connection() as db:
        db.execute("UPDATE films SET dir_path=?,video_path=?,files=?,detached=1,updated_at=? WHERE id=?",
                   (str(video.parent), str(video), json.dumps([str(path) for path in videos]), now_iso(), film_id))
    _refresh_film(film_id)
    return True


def _fill_from_task_metadata(film_id: str, task: dict) -> bool:
    """用任务里抓到的资料补影片的空字段，并写回它自己的 NFO。

    引擎抓到的资料可能被第三方（Jellyfin / Emby 打开“保存 NFO”时）用只含
    <title> 的 stub 覆盖掉；媒体库这时会显示“缺资料”，但任务记录里其实有数据。
    """
    meta = ((task.get('progress') or {}).get('metadata') or {})
    if not isinstance(meta, dict) or not meta:
        return False
    film = get_film(film_id, reconcile=False)
    if not film:
        return False

    def pick(key: str, incoming) -> str:
        # 任务刚抓到的资料优先（这才是刮削的结果）；抓取里没有的字段才沿用原值
        value = str(incoming or '').strip()
        return value or str(film.get(key) or '')

    crawled_actress = [str(item).strip() for item in (meta.get('actress') or []) if str(item).strip()]
    actress = crawled_actress or (film.get('actress') or [])
    values = {
        'dvdid': film.get('dvdid') or str(meta.get('dvdid') or ''),
        'title': pick('title', meta.get('title')),
        'original_title': film.get('original_title') or '',
        'plot': film.get('plot') or '',
        'release_date': pick('release_date', meta.get('publish_date')),
        'studio': pick('studio', meta.get('producer')),
        'publisher': pick('publisher', meta.get('publisher')),
        'director': pick('director', meta.get('director')),
        'runtime': film.get('runtime') or 0,
        'actress': actress,
    }
    changed = (
        values['title'] != (film.get('title') or '')
        or values['release_date'] != (film.get('release_date') or '')
        or values['studio'] != (film.get('studio') or '')
        or values['publisher'] != (film.get('publisher') or '')
        or values['director'] != (film.get('director') or '')
        or values['actress'] != (film.get('actress') or [])
    )
    if not changed:
        return False
    target = _nfo_target(film)
    expected = target.stat().st_mtime if target.is_file() else None
    try:
        _write_nfo(film, values, expected)
    except OSError:
        pass
    _refresh_film(film_id)
    return True


def apply_native_result(film_id: str, task: dict) -> bool:
    """把标题路径刮削结果写入影片与 NFO（媒体库以磁盘为真源）。

    JAV 路径由引擎自己写 NFO；标题路径不经过引擎，所以在这里补上，
    并沿用同一套 `_write_nfo`，保证两条路径产出的 NFO 结构一致。
    """
    data = task.get('scraped_data') or {}
    if not isinstance(data, dict) or not data:
        return False
    film = get_film(film_id, reconcile=False)
    if not film:
        return False

    def first(*keys: str) -> str:
        for key in keys:
            value = data.get(key)
            if value not in (None, '', [], {}):
                return str(value).strip()
        return ''

    actress = [str(item).strip() for item in (data.get('actress') or []) if str(item).strip()]
    values = {
        # 标题路径不产生番号；保持原值（用户可能手填过）
        'dvdid': film.get('dvdid') or '',
        'tmdb_id': first('tmdb_id') or film.get('tmdb_id') or '',
        'imdb_id': first('imdb_id') or film.get('imdb_id') or '',
        'title': first('title') or film.get('title') or '',
        'original_title': first('ori_title') or film.get('original_title') or '',
        'plot': first('plot') or film.get('plot') or '',
        'release_date': first('publish_date') or film.get('release_date') or '',
        'studio': first('producer') or film.get('studio') or '',
        'publisher': first('publisher') or film.get('publisher') or '',
        'director': first('director') or film.get('director') or '',
        'runtime': int(data.get('duration') or film.get('runtime') or 0),
        'actress': actress or (film.get('actress') or []),
    }
    target = _nfo_target(film)
    expected = target.stat().st_mtime if target.is_file() else None
    try:
        _write_nfo(film, values, expected)
    except (OSError, ValueError, ET.ParseError):
        target = None  # NFO 写不进去也要把索引更新到最新
    # 图片：竖版海报（cover）+ 横版背景（big_cover/backdrop）分别落盘，让媒体库和 Emby 都能用
    # 默认「已有图就不重复下载」；只有这部影片的季号变了（例如从第一季换到第五季）
    # 才覆盖，否则第五季的海报永远进不来。
    overwrite = _artwork_should_overwrite(film, task)
    artwork = [('cover', first('cover')), ('fanart', first('big_cover') or first('backdrop'))]
    failed: list[str] = []
    for kind, url in artwork:
        if not str(url or '').startswith(('http://', 'https://')):
            continue
        saved = _download_artwork(film, str(url), kind, overwrite=overwrite)
        if not saved:
            # 图片域名可能被墙或限流；不算刮削失败，但要让用户知道旧图没被替换
            failed.append('横版图' if kind == 'fanart' else '封面')
            try:
                from . import oplog
                label = '横版图' if kind == 'fanart' else '封面'
                oplog.get('scrape').warning(
                    '%s下载失败（可在刮削器设置里更换图片地址）: %s', label, str(url)[:120])
            except Exception:  # noqa: BLE001
                pass
    if failed:
        # 同时写进任务日志与 error 字段：
        # 只写日志的话，用户看到「已完成」却不知道封面为什么没下来。
        names = '、'.join(failed)
        note = ('图片未更新：%s下载失败，磁盘上保留原图。'
                '可在「刮削器管理 → TMDb → 图片地址」换成可访问的镜像后重新刮削。'
                % names)
        try:
            from . import tasks as task_runner
            task_runner.append_task_log(task.get('id'), note)
        except Exception:  # noqa: BLE001
            pass
        # 任务状态仍是成功（资料确实抓到了），但把原因挂出来让界面能显示
        if isinstance(task, dict):
            task['artwork_error'] = '封面未下载：图片地址不可达，请在刮削器设置里更换镜像'
    _refresh_after_native(film_id)
    return True


def _refresh_after_native(film_id: str) -> None:
    """标题路径刮完后刷新索引：剧集走剧集刷新，电影走影片刷新。

    剧集的 kind 必须保住：一旦误走 `_refresh_film`（电影逻辑），
    `_record_for_files` 生成的新记录 kind='movie'，会把整部剧降级成一部电影。
    """
    film = get_film(film_id, reconcile=False)
    if film and film.get('kind') == 'series':
        refresh_series(film_id)
        return
    _refresh_film(film_id)


def _artwork_should_overwrite(film: dict, task: dict | None) -> bool:
    """这次刮削要不要覆盖已有的图片。

    规则：**默认不覆盖**——目录里已经有 poster.jpg / fanart.jpg 就不再下载，
    避免每次刮削都重下同一张图。

    两种必须覆盖的情况：
      1. 换了一部片（TMDb / IMDb ID 变了）：旧图属于上一部作品，
         不换就会出现「NFO 是新的、海报还是旧的」；
      2. 剧集季号变了：一季一个目录时，新季目录里往往还留着上一季的 poster.jpg。
    """
    film = film or {}
    query = (task or {}).get('scrape_query') or {}

    # 1) 外部 ID 变化：换了另一部作品。
    #    拿「本次刮削实际使用的 ID」和「库里上一次记下的 ID」比较。
    used = query.get('external_id') or {}
    used_tmdb = str(used.get('tmdb') or '').strip()
    used_imdb = str(used.get('imdb') or '').strip()
    have_tmdb = str(film.get('tmdb_id') or '').strip()
    have_imdb = str(film.get('imdb_id') or '').strip()
    if used_tmdb and have_tmdb and used_tmdb != have_tmdb:
        return True
    if used_imdb and have_imdb and used_imdb != have_imdb:
        return True
    # 库里没有 ID 却显式指定了：用户「清空输入」后重填再刮，
    # 目录里留着的还是上一部作品的封面，必须换掉。
    if (used_tmdb and not have_tmdb) or (used_imdb and not have_imdb):
        return True

    # 2) 剧集换季
    if film.get('kind') not in ('series', 'episode'):
        return False
    try:
        wanted = int(query.get('season') or 0)
    except (TypeError, ValueError):
        wanted = 0
    current = int(film.get('season_number') or 0)
    return bool(wanted and current and wanted != current)


def _fetch_image(url: str, attempts: int = 3) -> bytes:
    """下载图片，失败时重试。

    image.tmdb.org 偶发被重置连接（实测约 1/6 概率），
    单次失败就放弃会让「刮削成功但没图」看起来像 bug。
    """
    import requests

    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            response = requests.get(url, timeout=45,
                                    headers={'User-Agent': 'MovSP/1.0'})
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(0.6 * (attempt + 1))
    raise last if last else RuntimeError('图片下载失败')


def _download_artwork(film: dict, url: str, kind: str, overwrite: bool = False) -> str:
    """下载图片到影片目录，失败时静默跳过。

    `kind` 决定目标文件名（严格按 Emby 规则）：
      * `cover`  竖版海报 → `{stem}.jpg`（剧集用 `poster.jpg`）
      * `fanart` 横版背景 → `{stem}-fanart.jpg`（剧集用 `fanart.jpg`）

    `overwrite=True` 时覆盖已有图片：重新刮削（例如从第一季换到第五季）
    必须能替换旧图，否则永远显示上一次的结果。

    注意：**不能**用 `{stem}-poster.jpg` 当竖版——那是 Emby 的 Primary 后缀，
    虽然也认，但与「同名图」语义重复且容易和横版混淆。
    """
    import requests

    directory = Path(str(film.get('dir_path') or ''))
    if not directory.is_dir():
        return ''
    is_series = film.get('kind') in ('series', 'episode')
    if is_series:
        target = directory / ('fanart.jpg' if kind == 'fanart' else 'poster.jpg')
    else:
        stem = Path(str(film.get('video_path') or '')).stem
        if not stem:
            return ''
        target = directory / (f'{stem}-fanart.jpg' if kind == 'fanart' else f'{stem}.jpg')
    if target.is_file() and not overwrite:
        return str(target)
    try:
        temporary = target.with_suffix('.jpg.part')
        temporary.write_bytes(_fetch_image(url))
        temporary.replace(target)
        return str(target)
    except (requests.RequestException, OSError) as exc:
        try:
            from . import oplog
            oplog.get('scrape').warning('图片下载失败 %s: %s', kind, exc)
        except Exception:  # noqa: BLE001
            pass
        return ''


def _refresh_series_after_task(film_id: str, status: str, error: str, task: dict | None) -> None:
    """剧集任务结束后刷新：写 tvshow.nfo，再按剧集逻辑重建索引（不改 kind）。"""
    if status == 'succeeded':
        apply_native_result(film_id, task or {})
        try:
            refresh_series(film_id)
        except ValueError:
            # 剧集刷新失败时不能再退回电影逻辑，否则 kind 会被改写成 movie
            pass
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif status == 'needs_choice':
        # 多结果时**不写入任何结果**：用户可能发现所有候选都不对，
        # 提前落盘会让他以为「已经改好了」，还得先清空才能重刮。
        # 只把状态挂成待确认，等用户选定后再按 ID 取详情。
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='needs_choice',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif status in ('failed', 'cancelled'):
        with connection() as db:
            db.execute("UPDATE films SET scrape_state=?,last_error=?,updated_at=? WHERE id=?",
                       ('failed', error or ('任务已取消' if status == 'cancelled' else '刮削失败'),
                        now_iso(), film_id))
    else:
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',updated_at=? WHERE id=?",
                       (now_iso(), film_id))


def refresh_film_after_task(film_id: str, status: str, error: str = '', task: dict | None = None) -> None:
    """任务结束后刷新影片索引：成功时重读磁盘（必要时跟随整理后的新路径），失败时保留状态与原因。"""
    if not film_id:
        return
    with connection() as db:
        row = db.execute('SELECT id, kind FROM films WHERE id=?', (film_id,)).fetchone()
    if not row:
        return
    # 剧集有自己的刷新路径：走电影逻辑会把 kind 覆盖成 movie
    if row['kind'] in ('series', 'episode'):
        _refresh_series_after_task(film_id, status, error, task)
        return
    if status == 'succeeded':
        # 标题路径不经过引擎，NFO 由这里按任务结果写回
        if (task or {}).get('scrape_path') == 'title':
            apply_native_result(film_id, task or {})
        if not _relocate_film(film_id, task or {}):
            _refresh_film(film_id)
        merged = _merge_nfo_snapshot(get_film(film_id, reconcile=False) or {'id': film_id})
        if merged:
            _refresh_film(film_id)
        # NFO 被第三方改写时，用任务里的资料兜底，保证媒体库立刻显示最新结果
        _fill_from_task_metadata(film_id, task or {})
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif status == 'needs_choice':
        # 多结果时**不写入任何结果**：用户可能发现所有候选都不对，
        # 提前落盘会让他以为「已经改好了」，还得先清空才能重刮。
        # 只把状态挂成待确认，等用户选定后再按 ID 取详情。
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='needs_choice',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif status in ('failed', 'cancelled'):
        _drop_snapshot(film_id)
        with connection() as db:
            db.execute("UPDATE films SET scrape_state=?,last_error=?,updated_at=? WHERE id=?",
                       ('failed', error or ('任务已取消' if status == 'cancelled' else '刮削失败'), now_iso(), film_id))
    else:
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',updated_at=? WHERE id=?", (now_iso(), film_id))


def film_ids(library_id: str = '', folder_id: str = '', status: str = '', query: str = '',
             group_by: str = '', group: str = '', limit: int = 2000) -> list[str]:
    """按媒体库的筛选条件取影片 id，供批量刮削使用（与列表页同一套条件）。

    剧集库不联网刮削，所以这里只取普通影片。
    """
    conditions, params = _conditions(library_id, folder_id, status, query, group_by, group)
    conditions.append("f.kind='movie'")
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    with connection() as db:
        rows = db.execute(
            f'SELECT f.id FROM films f{where} ORDER BY f.updated_at DESC, f.id LIMIT ?',
            [*params, max(1, int(limit))]).fetchall()
    return [row['id'] for row in rows]


def groups(by: str, library_id: str = '', query: str = '', folder_id: str = '') -> dict:
    if by not in ('actress', 'studio', 'year'):
        raise ValueError('不支持的分类方式')
    if folder_id:
        films_where = ' WHERE f.root_id=?'
        params = [folder_id]
    elif library_id:
        films_where = ' WHERE f.root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)'
        params = [library_id]
    else:
        films_where, params = '', []
    with connection() as db:
        if by == 'actress':
            rows = db.execute(f"""SELECT a.name AS key, count(*) AS count FROM film_actress a
                JOIN films f ON f.id=a.film_id{films_where}
                GROUP BY a.name ORDER BY count DESC, a.name""", params).fetchall()
            missing = db.execute(f"""SELECT count(*) FROM films f{films_where}
                {'AND' if films_where else 'WHERE'} NOT EXISTS (SELECT 1 FROM film_actress a WHERE a.film_id=f.id)""",
                                 params).fetchone()[0]
        elif by == 'studio':
            rows = db.execute(f"""SELECT coalesce(nullif(f.studio,''),'') AS key, count(*) AS count
                FROM films f{films_where} GROUP BY key ORDER BY count DESC, key""", params).fetchall()
            missing = 0
        else:
            rows = db.execute(f"""SELECT substr(coalesce(f.release_date,''),1,4) AS key, count(*) AS count
                FROM films f{films_where} GROUP BY key ORDER BY key DESC""", params).fetchall()
            missing = 0
        # 封面用一次窗口函数查询取完，避免每个分类都单独查一遍（637 个演员时会慢到数秒）
        if by == 'actress':
            cover_key = 'a.name'
            cover_join = ' JOIN film_actress a ON a.film_id=f.id'
        elif by == 'studio':
            cover_key = "coalesce(nullif(f.studio,''),'')"
            cover_join = ''
        else:
            cover_key = "substr(coalesce(f.release_date,''),1,4)"
            cover_join = ''
        cover_rows = db.execute(
            f"""SELECT key, film_id, updated_at FROM (
                SELECT {cover_key} AS key, f.id AS film_id, f.updated_at AS updated_at,
                       ROW_NUMBER() OVER (PARTITION BY {cover_key} ORDER BY f.updated_at DESC, f.id) AS rn
                FROM films f{cover_join}{films_where}
                {'AND' if films_where else 'WHERE'} f.cover_path<>''
            ) WHERE rn<=9""", params).fetchall()
        covers_by_key = {}
        for item in cover_rows:
            covers_by_key.setdefault(item['key'] or '', []).append(
                {'id': item['film_id'], 'v': item['updated_at']})
        items = []
        for row in rows:
            key = row['key'] or ''
            if by == 'actress' and not key:
                continue
            covers = covers_by_key.get(key, [])
            items.append({'key': key, 'name': key or {'actress': '未知演员', 'studio': '未知片商', 'year': '未知年份'}[by],
                          'count': row['count'], 'covers': covers})
        if by == 'actress' and missing:
            covers = [{'id': item['id'], 'v': item['updated_at']} for item in db.execute(
                f"""SELECT f.id,f.updated_at FROM films f{films_where}
                {'AND' if films_where else 'WHERE'} f.cover_path<>'' AND NOT EXISTS
                (SELECT 1 FROM film_actress a WHERE a.film_id=f.id)""", params).fetchall()[:9]]
            items.append({'key': '', 'name': '未知演员', 'count': missing, 'covers': covers})
        if query:
            items = [item for item in items if query.casefold() in item['name'].casefold()]
        total = sum(item['count'] for item in items)
        # “全部影片”卡片：与概览一致，给出总数和封面拼贴素材
        all_count = db.execute('SELECT count(*) FROM films f' + films_where, params).fetchone()[0]
        all_covers = [{'id': item['id'], 'v': item['updated_at']} for item in db.execute(
            f"""SELECT f.id,f.updated_at FROM films f{films_where}
            {'AND' if films_where else 'WHERE'} f.cover_path<>''
            ORDER BY f.updated_at DESC LIMIT 9""", params).fetchall()]
        stats = _stats(db, library_id=library_id, folder_id=folder_id)
    return {'by': by, 'groups': items, 'total': total,
            'all': {'count': all_count, 'covers': all_covers}, 'stats': stats}


# ---------------------------------------------------------------------------
# Metadata / NFO writing


# 不同条目对应的 NFO 根节点：电影 <movie>、剧集 <tvshow>、分集 <episodedetails>
NFO_ROOT_BY_KIND = {'movie': 'movie', 'series': 'tvshow', 'episode': 'episodedetails'}


def nfo_root_tag(kind: str) -> str:
    return NFO_ROOT_BY_KIND.get(kind or 'movie', 'movie')


def _nfo_target(film: dict) -> Path:
    """写入目标：始终用唯一的规范 NFO（避免同一部影片出现两个 NFO）。"""
    directory = Path(film['dir_path'])
    # 剧集条目没有视频文件，资料写在剧集目录的 tvshow.nfo
    if film.get('kind') == 'series':
        return directory / 'tvshow.nfo'
    video = Path(film['video_path'])
    canonical = canonical_nfo_path(directory, video)
    if canonical.is_file():
        return canonical
    # 多视频目录绝不复用 movie.nfo，否则一部影片的写入会串到别的影片
    if len(_folder_videos(directory)) <= 1 and film.get('nfo_path') and Path(film['nfo_path']).is_file():
        return Path(film['nfo_path'])
    return canonical


def _replace_text(root: ET.Element, tag: str, value: str) -> None:
    matches = list(root.findall(tag))
    if value:
        target = matches[0] if matches else ET.SubElement(root, tag)
        target.text = value
        for extra in matches[1:]:
            root.remove(extra)
    else:
        for match in matches:
            root.remove(match)


def _snapshot_nfo(film: dict) -> Path | None:
    """刮削前把现有 NFO 备份到快照目录，任务结束后用来补齐被引擎覆盖掉的字段。"""
    source = _nfo_target(film)
    if not source.is_file():
        return None
    try:
        NFO_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = NFO_SNAPSHOT_DIR / f"{film['id']}.nfo"
        snapshot.write_bytes(source.read_bytes())
        return snapshot
    except OSError:
        return None


def _snapshot_path(film_id: str) -> Path:
    return NFO_SNAPSHOT_DIR / f"{film_id}.nfo"


def _drop_snapshot(film_id: str) -> None:
    try:
        _snapshot_path(film_id).unlink(missing_ok=True)
    except OSError:
        pass


# 这些标签是“多值”结构（每个演员/分类/标签一个节点），缺失时整组补回
_MULTI_TAGS = {"actor", "genre", "tag"}
# 只在旧 NFO 有、新 NFO 完全没有时才补回的文本字段
_TEXT_TAGS = {"title", "originaltitle", "plot", "runtime", "rating", "premiered",
              "studio", "publisher", "director", "set", "country", "mpaa"}


def _merge_nfo_snapshot(film: dict) -> bool:
    """把刮削前的资料补回新 NFO：只补引擎没有写出的字段，不覆盖新抓到的值。"""
    snapshot = _snapshot_path(film['id'])
    if not snapshot.is_file():
        return False
    target = _nfo_target(film)
    if not target.is_file():
        _drop_snapshot(film['id'])
        return False
    # 只允许写回本片的 NFO：同名 NFO 或本片目录下的 movie.nfo
    video_dir = Path(str(film.get('video_path') or '')).parent
    stem = Path(str(film.get('video_path') or '')).stem.casefold()
    if target.parent != video_dir or target.stem.casefold() not in {stem, 'movie'}:
        _drop_snapshot(film['id'])
        return False
    try:
        old_root = ET.parse(snapshot).getroot()
    except (OSError, ET.ParseError):
        _drop_snapshot(film['id'])
        return False
    # 快照必须属于本片：番号对不上说明索引/路径错位，宁可放弃合并也不能串号
    film_dvdid = str(film.get('dvdid') or '').strip().upper()
    snapshot_dvdid = ''
    for node in old_root.findall('uniqueid'):
        if (node.get('type') or '').lower() == 'num' and (node.text or '').strip():
            snapshot_dvdid = node.text.strip().upper()
            break
    if film_dvdid and snapshot_dvdid and film_dvdid != snapshot_dvdid:
        _drop_snapshot(film['id'])
        return False
    try:
        new_root = ET.parse(target).getroot()
    except (OSError, ET.ParseError):
        _drop_snapshot(film['id'])
        return False
    changed = False
    for tag in _TEXT_TAGS:
        if list(new_root.findall(tag)):
            continue
        for node in old_root.findall(tag):
            if (node.text or '').strip():
                new_root.append(copy.deepcopy(node))
                changed = True
                break
    for tag in _MULTI_TAGS:
        if list(new_root.findall(tag)):
            continue
        for node in old_root.findall(tag):
            new_root.append(copy.deepcopy(node))
            changed = True
    # uniqueid(type=num/cid) 属于身份字段，新 NFO 缺失时补回
    existing_ids = {(node.get('type') or '').lower() for node in new_root.findall('uniqueid')}
    for node in old_root.findall('uniqueid'):
        kind = (node.get('type') or '').lower()
        if kind and kind not in existing_ids and (node.text or '').strip():
            new_root.append(copy.deepcopy(node))
            changed = True
    _drop_snapshot(film['id'])
    if not changed:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = target.with_suffix(target.suffix + '.bak')
        shutil.copy2(target, backup)
        temporary = target.with_suffix(target.suffix + '.tmp')
        tree = ET.ElementTree(new_root)
        ET.indent(tree, space='  ')
        tree.write(temporary, encoding='utf-8', xml_declaration=True)
        temporary.replace(target)
    except OSError:
        return False
    return True


def _write_nfo(film: dict, values: dict, expected_mtime: float | None) -> Path:
    kind = film.get('kind') or 'movie'
    root_tag = nfo_root_tag(kind)
    target = _nfo_target(film)
    if expected_mtime is not None and target.is_file():
        actual = target.stat().st_mtime
        if abs(actual - float(expected_mtime)) > 0.001:
            raise FileExistsError('NFO 已被其他程序修改，请重新读取后再保存')
    head_comments = ''
    if target.is_file():
        try:
            raw = target.read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            raw = ''
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith('<?xml') or not stripped:
                continue
            if stripped.startswith('<!--'):
                head_comments += line.rstrip() + '\n'
                continue
            break
        try:
            root = ET.parse(target).getroot()
        except ET.ParseError:
            # 磁盘上的 .nfo 不是合法 XML（常见于被 PT 站的发布说明、
            # BBCode 文本覆盖）。此时不能崩掉保存：
            # 保留原文件做一份 .bak，然后用全新结构重建。
            try:
                shutil.copy2(target, target.with_suffix(target.suffix + '.bak'))
            except OSError:
                pass
            root = ET.Element(root_tag)
        if str(root.tag).lower() != root_tag:
            raise ValueError(f'现有 NFO 根节点不是 {root_tag}，已停止写入')
    else:
        root = ET.Element(root_tag)
    _replace_text(root, 'title', values.get('title', ''))
    _replace_text(root, 'originaltitle', values.get('original_title', ''))
    _replace_text(root, 'plot', values.get('plot', ''))
    if kind == 'series':
        # <tvshow>：没有番号、没有时长，年份与首播日期分开写
        _replace_text(root, 'showtitle', values.get('title', ''))
        _replace_text(root, 'premiered', values.get('release_date', ''))
        _replace_text(root, 'year', (values.get('release_date') or '')[:4])
        _replace_text(root, 'studio', values.get('studio', ''))
        # 剧集也用 Kodi/tMM 标准外部 ID
        for item in list(root.findall('uniqueid')):
            if (item.get('type') or '').lower() in ('tmdb', 'imdb'):
                root.remove(item)
        for external, value in (('tmdb', values.get('tmdb_id')), ('imdb', values.get('imdb_id'))):
            if value:
                ET.SubElement(root, 'uniqueid', {'type': external}).text = str(value)
    elif kind == 'episode':
        # <episodedetails>：播出日期写 aired，季集号以索引为准
        _replace_text(root, 'aired', values.get('release_date', ''))
        _replace_text(root, 'season', str(film.get('season') or 0))
        _replace_text(root, 'episode', str(film.get('episode') or 0))
        _replace_text(root, 'runtime', str(values['runtime']) if values.get('runtime') else '')
    else:
        _replace_text(root, 'premiered', values.get('release_date', ''))
        _replace_text(root, 'studio', values.get('studio', ''))
        _replace_text(root, 'publisher', values.get('publisher', ''))
        _replace_text(root, 'director', values.get('director', ''))
        _replace_text(root, 'runtime', str(values['runtime']) if values.get('runtime') else '')
        for item in list(root.findall('uniqueid')):
            if (item.get('type') or '').lower() in ('num', 'tmdb', 'imdb'):
                root.remove(item)
        if values.get('dvdid'):
            ET.SubElement(root, 'uniqueid', {'type': 'num', 'default': 'true'}).text = values['dvdid']
        # 普通电影用 Kodi/tMM 标准的外部 ID（不写 num，避免被当成番号）
        if kind == 'movie':
            for external, value in (('tmdb', values.get('tmdb_id')), ('imdb', values.get('imdb_id'))):
                if value:
                    ET.SubElement(root, 'uniqueid', {'type': external}).text = str(value)
    for item in list(root.findall('actor')):
        root.remove(item)
    for name in values.get('actress') or []:
        actor = ET.SubElement(root, 'actor')
        ET.SubElement(actor, 'name').text = name
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        backup = target.with_suffix(target.suffix + '.bak')
        shutil.copy2(target, backup)
    temporary = target.with_suffix(target.suffix + '.tmp')
    tree.write(temporary, encoding='utf-8', xml_declaration=True)
    if head_comments:
        text = temporary.read_text(encoding='utf-8')
        declaration, separator, rest = text.partition('\n')
        temporary.write_text(f'{declaration}{separator}{head_comments}{rest}', encoding='utf-8')
    temporary.replace(target)
    return target


def _status_for_update(film: dict, payload: dict, nfo_path: str) -> tuple[str, list[str]]:
    """保存资料后重算状态：电影沿用必需字段校验，剧集/分集按标题+封面判断。"""
    kind = film.get('kind') or 'movie'
    if kind == 'series':
        if payload['title'] and film.get('cover_path'):
            return 'complete', []
        if payload['title'] or film.get('cover_path') or nfo_path:
            return 'partial', []
        return 'unscraped', []
    if kind == 'episode':
        has_thumb = bool(film.get('cover_path'))
        if payload['title'] and has_thumb:
            return 'complete', []
        if payload['title'] or nfo_path:
            return 'partial', []
        return 'unscraped', []
    # 电影：有外部 ID（TMDb / IMDb）的按外部 ID 判定，不再强制要求番号
    if payload.get('tmdb_id') or payload.get('imdb_id'):
        missing = []
        if not payload['title']:
            missing.append('title')
        if not film.get('cover_path'):
            missing.append('cover')
        return ('complete' if not missing else 'partial'), missing
    return _status_for(payload['dvdid'], payload['title'], film['cover_path'],
                       bool(nfo_path), '', payload['actress'], payload['studio'], payload['release_date'])


def update_film(film_id: str, values: dict, write_nfo: bool = True,
                expected_nfo_mtime: float | None = None) -> dict | None:
    film = get_film(film_id, reconcile=False)
    if not film:
        return None
    dvdid = str(values.get('dvdid') or '').strip().upper()
    # 外部 ID 只在「显式提供」时才改：
    # 前端在没有主动修改 ID 时会省略这两个键，此时保留库里的值。
    # 否则候选面板刚选定的新 ID 会被输入框里的旧值覆盖回上一次的结果。
    if 'tmdb_id' in values:
        tmdb_id = str(values.get('tmdb_id') or '').strip()
    else:
        tmdb_id = str(film.get('tmdb_id') or '').strip()
    if 'imdb_id' in values:
        imdb_id = str(values.get('imdb_id') or '').strip()
    else:
        imdb_id = str(film.get('imdb_id') or '').strip()
    payload = {
        'dvdid': dvdid, 'tmdb_id': tmdb_id, 'imdb_id': imdb_id,
        'title': str(values.get('title') or '').strip(),
        'original_title': str(values.get('original_title') or '').strip(),
        'plot': str(values.get('plot') or '').strip(),
        'release_date': str(values.get('release_date') or '').strip(),
        'studio': str(values.get('studio') or '').strip(),
        'publisher': str(values.get('publisher') or '').strip(),
        'director': str(values.get('director') or '').strip(),
        'runtime': int(values.get('runtime') or 0),
        'actress': [str(item).strip() for item in values.get('actress') or [] if str(item).strip()],
    }
    nfo_path = film['nfo_path']
    if write_nfo:
        nfo_path = str(_write_nfo(film, payload, expected_nfo_mtime))
    status, missing = _status_for_update(film, payload, nfo_path)
    # 用户保存后，标题就属于他自己了：后续重扫的文件名推测不能覆盖
    title_source = 'manual' if payload['title'] else ''
    # 剧集季号：只由用户在资料页填写，决定刮削取哪一季
    season_number = 0
    if film.get('kind') == 'series' and 'season_number' in values:
        try:
            season_number = max(0, min(200, int(values.get('season_number') or 0)))
        except (TypeError, ValueError):
            season_number = 0
    else:
        season_number = int(film.get('season_number') or 0)
    with connection() as db:
        db.execute("""UPDATE films SET dvdid=?,dvdid_source=?,tmdb_id=?,imdb_id=?,title_source=?,
            title=?,original_title=?,plot=?,release_date=?,
            studio=?,publisher=?,director=?,runtime=?,actress_text=?,nfo_path=?,nfo_mtime=?,status=?,
            missing_fields=?,season_number=?,updated_at=? WHERE id=?""",
                   (payload['dvdid'], 'manual' if dvdid else film['dvdid_source'],
                    payload['tmdb_id'], payload['imdb_id'], title_source, payload['title'],
                    payload['original_title'], payload['plot'], payload['release_date'], payload['studio'],
                    payload['publisher'], payload['director'], payload['runtime'], '、'.join(payload['actress']),
                    nfo_path, Path(nfo_path).stat().st_mtime if nfo_path and Path(nfo_path).exists() else 0,
                    status, json.dumps(missing, ensure_ascii=False), season_number, now_iso(), film_id))
        db.execute('DELETE FROM film_actress WHERE film_id=?', (film_id,))
        db.executemany('INSERT OR IGNORE INTO film_actress(film_id,name,ord) VALUES(?,?,?)',
                       [(film_id, name, index) for index, name in enumerate(payload['actress'])])
    return get_film(film_id, reconcile=False)


def read_nfo(film_id: str) -> dict | None:
    film = get_film(film_id, reconcile=False)
    if not film:
        return None
    target = _nfo_target(film)
    if not target.is_file():
        return {'content': '', 'path': str(target), 'mtime': 0}
    return {'content': target.read_text(encoding='utf-8', errors='replace'), 'path': str(target),
            'mtime': target.stat().st_mtime}


def _season_dir(film: dict, season: int) -> Path:
    """季目录：多季时 NFO 放在 Sxx 子目录，单季就放在剧集目录。"""
    base = Path(film['dir_path'])
    try:
        for child in base.iterdir():
            if child.is_dir() and series_scan.season_from_dir_name(child.name) == int(season):
                return child
    except OSError:
        pass
    return base


def _download_episode_still(episode: dict, url: str, overwrite: bool = True) -> str:
    """下载分集剧照为 `<视频名>-thumb.jpg`（Emby/Jellyfin 认这个后缀）。

    默认覆盖：重新刮削某一季时必须替换旧剧照，
    否则切季之后仍然显示上一季的图。
    """
    import requests

    video = Path(str(episode.get('video_path') or ''))
    if not video.name:
        return ''
    directory = video.parent
    if not directory.is_dir():
        return ''
    target = directory / f'{video.stem}-thumb.jpg'
    if target.is_file() and not overwrite:
        return str(target)
    try:
        temporary = target.with_suffix('.jpg.part')
        temporary.write_bytes(_fetch_image(url))
        temporary.replace(target)
        return str(target)
    except (requests.RequestException, OSError) as exc:
        try:
            from . import oplog
            oplog.get('scrape').warning('分集剧照下载失败 %s: %s', target.name, exc)
        except Exception:  # noqa: BLE001
            pass
        return ''


def apply_season_result(series_id: str, season: int, data: dict) -> dict:
    """把一整季的分集资料写进各集的 NFO，并同步索引。

    data 来自 `native_scrape.scrape_season()`：
        {"name", "overview", "poster", "episodes": [{episode, title, plot,
          air_date, runtime, still}]}

    行为：
      * 季级资料写 season NN.nfo（放在季目录，没有季目录就放剧集目录）
      * 每集按 `episode` 号匹配，写 <episodedetails>
      * 只覆盖**抓到的字段**，用户手填的其它字段保留
    """
    series = get_film(series_id, reconcile=False)
    if not series or series.get('kind') != 'series':
        raise ValueError('不是剧集条目')
    episodes = list((data or {}).get('episodes') or [])
    if not episodes:
        raise ValueError('这一季没有取到分集资料')

    # 季目录里的分集条目：按 episode 号建索引（带上视频路径，用于落盘剧照）
    with connection() as db:
        rows = db.execute(
            "SELECT id, episode, video_path, dir_path FROM films "
            "WHERE parent_id=? AND kind='episode' AND season=?",
            (series_id, int(season))).fetchall()
    by_number = {int(row['episode']): dict(row) for row in rows if row['episode'] is not None}

    updated: list[int] = []
    missing: list[int] = []
    for item in episodes:
        number = int(item.get('episode') or 0)
        target = by_number.get(number)
        if not target:
            missing.append(number)
            continue
        film_id = target['id']
        values = {
            'title': str(item.get('title') or '').strip(),
            'plot': str(item.get('plot') or '').strip(),
            'release_date': str(item.get('air_date') or '').strip(),
            'runtime': int(item.get('runtime') or 0),
            'actress': [],
        }
        if not any(values[key] for key in ('title', 'plot', 'release_date')):
            continue
        try:
            update_film(film_id, values, write_nfo=True)
            # 分集剧照：写成 `<视频名>-thumb.jpg`，Emby/Jellyfin 按这个后缀认
            still_url = str(item.get('still') or '').strip()
            if still_url.startswith(('http://', 'https://')):
                _download_episode_still(target, still_url)
            updated.append(number)
        except (OSError, ValueError, ET.ParseError):
            missing.append(number)

    # 季级资料
    season_written = False
    season_name = str((data or {}).get('name') or '').strip()
    season_plot = str((data or {}).get('overview') or '').strip()
    if season_name or season_plot:
        root = ET.Element('season')
        ET.SubElement(root, 'seasonnumber').text = str(int(season))
        if season_name:
            ET.SubElement(root, 'title').text = season_name
        if season_plot:
            ET.SubElement(root, 'plot').text = season_plot
        target = _season_dir(series, int(season))
        target.mkdir(parents=True, exist_ok=True)
        path = target / 'season.nfo'
        if path.is_file():
            try:
                shutil.copy2(path, path.with_suffix(path.suffix + '.bak'))
            except OSError:
                pass
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(
            ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8'),
            encoding='utf-8')
        temporary.replace(path)
        season_written = True

    try:
        refresh_series(series_id)
    except ValueError:
        pass
    return {
        'season': int(season),
        'updated': sorted(updated),
        'missing': sorted(missing),
        'season_nfo': season_written,
    }


def read_season_nfo(series_id: str, season: int) -> dict:
    film = get_film(series_id, reconcile=False)
    if not film or film.get('kind') != 'series':
        raise ValueError('不是剧集条目')
    target = _season_dir(film, season) / 'season.nfo'
    if not target.is_file():
        return {'content': '', 'path': str(target), 'mtime': 0, 'season': int(season)}
    return {'content': target.read_text(encoding='utf-8', errors='replace'), 'path': str(target),
            'mtime': target.stat().st_mtime, 'season': int(season)}


def write_season_nfo(series_id: str, season: int, content: str,
                     expected_mtime: float | None = None) -> dict:
    """写入某一季的 season.nfo（根节点 season）。"""
    film = get_film(series_id, reconcile=False)
    if not film or film.get('kind') != 'series':
        raise ValueError('不是剧集条目')
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f'NFO XML 格式错误：{exc}') from exc
    if str(root.tag).lower() != 'season':
        raise ValueError('季 NFO 的根节点必须是 season')
    target = _season_dir(film, season) / 'season.nfo'
    if expected_mtime is not None and target.is_file() and \
            abs(target.stat().st_mtime - float(expected_mtime)) > 0.001:
        raise FileExistsError('NFO 已被其他程序修改，请重新读取后再保存')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        shutil.copy2(target, target.with_suffix(target.suffix + '.bak'))
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(target)
    return read_season_nfo(series_id, season)


def _apply_nfo_fields(film_id: str, content: str) -> None:
    """剧集/分集直接改原始 NFO 后，把常见字段回写到索引。

    电影走 `_refresh_film`（按文件重建条目），但剧集条目没有自己的视频，
    用那套逻辑会把 series 记录冲成电影记录，所以这里只按标签回填。
    """
    film = get_film(film_id, reconcile=False)
    if not film:
        return
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return

    def text(tag: str) -> str:
        return (root.findtext(tag) or '').strip()

    actors = []
    for node in root.findall('actor'):
        name = (node.findtext('name') or '').strip()
        if name:
            actors.append(name)
    payload = {
        'title': text('title') or film['title'],
        'original_title': text('originaltitle') or film['original_title'],
        'plot': text('plot') or film['plot'],
        'release_date': text('premiered') or text('aired') or text('year') or film['release_date'],
        'studio': text('studio') or film['studio'],
        'actress': actors or film['actress'],
    }
    target = _nfo_target(film)
    status, missing = _status_for_update(film, {**payload, 'dvdid': film['dvdid']}, str(target))
    with connection() as db:
        db.execute("""UPDATE films SET title=?,original_title=?,plot=?,release_date=?,studio=?,
            actress_text=?,nfo_path=?,nfo_mtime=?,status=?,missing_fields=?,updated_at=? WHERE id=?""",
                   (payload['title'], payload['original_title'], payload['plot'], payload['release_date'],
                    payload['studio'], '、'.join(payload['actress']), str(target),
                    target.stat().st_mtime if target.is_file() else 0,
                    status, json.dumps(missing, ensure_ascii=False), now_iso(), film_id))
        db.execute('DELETE FROM film_actress WHERE film_id=?', (film_id,))
        db.executemany('INSERT OR IGNORE INTO film_actress(film_id,name,ord) VALUES(?,?,?)',
                       [(film_id, name, index) for index, name in enumerate(payload['actress'])])


def write_raw_nfo(film_id: str, content: str, expected_mtime: float | None = None) -> dict | None:
    film = get_film(film_id, reconcile=False)
    if not film:
        return None
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f'NFO XML 格式错误：{exc}') from exc
    expected = nfo_root_tag(film.get('kind') or 'movie')
    if str(root.tag).lower() != expected:
        raise ValueError(f'NFO 根节点必须是 {expected}')
    target = _nfo_target(film)
    if expected_mtime is not None and target.is_file() and abs(target.stat().st_mtime - float(expected_mtime)) > 0.001:
        raise FileExistsError('NFO 已被其他程序修改，请重新读取后再保存')
    if target.is_file():
        shutil.copy2(target, target.with_suffix(target.suffix + '.bak'))
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(target)
    if film.get('kind') in ('series', 'episode'):
        _apply_nfo_fields(film_id, content)
    else:
        _refresh_film(film_id, preserve_manual=False)
    return read_nfo(film_id)


# ---------------------------------------------------------------------------
# Scrape integration


def start_scrape_with_external(film_id: str, *, tmdb_id: str | None = None,
                               imdb_id: str | None = None,
                               preset_id: str | None = None) -> dict:
    """用户从候选面板选定一部：记下外部 ID，并立即按该 ID 重刮。

    记下 ID 之后，后续每次刮削都直接按 ID 取，不会再出现同名歧义。
    """
    film = get_film(film_id, reconcile=False)
    if not film:
        raise ValueError('影片不存在')
    tmdb = str(tmdb_id or '').strip()
    imdb = str(imdb_id or '').strip()
    if not tmdb and not imdb:
        raise ValueError('请选择一条候选结果')
    with connection() as db:
        db.execute("UPDATE films SET tmdb_id=?,imdb_id=?,updated_at=? WHERE id=?",
                   (tmdb or film.get('tmdb_id') or '', imdb or film.get('imdb_id') or '',
                    now_iso(), film_id))
    return start_scrape(film_id, None, preset_id,
                        external={'tmdb': tmdb, 'imdb': imdb})


def start_scrape(film_id: str, dvdid: str | None = None, preset_id: str | None = None,
                 external: dict | None = None) -> dict:
    from . import tasks
    film = get_film(film_id)
    if not film:
        raise ValueError('影片不存在')
    is_series = film.get('kind') in ('series', 'episode')
    if film['scrape_state'] in ('queued', 'running'):
        raise ValueError('该影片正在刮削')
    if is_series:
        # 剧集条目没有视频文件，刮的是剧集目录（写 tvshow.nfo）
        target_path = Path(film['dir_path']) if film.get('dir_path') else None
        if not target_path or not target_path.is_dir():
            raise ValueError('剧集目录不存在或不可访问')
        video = target_path
    else:
        video = Path(film['video_path'])
        if not video.is_file():
            raise ValueError('视频文件不存在或不可访问')
    if dvdid is not None:
        value = str(dvdid).strip().upper()
        if value:
            with connection() as db:
                db.execute("UPDATE films SET dvdid=?,dvdid_source='manual',updated_at=? WHERE id=?",
                           (value, now_iso(), film_id))
            film['dvdid'] = value
    forced = film['dvdid'] or None
    # 影片已经记下外部 ID（从 NFO 读到或用户手填）时，直接按 ID 刮，不再按标题搜索 ——
    # 这是「同名影片搜错」的唯一根治办法，也避免了保存 ID 后仍然报错。
    if not external:
        existing_tmdb = str(film.get('tmdb_id') or '').strip()
        existing_imdb = str(film.get('imdb_id') or '').strip()
        if existing_tmdb or existing_imdb:
            external = {'tmdb': existing_tmdb, 'imdb': existing_imdb}
    preset = preset_id or library_preset_for_folder(film['root_id']) or 'default'
    # D1：JAV 路径必须有番号。番号为空时直接跳过，不创建必然失败的任务 ——
    # 用户可以先在「编辑资料」里手填番号，再点刮削。
    from . import scrapers as scraper_registry
    resolved = scraper_registry.resolve_path(preset)
    # 剧集只按标题刮削：JAV 预设没有可用的剧集刮削器，先说清楚
    if is_series and resolved['path'] == 'jav':
        raise ValueError('剧集不能按番号刮削；请给这个媒体库选择含电影/剧集刮削器的预设')
    # D1：JAV 路径必须有番号。番号为空时直接跳过，不创建必然失败的任务 ——
    # 用户可以先在「编辑资料」里手填番号，再点刮削。
    if resolved['path'] == 'jav' and not forced:
        raise ValueError('该预设只按番号刮削，这部影片缺少番号。请在「编辑资料」里填写番号后再刮削')
    # 引擎写 NFO 是整份重建：先留一份旧资料，任务结束后补回没被抓到的字段
    _snapshot_nfo(film)
    # 刮削的输入**只来自资料页**（即索引里已存的字段），绝不回头读文件夹名：
    #   manual    — 用户手动改过，最高优先
    #   nfo       — 磁盘 NFO 里的标题/年份，等同用户资料
    #   suggested — 文件名清洗出来的「推荐值」，只是预填，**不能**直接拿去搜
    # 所以只有 manual / nfo 才作为查询依据；suggested 说明用户还没确认，
    # 宁可退回标题推导（会走既有兜底），也不把猜测当成用户输入。
    hint = None
    hint_year = None
    source = str(film.get('title_source') or '')
    if source in ('manual', 'nfo'):
        hint = str(film.get('title') or '').strip() or None
        # 年份同样以资料页为准：NFO 的发行日期优先，其次从标题推
        release = str(film.get('release_date') or '').strip()
        if release[:4].isdigit():
            hint_year = int(release[:4])
    task = tasks.create_task(str(video), preset, source='library', forced_avid=forced,
                             library_film_id=film_id, external_ids=external,
                             is_series=is_series, title_hint=hint, year_hint=hint_year,
                             # 剧集季号：决定 TMDb 取 tv/{id}/season/{n}
                             season_hint=int(film.get('season_number') or 0) or None)
    with connection() as db:
        db.execute("UPDATE films SET scrape_state='queued',last_task_id=?,last_error='',updated_at=? WHERE id=?",
                   (task['id'], now_iso(), film_id))
    return {'task_id': task['id'], 'film': get_film(film_id, reconcile=False)}


def library_preset_for_folder(root_id: str) -> str:
    """影片所属媒体库的默认预设（一个文件夹可能属于多个库，取最早创建的那个）"""
    with connection() as db:
        row = db.execute(
            """SELECT l.preset_id FROM libraries l JOIN library_roots lr ON lr.library_id=l.id
               WHERE lr.root_id=? ORDER BY l.created_at LIMIT 1""", (root_id,)).fetchone()
    return (row['preset_id'] if row else '') or ''


def library_for_folder(root_id: str) -> dict | None:
    with connection() as db:
        row = db.execute(
            """SELECT l.* FROM libraries l JOIN library_roots lr ON lr.library_id=l.id
               WHERE lr.root_id=? ORDER BY l.created_at LIMIT 1""", (root_id,)).fetchone()
        return _library_from_row(db, row) if row else None
