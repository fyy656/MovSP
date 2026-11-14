# memory.md — MovSP 项目记忆

> 给后续接手的人/AI：先读这一页，再动手。最后更新：2026-10-23。

## 1. 项目与仓库

| 项 | 值 |
|---|---|
| 仓库 | `fyy656/JavSP-Web-Next`（GitHub），GPL-3.0 |
| 上游来源 | JavSP（刮削引擎，vendor/JavSP）、JavSP-Web（网页端，apecme 作者） |
| 技术栈 | 后端 FastAPI + SQLite；前端无框架（原生 JS + 少量 ArtPlayer/hls.js） |
| 已发布版本 | **v2.2.2**（main 分支，2026-10-20） |
| 当前开发分支 | **`feat/movie-library`**（未合并、未推送） |
| 默认端口 | `launcher.py` 启动，通常 8090 |

**硬规则**
- **没有明确指令不要 commit / push**（历史上有过约定："以后 git 推送操作必须要等我指令"）。
- 分支 `feat/movie-library` 是半成品大改，合并前必须跑全量测试。
- 不要把任何 API Key、真实路径、内网 IP 写进仓库文件；`data/` 已被 gitignore（`data/presets.json` 里有用户自己的 TMDb Key）。
- 删除/移动文件只能在本机执行；播放相关的"打开目录/系统选择框/还原文件/删除图片"接口已做本机校验（`_is_local_client`）。

## 2. 运行与测试

```powershell
# 启动
C:\ProgramData\miniconda3\envs\py311\python.exe .\launcher.py

# Python 测试（520 项）
$env:MOVSP_DATA_DIR = $null
$env:PYTHONPATH = (Join-Path $env:TEMP 'dsh-shim')
C:\ProgramData\miniconda3\envs\py311\python.exe -m unittest discover -s tests -p 'test_*.py'

# 前端测试（11 个文件，逐个跑）
node tests\test_library_client.cjs      # 媒体库前端
node tests\test_movies_client.cjs       # 影视库前端
node tests\test_web_client.cjs
node tests\test_library_playback.cjs
node tests\test_demo.cjs
# 其它：test_scan_client / test_mount_batch_client / test_ai_translation_client
#       test_cookiecloud_client / test_comments_client / test_community_client

# 改了 movsp/web/assets/* 之后必须重建演示站，否则 test_demo 会失败
python scripts\build_demo.py
```

## 3. 架构地图

```
movsp/
├─ server.py          所有 HTTP 路由（含 /api/library/* 统一媒体库、/api/movies/* 影视条目）
├─ media.py           Emby/Jellyfin 客户端 + 播放地址生成（film_playback）+ 播放停止
├─ streaming.py       本地直连的分段流（Range/206 + 客户端断开检测 + 主动叫停）
├─ oplog.py           统一日志：movsp.playback / movsp.scrape
├─ storage.py         预设、媒体服务器、下载器等 JSON 存储（含 MOVIE_PRESET 种子）
├─ library_store.py   成人媒体库：目录扫描、films 表、NFO 读写、分类统计
├─ movies.py          影视库（电影/剧集）：movies.db、文件夹扫描、TMDb 刮削、NFO 写回
├─ movie_nfo.py       Kodi / tinyMediaManager 兼容的 movie / tvshow / episode NFO
├─ tmdb.py            TMDb 客户端（base/key/language/proxy 可配，含剧集接口）
└─ tasks.py           JAV 刮削任务（子进程调用 vendor/JavSP 引擎）
```

**两套数据存储**
- 成人库：`data/library.db`（`libraries` / `roots` / `films` …），`libraries.kind='adult'`
- 影视库：`data/movies.db`（`movie_libraries` / `movie_items`），kind = `movie` / `series`
- 统一入口：`/api/library/libraries` 把两者合并成同一结构（`kind`、`source`、`item_count`、`folders`）

**媒体库模型**
| kind | 条目来源 | 识别 | 刮削 | 库内视图 |
|---|---|---|---|---|
| adult | 本地文件夹 | 番号识别（预设规则） | JAV 引擎（tasks.py 子进程） | 番号海报墙 + 分类 |
| movie | 本地文件夹（一目录一部） | 目录名 | TMDb `/movie/{id}` | 电影海报墙 |
| series | 本地文件夹 | `SxxExx` / 第X集 | TMDb `/tv/{id}` + 每集 | 剧集卡 → 头部 + 季切换 + 分集行 |

## 4. 播放链路（重点，改动前务必理解）

```
前端 library.js
  openPlayer(id, {playbackBase})            // 成人: /api/library/films，影视: /api/movies/items
    → GET {base}/{id}/playback?prefer=&start=&caps=
        → media.film_playback(film, prefer, start, caps, stream_base)
            ├─ 本地可读 → mode=local，url={stream_base}/{id}/stream
            └─ 否则找 Emby 条目（按路径→文件名，不看标题）
                 → PlaybackInfo（DirectPlay/DirectStream 开启，除非 prefer=transcode）
                 → URL 优先级：DirectPlay(直连) > DirectStream(直封装) > TranscodingUrl(HLS)
    → ArtPlayer + hls.js 播放（m3u8 用 customType）
    → 关闭时 abortMediaElement + POST {base}/{id}/playback/stop
```

**关键决定与坑**
- **`stream_base` 必须传对**：写死成 `/api/library/films` 会导致影视条目 404 → 卡住 → 转码，表现为"电影库播放变慢"（已修，别再写死）。
- **DirectPlay/DirectStream 默认开启**：强制转码会让 4K HEVC 把 NAS CPU 打满；直封装只换容器不重编码。
- **前端会带 `caps`**（h264/hevc/av1/vp9）：浏览器不支持 HEVC 时服务端直接转码，省掉一次必然失败的直连。
- **本地直连卡住检测 3 秒**（转码 6 秒）：本地流要么立刻出画，要么就是编码不支持。
- **关闭播放器必须停流**：`streaming.stop_stream` 注册表 + 前端 `keepalive` 请求，否则磁盘/网络一直跑。
- **`dialog.close()` 的 close 事件是异步的**：切换模式时用 `destroyPlayer({keepDialog:true})` + `state.playerSwitching` 屏蔽，否则新播放器被误关、残留声音。
- **不再显示/使用番号称呼**：UI 统一叫 **ID**（JAV=番号，电影=TMDb id），**演员**（原女优）。

## 5. 刮削

- **成人**：预设 `default`，JAV 引擎在 `vendor/JavSP`，按 `tasks.py` 子进程跑，日志 `movsp.scrape`。
- **电影/剧集**：预设 `movie`（`影视（非成人）`，不含任何 JAV 爬虫）
  - `form.network`：`tmdb_base`（默认官方 `https://api.themoviedb.org/3`）、`tmdb_api_key`、`tmdb_language`、`proxy_server`
  - `form.movie`：`write_nfo`（是否写回，写前备份 `.nfo.bak`）、`download_artwork`
  - 实测该机器可用的 TMDb 地址是 **`https://api.tmdb.org/3`**（官方域名不可达）；Key 存在用户本地 `data/presets.json`，不要外泄
- **NFO**：优先读目录里已有的 tMM/Kodi NFO（电影 `<影片名>.nfo`/`movie.nfo`，剧集 `tvshow.nfo` + 每集同名 nfo）；缺资料再用 TMDb 补。
- **海报**：`-poster.jpg` / `poster.jpg` / `folder.jpg` / `cover.jpg`；分集剧照 `<集名>-thumb.jpg`；影视条目没有本地图时回落 Emby 图片接口。

## 6. 已知问题 / 待办

1. **剧集批量刮削**：目前要逐集点"刮削"，缺"整部剧一次刮完（含所有分集）"。
2. **示例扫描性能**：1330 条真实剧集约 15 秒（已从 34.6 秒优化），还能再快（减少 stat/NFO 解析）。
3. `/api/movies/*` 与 `/api/library/*` 并存，长期应合并（`movies.js` 只作为影视详情/播放的底层接口保留）。
4. 剧集详情还可以补：演员表、海报更换、观看进度（现在只用 localStorage 记影片进度）。
5. 电影详情页缺"换海报/换背景图"。
6. 合并分支前需要：全量回归 + 真实库点测（新建三类库 → 扫描 → 刮削 → 播放）+ 版本号与发布说明。

## 7. 用户偏好（重要）

- 中文交流；回答要短、给结论 + 证据（实测数字/日志）。
- 偏好"一个统一入口"，不喜欢功能重复或概念分裂（所以才有 ID/演员 统一、媒体库统一）。
- 强调性能，特别是 4K HEVC 的转码成本；优先直连/直封装。
- 剧集布局希望参考成熟项目（Emby/Jellyfin 风格：剧 → 季 → 集）。
- 发布流程：`VERSION` + `movsp/__init__.py` + `docs/RELEASE_NOTES_vX.Y.Z.md` + tag `vX.Y.Z`（workflow 自动发 release + EXE + Pages）。
