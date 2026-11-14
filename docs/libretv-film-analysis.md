# 影视库可以借鉴 LibreTV 什么

> 分析对象：本仓库 `feat/movie-library` 分支（v2.2.2 + 影视库改造）  
> 对照项目：[LibreSpark/LibreTV](https://github.com/LibreSpark/LibreTV) `main`（2026-10-23，AGPL-3.0）  
> 本文只做评估与落地方案，**没有改动代码**。

## 结论

1. **能借鉴，但只在「播放体验层」**：观看进度/历史/连播、片头片尾跳过、播放失败恢复、画质标识。这四项正好对应本项目影视库目前最明显的缺口。
2. **不建议借鉴「内容聚合层」**：Apple CMS 采集源、跨源搜索、直播 IPTV、源订阅。它们解决的是「没有片源」，本项目解决的是「本地片源的管理与刮削」，定位不同，还会引入版权与合规风险。
3. **不能直接拷代码**：LibreTV 是 AGPL-3.0，本项目是 GPL-3.0。把 AGPL 代码并进来，整体分发要按 AGPL-3.0 处理（GPLv3 §13），对本项目的 EXE / Docker 分发是实质性义务。可行方式是照行为重写。
4. **最值得先做**：P0-1「观看进度 + 继续观看 + 自动连播」。它同时解决「看剧看到一半找不回来」和「每集手动点」两个高频问题，而且本项目有账号、多设备，应该放服务端 SQLite，而不是照抄 LibreTV 的 IndexedDB。

## 1. 两边是什么 / 不是什么

| | LibreTV | JavSP-Web 影视库 |
|---|---|---|
| 片源 | 第三方 Apple CMS 采集站，在线 m3u8 | 本地磁盘文件 + 可选 Emby 转码 |
| 核心 | 聚合搜索、跨源换源、直播 | 扫描、TMDb 刮削、NFO 读写 |
| 数据 | 浏览器 IndexedDB / localStorage | 服务端 SQLite（`movies.db`） |
| 用户 | 单密码，无账号 | 多账号（admin / operator） |
| 播放 | ArtPlayer + hls.js | 同一套 ArtPlayer + hls.js |
| 许可 | AGPL-3.0 | GPL-3.0 |

共同点只有一个：**播放器**。所以借鉴范围也应止于播放器及其周边。

## 2. 现状缺口（带证据）

| 能力 | 现状 | 证据 |
|---|---|---|
| 观看进度 | 只按单个条目存秒数到 `localStorage`，没有时间戳、没有历史、没有跨集 | `movsp/web/assets/library.js:1066-1121` |
| 续播/连播 | 播完只清进度，不会自动下一集 | `library.js:1263-1265` |
| 观看历史 | 没有「继续观看」入口，也没有历史列表 | 全仓库搜不到 `继续观看` / `观看历史` |
| 片头片尾跳过 | 没有 | 全仓库搜不到 `片头` / `片尾` |
| 播放失败 | 一次 stall 兜底 + 一次转码重试，无退避、无 HLS 错误恢复 | `library.js:1080-1095`、`1301-1320` |
| 画质/编码 | `/playback` 已经返回 `codec` / `height`，前端只显示「直连/转码」 | `movsp/media.py:396-398`、`library.js:1426-1436` |
| 批量刮削 | 在 HTTP 请求里同步串行，40 集要等 40 次 TMDb | `movsp/server.py:3214-3229` |
| 豆瓣评分 | 表里有 `douban_rating`，只在读 NFO 时填入，没有抓取与展示 | `movsp/movies.py:99`、`336` |

## 3. 能力矩阵

| LibreTV 能力 | 关键实现 | 对本项目价值 | 成本 | 结论 |
|---|---|---|---|---|
| 观看历史 + 续播 + 连播 | `src/lib/db.ts`、`src/app/watch/page.tsx`、`history-panel.tsx` | 高 | 中 | **P0-1** |
| 片头/中段/片尾跳过 | `src/lib/skip-marks.ts`、`player-shell.tsx` | 高 | 中 | **P0-2** |
| HLS 播放错误恢复 | `src/lib/playback-recovery.ts` | 中高 | 低 | **P0-3** |
| 画质/编码徽章 | `src/lib/quality.ts`、`player-shell.tsx` | 中 | 低 | **P1-1** |
| 并行 + 单源超时 + 部分成功 | `src/app/api/search/route.ts` | 高（工程） | 中 | **P1-2** |
| 豆瓣 / Bangumi 推荐 | `src/lib/douban.ts`、`douban-weekly.ts` | 中低 | 中 | P2 |
| 同片多版本聚合 / 换源 | 跨源同名聚合 | 中 | 高 | P2 |
| Apple CMS 在线片源 | `src/lib/cms-parser.ts` | 产品级 | 高 | 不建议 |
| 直播 IPTV | `src/lib/m3u-parser.ts`、`src/app/live` | 无关 | 高 | 不建议 |
| 源订阅 / TVBOX | `source-list.ts`、`tvbox-parser.ts` | 无关 | 高 | 不建议 |
| 广告过滤 / 下载器 / 分片预取 | `player-shell.tsx`、`m3u8-downloader.ts`、`video-prefetcher.ts` | 无关 | 中 | 不建议 |

## 4. P0-1 观看进度 / 观看历史 / 续播 / 连播

### LibreTV 的做法

`src/lib/db.ts` 有两张表：

- `history`：主键 `sourceKey_vodId`，记录 `episodeIndex`、`totalEpisodes`、`playbackPosition`、`duration`、`timestamp`，上限 100 条，按时间淘汰。
- `progress`：主键 `sourceKey_vodId_episodeIndex`，精确到每集。

几个值得抄的细节：

- `upsertHistory` 在新进度小于 10 秒时保留旧进度，避免刚起播就把「看到 90%」冲掉。
- `updateHistoryProgress` 在位置/时长变化小于 2 秒时不写库。
- 播完（`ended`）清掉该集进度，若 `autoplayNext` 为真则 800ms 后进下一集。
- 恢复位置只在 `>10s` 且 `< 时长-2s` 时应用。
- 历史面板支持搜索、单条删除（可撤销）、清空全部。

### 本项目的落点

**不要照抄 IndexedDB**。本项目是服务端应用、有账号、要跨设备，进度应进 `movies.db`：

```sql
CREATE TABLE movie_progress (
  user      TEXT NOT NULL,
  item_id   TEXT NOT NULL,
  position  INTEGER NOT NULL DEFAULT 0,
  duration  INTEGER NOT NULL DEFAULT 0,
  finished  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user, item_id)
);

CREATE TABLE movie_history (
  user       TEXT NOT NULL,
  series_id  TEXT NOT NULL,   -- 电影 = 自身 id；剧集 = parent_key
  item_id    TEXT NOT NULL,   -- 最后播放的那一集/那部
  season     INTEGER NOT NULL DEFAULT 0,
  episode    INTEGER NOT NULL DEFAULT 0,
  position   INTEGER NOT NULL DEFAULT 0,
  duration   INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user, series_id)
);
```

API：

- `PUT /api/movies/items/{id}/progress`，body `{position, duration, finished}`。
- `GET /api/movies/history?limit=20`，join `movie_items` 返回海报、季集、进度。
- `DELETE /api/movies/history/{series_id}`。
- `GET /api/movies/items/{id}` 增加 `progress`、`next_id`、`prev_id`（跨季由服务端算，前端不用自己拼）。

规则：

- 位置 `<1` 或时长 `<=0` 不写；前后端都做 5 秒节流，服务端再按「变化 <2 秒」去抖。
- 播完 `finished=1` 并清 playback，历史保留并指向下一集。
- 恢复只在 `>10s` 且 `< 时长-10s` 时应用，和现在 `library.js` 的阈值保持一致。
- 历史上限 100 条，LRU 淘汰；文件丢失的条目仍显示，但播放按钮禁用并提示重扫。

UI：

- 媒体库网格顶部加一行「继续观看」横向卡片（最多 8 个），显示剧名 + `S01E02` + 进度条 + 「还剩 23 分钟」。
- 分集行显示细进度条和「已看 37%」。
- 播放结束浮层：「5 秒后播放 S01E02」+「立即播放 / 取消」；自动连播开关放播放器设置里，默认开。
- 观看历史抽屉（搜索、单条删除、清空）可以放到第二阶段。

前端结构：把纯逻辑放 `assets/player-core.js`（`resumePosition`、`nextEpisode`、`progressPercent`），在 `library.js` 之前加载，`window.JavspPlayerCore` 暴露；Node 测试沿用 `tests/test_library_playback.cjs` 的 `vm` 方式，不依赖 DOM。

工作量：后端约 1 天，前端约 1 天，测试约 0.5 天。

## 5. P0-2 片头 / 中段 / 片尾跳过

### LibreTV 的做法

`src/lib/skip-marks.ts` 把「片头/片尾」泛化成任意数量的 `SkipSegment`：

- 类型 `opening`（起点恒为 0）、`middle`、`ending`（`end=-1` 表示到结尾，运行时用实际时长填充）。
- 校验：片段至少 3 秒、相邻至少间隔 5 秒、终点至少比总时长提前 2 秒。
- 读取时按当前集的实际时长再裁剪一次，越界/损坏的标记直接丢弃；不同类型版本共用标记也不会让播放报错。
- 按「归一化剧名」存，同一部剧各集、换源后同名资源都能复用；标记带总开关，关掉后保留数据但不生效。
- 播放器里：进度条着色、进入片段弹「跳过片头 [跳过]」、自动跳过、12 秒内可撤销；片尾用「小步连续越过」判定，拖动进度条不会误触发结束。

### 本项目的落点

本项目有稳定的 `series_id`，不需要按标题归一化，直接按剧集 id 存：

```sql
CREATE TABLE movie_skip_marks (
  series_id  TEXT PRIMARY KEY,
  segments   TEXT NOT NULL DEFAULT '[]',
  enabled    INTEGER NOT NULL DEFAULT 1,
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
```

- `segments` 形如 `[{"type":"opening","start":0,"end":90},{"type":"ending","start":2640,"end":-1}]`。
- API：`GET/PUT /api/movies/items/{series_id}/skip-marks`，PUT 时做同一套校验；电影也允许标，但默认只对剧集展示入口。
- 播放时按当前 `video.duration` 解析（`resolveSegments`），不要直接信任库里的绝对值。
- 播放器 UI：进度条着色、右上角「跳过设置」面板（当前时间 + 片头结束/片尾开始/清除）、可选快捷键 `Shift+I` / `Shift+O`。
- 自动跳过默认开；跳过时给 12 秒「撤销」，撤销后本集不再自动跳这个片段（否则会「撤销→又被跳走」死循环）。
- 片尾命中后走和自然播完同一条链路：清进度 → 自动连播。

工作量：后端约 0.5 天，前端约 1.5 天，测试 0.5 天。

## 6. P0-3 播放失败恢复

### LibreTV 的做法

`src/lib/playback-recovery.ts` 是一个纯决策状态机：

- 网络错误：最多重试 3 次，退避 `800ms × 2^(n-1)`，上限 8 秒。
- 媒体错误：最多恢复 2 次，第二次追加 `swapAudioCodec()`。
- `manifestParsingError` / `manifestIncompatibleCodecsError` / `levelEmptyError` 视为硬失败，不重试，直接换源。
- 收到 `FRAG_LOADED` / `MANIFEST_PARSED` 视为链路恢复，但动作后 3 秒静默窗内的成功不清零，避免抖动误判。
- 超过重试次数才换源，不会形成无限重试风暴。

### 本项目的落点

本项目的「源」只有本地直连和 Emby 转码，没有第三个源，但 HLS 转码链路完全适用：

- 把状态机移植到 `player-core.js`（纯函数/类，可单测），`ensureArtPlayer` 的 `customType.m3u8` 里监听 `Hls.Events.ERROR`。
- `NETWORK_ERROR` → 退避后 `hls.startLoad()`；`MEDIA_ERROR` → `recoverMediaError()`，第二次 `swapAudioCodec()`；硬失败或重试耗尽 → 走现有 `retryWithEmby()` / 提示。
- 本地直连（mp4 文件）保持现在的一次性兜底：编码不支持时重试没有意义，直接换转码。
- 错误文案用中文，明确「播放列表加载失败 / 分片加载失败 / 解码失败」，不要只弹 code。

工作量：约 0.5–1 天，含 Node 侧决策单测。

## 7. P1-1 画质 / 编码 / 播放方式标识

`/playback` 已经返回 `codec`、`height`、`play_mode`、`transcoding`，只是前端没用上。借鉴 `quality.ts` 的档位表（2160→4K、1440→2K、1080P、720P，允许 15% 的裁切误差）做一个 `qualityLabel(height)`：

- 播放器状态行从「直连播放」变成「4K · HEVC · 直连（HLS）」。
- 转码时用琥珀色标出「转码」，让用户一眼看到 CPU 成本；这符合本项目「优先直连/直封装」的性能取向。
- 本地直连没有 `height`，在 `loadedmetadata` 后读 `video.videoHeight` 补上，并写回 `movie_items` 的 `video_height` 字段（可选）。
- 画质徽章四态可以简化：已知 / 实测 / 未知 / 失败，不需要 LibreTV 的源探测队列。

工作量：约 0.5 天。

## 8. P1-2 批量刮削：并行 + 单源超时 + 部分成功

这是 LibreTV 的**工程模式**，不是影视功能本身，但正好能解决本项目的头号待办「整部剧批量刮削」。

LibreTV 的 `search/route.ts`：

- 每个源并行请求，单源总死线 10 秒，到点中断在途请求。
- 第一页读 `pagecount`，后续页并行抓，单页失败只丢该页。
- 每个源返回 `{ok,error,ms}`，一个源失败不影响整体；结果有 60 秒短缓存。

本项目的现状是 `server.py:3214-3229` 在请求线程里 `for` 循环串行调用 `movie_store.scrape_item`，40 集会阻塞几分钟，而且失败只回传前 5 条错误。建议：

- 新增后台刮削任务表/注册表，`POST /api/movies/items/scrape` 返回 202 + `job_id`，`GET /api/movies/scrapes/{id}` 查进度。
- 并发 3（保守，TMDb 限流），单请求超时读预设 `form.network.timeout`，失败重试读 `form.network.retry`。
- 每个条目独立结果 `{id, ok, error}`，失败继续；「整部剧」模式先刮剧集、再刮所有分集，已完成的可跳过。
- 前端显示进度条和失败列表，支持「只重试失败」。

工作量：约 1.5–2 天。

## 9. P2 / 可选

- **豆瓣评分**：`douban_rating` 字段已经有了，但只在 NFO 里有值时才填。LibreTV 的豆瓣接口带 10 分钟缓存和 CORS 代理降级，思路可借；但豆瓣反爬和接口稳定性一般，建议先只展示 NFO 里的值，或做成预设里可选、失败静默的补充，不要作为主数据源。
- **TMDb 镜像健康度**：LibreTV 的「连续失败 → 30 分钟 / 24 小时阶梯停用 → 一键恢复」可以套到 TMDb base 列表上。本项目现在只能手填一个 base，遇到域名被墙要用户自己找镜像；多 base 顺序尝试 + 冷却，能直接改善这个痛点。
- **同片多版本聚合**：LibreTV 把「跨源同名」合并成一张卡、展开换源；本项目同一部电影放在两个目录会变成两张卡。可以按 `tmdb_id` 聚合成一张卡 + 版本选择器（4K/1080P/路径），但改动面比前几项大，放后面。
- **预告片**：TMDb `videos` 已经能拿到 YouTube key，用 ArtPlayer 播即可。这不是 LibreTV 的能力，但属于影视详情页的自然缺口。

## 10. 不建议照搬

- **Apple CMS 在线片源 / 跨源搜索 / 换源**：产品定位从「本地媒体库」变成「在线聚合站」；采集站内容版权不明，与本项目 README 的免责声明和「仅供个人学习」定位冲突；还会引入 SSRF、防盗链、代理等一整套服务端安全面。若将来真要做，也应做成用户自填、默认关闭、不内置任何源、单独开关的模块，并先过 SSRF 校验（可参考 `src/lib/ssrf.ts`）。
- **直播 IPTV / M3U / EPG**：与影视库无关，是另一个产品面。
- **数据源订阅 / TVBOX 解析**：没有远端源列表可订阅。
- **广告分片过滤 / m3u8 下载器 / 分片预取**：本地直连不需要；Emby HLS 是内网转码，预取收益也有限。
- **IndexedDB 进度**：LibreTV 没有服务端账号，只能放浏览器；本项目有账号和多设备，放服务端 SQLite 更合适。
- **PWA / 主题**：本项目已有主题体系，不重复做。

## 11. 许可与合规

- LibreTV：AGPL-3.0；本项目：GPL-3.0。
- GPLv3 §13 允许与 AGPLv3 作品组合，但**组合后的整体必须按 AGPL-3.0 分发**，并且通过网络提供服务时要向用户提供对应源码。本项目发布 EXE、Docker 镜像和 Web 服务，这个义务是实际存在的。
- 因此：**只借鉴行为、数据结构和交互，自己写实现**；不要复制函数名、注释、类型定义和文件结构。若确实要复用某段代码，需要项目所有者决定是否整体转 AGPL，并保留署名。
- 另外，LibreTV 的采集源来自第三方，本项目不应内置、分发或推荐任何具体源。

## 12. 建议排期

| 迭代 | 内容 | 预估 |
|---|---|---|
| 1 | P0-1 进度/历史/连播 + P1-1 画质标识 | 2–3 天 |
| 2 | P0-2 片头片尾 + P0-3 播放恢复 | 2 天 |
| 3 | P1-2 异步批量刮削（整部剧） | 2 天 |
| 后续 | P2 豆瓣 / TMDb 镜像 / 多版本聚合 | 按需 |

先做迭代 1 的理由：它不依赖新外部服务，全部在现有 `movies.db` + ArtPlayer 链路内；对「剧集库 1330 条、每集手动点」的体感提升最大；而且能为迭代 2 的「片尾 → 连播」打好同一套进度/下一集接口。

## 13. 测试建议

- Python：`tests/test_movie_progress.py`（进度 CRUD、去抖、历史排序/上限、下一集跨季、文件丢失）、`tests/test_movie_skip_marks.py`（校验、按集裁剪、总开关）、`tests/test_movie_scrape_jobs.py`（并发、单条失败不中断、进度）。
- Node：`tests/test_movie_player.cjs`，沿用 `vm` 方式测 `player-core.js` 的纯函数（续播阈值、下一集、片段解析、恢复决策）。
- 手工验收：两季剧 → 看到 30 分钟关闭 → 重开续播 → 播完自动下一集 → 标 90 秒片头并自动跳过 → 播放中杀掉 Emby 转码，观察退避重试与转码兜底。

## 附：LibreTV 关键文件索引

| 文件 | 用途 |
|---|---|
| `src/lib/db.ts` | IndexedDB：历史、进度、搜索历史、LRU |
| `src/app/watch/page.tsx` | 续播、进度写入、自动连播、历史 upsert |
| `src/components/history-panel.tsx` | 观看历史 UI |
| `src/lib/skip-marks.ts` | 跳过片段的模型、校验、按集裁剪 |
| `src/components/player-shell.tsx` | 进度条着色、自动跳过、撤销、HLS 错误接入 |
| `src/lib/playback-recovery.ts` | HLS 错误恢复状态机 |
| `src/lib/quality.ts` | 画质档位、徽章、汇总 |
| `src/app/api/search/route.ts` | 并行搜索、单源超时、部分成功 |
| `src/lib/cms-parser.ts` | Apple CMS 解析（不建议引入） |
| `src/lib/ssrf.ts` | 服务端请求的内网/保留地址校验 |
