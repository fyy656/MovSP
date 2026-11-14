# 交接报告 — `feat/movie-library` 分支

> 生成时间：2026-10-23 · 分支：`feat/movie-library`（**未合并、未推送**）· 基线：`main` = v2.2.2

---

## 一、这次做了什么（一句话）

在 **v2.2.2 基础上**，把「只刮 JAV」的工具，扩展成「**通用刮削 + 播放服务**」的第一步：

1. 新增**电影 / 剧集**两类媒体库，**直接从本地文件夹导入**（不依赖 Emby 建库）
2. 用 **TMDb** 刮削电影与剧集，读写 **tMM/Kodi 兼容 NFO**
3. 媒体库**合并成单一入口**，新建时选类型（成人 / 电影 / 剧集）
4. 术语统一为 **ID / 演员**，为「一套界面维护三种库」打基础
5. 播放链路支持 **直连 / 直封装 / 转码** 三档，并修复影视库播放性能倒退

---

## 二、分支提交（16 个，按时间）

| 提交 | 内容 |
|---|---|
| `ce3e689` | 影视库初版：Emby 条目 + TMDb + NFO 读写（`movies.py` / `tmdb.py` / `movie_nfo.py`） |
| `f3bc2d2` | 预设编辑器加「影视」页签与 TMDb 字段说明 |
| `d6f38a2` | 播放改为直连/直封装优先，客户端上报编码能力（`caps`） |
| `1a2a98a` + `2b9d422` | 术语统一：番号→ID、女优→演员（含测试） |
| `10c7032` | 剧集 NFO（tvshow/episode）与 TMDb 剧集接口 |
| `3d1abee` | 未配置媒体服务器时的播放提示 |
| `b0396a9` | `libraries.kind`（adult/movie/series）+ 自动补列 |
| `3c596f2` | 电影/剧集从文件夹导入（`folder_paths`、`scan_folder_library`、季集解析） |
| `651670a` | 统一 `/api/library/*`（列表/创建/扫描分派/统一条目）；修 movies 连接泄漏 |
| `b0e5694` | 剧集与单集刮削接线（tvshow.nfo / SxxExx.nfo） |
| `7d88d4c` | 前端合并为单一媒体库入口，按 kind 渲染 |
| `63615ac` | 文档改写 + 文件夹海报识别 |
| `a27ecc0` | **修复电影库直连地址错误**导致的性能倒退（`stream_base`） |
| `2af6702` | 扫描批量化（34.6s→15.9s）、影视库增删文件夹、剧集按季显示 |
| `04f49c8` | 影视详情统一为成人库外观；剧集页改「头部 + 季切换 + 分集行」 |

统计：28 个文件变更，+4734 / −217。

---

## 三、当前可用能力（已实测）

| 能力 | 状态 | 证据 |
|---|---|---|
| 新建成人 / 电影 / 剧集库（选文件夹 + 预设） | ✅ | `tests/test_unified_library.py` 10 项 |
| 电影库扫描（一目录一部） | ✅ | 真实库 `华语电影` 21 部 0.6s |
| 剧集库扫描（SxxExx 分集 + tvshow.nfo） | ✅ | 真实库 `国产剧` 1330 条 15.9s |
| 扫描批量化（每 200 条提交） | ✅ | 230 条跨批测试 |
| 影视库增删文件夹 / 删除库 | ✅ | 路由测试 |
| TMDb 剧集刮削 + 写 tvshow.nfo | ✅ | `蜀锦人家` 2024 / tmdb 258463 |
| TMDb 单集刮削 + 写 SxxExx.nfo | ✅ | `S01E01 季英英为生计得罪花家` |
| 剧集按季分组、分集行展示、返回按钮 | ✅ | 前端断言 + 手工预览 |
| 电影/剧集详情（左封面右资料，与 JAV 同款） | ✅ | `#unified-film-dialog` |
| 播放：本地直连 / 直封装 / HLS 转码 | ✅ | `一路超平安` → `/api/movies/items/{id}/stream` 206 |

测试基线：**Python 520 项 + Node 11 项全绿**。

---

## 四、架构变更要点（接手必看）

1. **两套存储，一个入口**
   - 成人：`data/library.db`；影视：`data/movies.db`
   - `/api/library/libraries` 合并两者；`/api/library/libraries/{id}/items` 统一条目
   - `/api/movies/*` 仍存在，作为影视条目的底层接口（详情、播放、海报、NFO）

2. **播放地址前缀不能写死**
   `media.film_playback(..., stream_base=...)`：成人传 `/api/library/films`，影视传 `/api/movies/items`。写死会导致 404 → 卡住 → 转码（就是那次"电影库变慢"）。

3. **剧集三级模型**
   `movie_items.kind = series | episode`，`episode.parent_key` 指向剧集 id；`season`/`episode` 为数字。扫描时一部剧先写一条 series，再写每条 episode。

4. **NFO 写回开关在预设里**
   `form.movie.write_nfo`（默认开，写前备份 `.nfo.bak`）、`form.movie.download_artwork`。

5. **TMDb 配置在预设「网络」段**
   `tmdb_base` / `tmdb_api_key` / `tmdb_language` / `proxy_server`。本机可用地址是 `https://api.tmdb.org/3`；Key 只存在用户本地 `data/presets.json`。

---

## 五、已知问题与风险

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | 剧集只能逐集刮削 | 40 集要点 40 次 | 加"整部剧批量刮削（含分集）" |
| 2 | 影视库数据没有"已丢失文件"标记 | 文件被移除后仍显示 | 扫描时对比磁盘 |
| 3 | 旧剧集库需重扫 | 海报/名称/剧照是旧数据 | 提示用户点一次"扫描" |
| 4 | `/api/movies/*` 与 `/api/library/*` 并存 | 两套字段易漂移 | 后续合并，保留一层兼容 |
| 5 | 扫描无事务回滚 | 极端情况可能半成品 | 分批 + 失败标记 |
| 6 | 未做剧集演员表渲染 | 详情信息偏少 | 数据已有，补渲染即可 |

---

## 六、接手后的第一步（建议顺序）

1. **跑一遍全量测试**（命令见 `memory.md` 第 2 节），确认 520 + 11 全绿
2. **真实库点测**：新建电影库/剧集库 → 扫描 → 打开详情 → 刮削一部 → 播放一集
3. **读 `memory.md`**，特别是"播放链路"和"关键决定与坑"
4. 选一项继续：
   - A. 剧集批量刮削（用户明确要的，收益最大）
   - B. 影视库合并 `/api/movies/*` → `/api/library/*`
   - C. 电影详情换海报、剧集演员表

---

## 七、发布检查清单（合并前）

- [ ] 全量测试通过（Python + Node）
- [ ] `python scripts/build_demo.py` 已重建
- [ ] `git diff main..HEAD` 里没有 `data/`、没有 Key、没有真实路径
- [ ] 更新 `VERSION` 与 `movsp/__init__.py`
- [ ] 新增 `docs/RELEASE_NOTES_vX.Y.Z.md`（脱敏）
- [ ] 用户明确指令后再 commit / push / tag / 发 release
