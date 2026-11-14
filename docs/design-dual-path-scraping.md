# 设计文档：刮削预设驱动的双路径刮削

> 状态：**阶段 1–4 全部实现**
> 目标版本：v2.5.x
> 作者：AI 助手 · 2026-11-08

## 实现状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| **1** | 路径推导、`scrape_path` 只读展示、D1 无番号跳过 | ✅ |
| **2** | 标题执行器、TMDb `_query_title`、NFO 外部 ID、封面下载 | ✅ |
| **3** | 候选面板、`needs_choice` 状态、按选定 ID 重刮 | ✅ |
| **4** | 剧集刮削（`tv_by_title` + tvshow.nfo） | ✅ |

**阶段 1 落地**
- `scrapers.resolve_path()` — `jav` / `title` / `hybrid` / `none`
- `library_store._library_scrape_path()` — API 附带 `scrape_path`
- `library_store.start_scrape()` — JAV 路径无番号时拒绝建任务
- `library.js scrapePathLabel()` — 媒体库卡片路径标签

**阶段 2 落地**
- `scrape_plan.py` — 标题/年份推导（NFO → 目录名 → 文件名清洗）、形态判定（散装文件不用父目录名）
- `native_scrape.py` — 标题路径执行器：并发调 `parse_data`、按预设顺序逐字段合并
- TMDb 插件 — 优先 `_query_external_id`，其次 `_query_title`，支持年份消歧、回写 `tmdb_id`/`imdb_id`
- `tasks._run_native_task()` — 按 `scrape_path` 分派；`title` 走主进程执行器
- `library_store.apply_native_result()` — 标题路径写 Kodi/tMM 标准 NFO（`<uniqueid type="tmdb">`）+ 下载封面
- `films.tmdb_id` / `films.imdb_id` 新列（JAV 影片留空，行为不变）

**阶段 3 落地**
- 刮削器在源码里声明 `__provides__ = [...]` 即可改变路径归属（自定义刮削器也支持）
- 多候选时任务停在 `needs_choice`（不失败），先落盘最可能的一部
- `GET /api/library/films/{id}/candidates` — 返回候选列表
- `POST /api/library/films/{id}/choose` — 用户选定后写入外部 ID 并按 ID 重刮
- 候选面板（JAV 与电影共用）：海报 / 年份 / 评分 / 简介，选中后后续不再搜索
- `_reconcile` 与重新扫描都会保留 `needs_choice`，不会冲掉待确认提示
- 新增 `status=choice` 筛选，媒体库可按「待确认」过滤

**阶段 4 落地（剧集）**
- `scrape_plan.detect_series_query()` — 剧集查询：tvshow.nfo 标题 → 目录名，`kind='series'`
- TMDb 插件按 `_query_kind` 走 `/search/tv` + `/tv/{id}`，演员取 `aggregate_credits`
- `tasks._run_native_task()` 支持剧集：目录即「一部剧」，写 `tvshow.nfo`
- `library_store._refresh_series_after_task()` — 剧集专用收尾，**不走电影刷新**
- `tvshow.nfo` 写 Kodi/tMM 标准 `<uniqueid type="tmdb">` / `<uniqueid type="imdb">`
- 剧集封面写 `poster.jpg`（Emby/Jellyfin 认这个名字）
- **`_reconcile` 不再对剧集调用 `_refresh_film`**（曾导致整部剧被降级成 movie）
- 剧集不能选 JAV 预设：给出「剧集不能按番号刮削」的清晰错误

**关键修复：标题里不能带年份**
- `scrape_plan.strip_year()` — `三体 (2023)` → `三体`，年份单独放 `year` 参数
- 原因：TMDb 按标题精确匹配，带年份的标题命中率为零
- `1917` / `2012` 这类纯数字片名不会被误剥离

**URL 容错**
- 插件 `_normalize_base()` 自动补 `https://` 与 `/3`
- 用户只填 `api.tmdb.org` 也能正常工作（这是实机失败的实际原因）

**端到端实测**（真实 TMDb Key）
- 电影：`流浪地球 (2019)` → `tmdb_id=535167`、`imdb_id=tt7605074`，完整 NFO
- 剧集：`三体 (2023)` → `tmdb_id=204541`、`imdb_id=tt20242042`，`tvshow.nfo` 含剧名/简介/首播/片商/55 位演员/外部 ID
- 多候选：`Star Wars` 停在待确认，选定后按 ID 精确重刮

**已知环境限制**：`image.tmdb.org` 在当前网络不可达，封面下载失败只记 warning、不阻断刮削；
可在刮削器设置里把「图片地址」换成可用镜像。

回归基线：**Python 624 项 + 前端 14 个文件全绿**。

---

## 零、核心原则

**用户不需要声明「这个媒体库是 JAV 还是普通电影」。**

声明已经存在于现有配置里：

```
媒体库 (library)  →  绑定 preset_id  →  预设里的 scrapers 列表  →  每个刮削器的 provides
```

**刮削路径由「预设里选了哪些刮削器」自动推导**，不需要新增任何用户可选项。

用户唯一的操作是建库时选预设 —— 这本来就要选。

### 三条不可破坏的约束

| # | 约束 | 含义 |
|---|---|---|
| C1 | **不破坏现有 JAV 刮削** | JAV 路径的代码、配置、任务链路、NFO 格式全部不变 |
| C2 | **不做两套操作逻辑** | 用户界面、预设配置、编辑流程只有一套 |
| C3 | **不新增用户声明项** | 类型由预设推导，不给用户加选择负担 |

---

## 一、路径推导

### 1.1 推导规则

```python
def resolve_path(preset_id) -> "jav" | "title" | "hybrid" | "none":
    scrapers = 预设的 scrapers 列表（已过滤停用）
    provides = {每个刮削器的能力}
    if 提供 movie_by_code 且 提供 movie_by_title/tv_by_title → "hybrid"
    if 提供 movie_by_code                                   → "jav"
    if 提供 movie_by_title / tv_by_title                    → "title"
    否则                                                     → "none"
```

| 预设内容 | 推导路径 | 行为 |
|---|---|---|
| `[javbus, javdb, avsox]` | `jav` | 现有链路，**完全不变** |
| `[tmdb]` | `title` | 新的按标题链路 |
| `[javbus, tmdb]` | `hybrid` | 先试番号，无果再试标题 |
| `[]` | `none` | 报错「该预设没有配置刮削器」 |

**这就是全部。** 没有新的库类型、没有新的用户选项。

### 1.2 判定输入

路径确定后，还要知道**这个文件能提供什么输入**：

| 输入 | 来源 | 判定方式 |
|---|---|---|
| `code`（番号） | NFO `<uniqueid type="num">` / 文件名正则 / 用户手动填 | 现有 `_guess_dvdid()` |
| `title`（标题） | 见下方优先级 | 新增 |
| `external_id`（tmdb/imdb） | NFO `<uniqueid type="tmdb">` / `<uniqueid type="imdb">` | 新增 |

#### 标题的取值优先级（D2 已确认）

**先 NFO，再目录名，最后文件名。** 但必须区分两种存放形态。

**形态 A：一目录一片**（有独立文件夹，最常见）

```
D:\电影\星球大战 (1977)\Star.Wars.1977.2160p.BluRay.x265.mkv
```

| 优先级 | 来源 | 结果 |
|---|---|---|
| 1 | NFO `<title>` | 最准 |
| 2 | NFO `<originaltitle>` | 中文常见 |
| 3 | **目录名** | ✅ `星球大战 (1977)` —— 干净、带年份 |
| 4 | 文件名（清洗后） | 兜底 |

**形态 B：散装文件**（mp4 直接堆在一个目录里，没有独立文件夹）

```
D:\电影\Star.Wars.1977.2160p.BluRay.x265.mkv
D:\电影\流浪地球.2019.1080p.mkv
D:\电影\Interstellar.2014.2160p.mkv
```

此时**目录名是共用的父目录**（`D:\电影`），**绝对不能当标题**。必须跳过第 3 优先级：

| 优先级 | 来源 | 结果 |
|---|---|---|
| 1 | NFO `<title>` | 最准 |
| 2 | NFO `<originaltitle>` | 中文常见 |
| 3 | ~~目录名~~ | ❌ **跳过**（会得到「电影」这种无意义标题） |
| 4 | **文件名（清洗后）** | ✅ `Star Wars 1977` |

#### 如何判定形态（关键实现）

**判据：该目录下是否有多个「影片条目」。**

```python
def directory_is_single_film(directory: Path) -> bool:
    """目录只承载一部影片时，目录名才能当标题。"""
    videos = _folder_videos(directory)          # 该目录直属视频（不含子目录）
    if not videos:
        return False
    # 多个视频按分片规则分组；只有 1 组才算「一目录一片」
    groups = _group_videos(videos)
    return len(groups) <= 1
```

| 场景 | 目录内直属视频 | 结果 |
|---|---|---|
| `D:\电影\星球大战 (1977)\` | 1 个 mkv | ✅ 用目录名 |
| `D:\电影\`（散装） | 50 个 mp4 | ❌ 跳过目录名，用文件名 |
| `D:\电影\某片\` 有 CD1/CD2 | 2 个分片（同组） | ✅ 用目录名（分片合并为一片） |
| `D:\电影\` 只有 1 个 mp4 | 1 个 | 🤔 **仍用目录名？** → 见下方 |

**最后一个边界情况**：根目录里只有 1 个文件时，目录名往往仍是无意义的（`D:\电影`）。
再加一条约束：

```python
def directory_is_single_film(directory: Path, root_path: str) -> bool:
    # 媒体库根目录本身永远不当标题来源
    if Path(directory).resolve() == Path(root_path).resolve():
        return False
    videos = _folder_videos(directory)
    if not videos:
        return False
    return len(_group_videos(videos)) <= 1
```

**规则总结**：

```
目录名可作为标题  ⟺  该目录不是媒体库根目录  且  目录内只有一组视频
否则              ⟹  退回文件名（清洗后）
```

**文件名清洗规则**（两种形态都要用）：

```python
# 去掉常见技术片段后再作为标题
NOISE = r'(?i)\b(2160p|1080p|720p|480p|4k|uhd|hdr10?|dv|dolby\.?vision|' \
        r'bluray|blu-ray|bdrip|web-?dl|webrip|hdtv|remux|x264|x265|h\.?264|' \
        r'h\.?265|hevc|avc|aac|ac3|dts(-hd)?|truehd|atmos|flac|' \
        r'10bit|8bit|proper|repack|extended|uncut)\b'
# 再去掉首尾的点和括号，合并多余空格
```

例：

| 原文 | 清洗后 |
|---|---|
| `Star.Wars.1977.2160p.BluRay.x265.HDR.mkv` | `Star Wars 1977` |
| `流浪地球.2019.1080p.WEB-DL.mkv` | `流浪地球 2019` |
| `Interstellar (2014) [2160p] [HDR].mkv` | `Interstellar 2014` |

**年份提取**：按 NFO `<premiered>` → 目录名 `(1977)` → 文件名 `1977` 的顺序取；同样遵守上面的形态判定（散装文件不从父目录取年份）。

#### 番号与标题的互斥

- `jav` 路径**只**用 `code`，不取 title（现状不变）；
- `title` 路径**只**用 title，不做番号识别（避免把 `1917`、`2012` 误判成番号）；
- `hybrid` 路径：先看有没有 `code`，有则走 JAV，没有才取 title。

**关键**：`jav` 路径下，有番号才刮；无番号直接跳过（D1 已确认，不建任务）。
`title` 路径下，用标题去搜；没有标题则用目录名兜底。

---

## 二、双路径执行器

### 2.1 路径 A：JAV（现状，零改动）

```
tasks.create_task
  → _preset_config_data       （现有：扁平 scrapers 展开到 selection）
  → _build_task_config        （现有）
  → 子进程 python -m javsp    （现有）
  → 引擎 parse_data(movie)    （现有）
```

**这条路径一行都不改。** 引擎要求 `dvdid`/`cid`，我们会：

- 有番号 → 照常入队（现状）
- 无番号 → **不入队**，标记 `skip_reason='no_code'`（避免产生必然失败的任务）

### 2.2 路径 B：标题（新增）

```
tasks.create_task
  → resolve_path(preset) == "title"
  → 新执行器 native_scrape.run_task(task)
      ├─ 构造查询 {title, year?, external_id?}
      ├─ 并发调用选中的刮削器（复用 parse_data 契约）
      ├─ 逐字段合并（与引擎 info_summary 相同的语义）
      └─ 写入 NFO + 更新媒体库索引
```

**为什么复用 `parse_data(movie)` 而不新写契约**：

- 现有插件（tmdb、local_json）已经实现了它；
- 用户自定义刮削器也是这个契约；
- 不引入第二套 API。

查询怎么传进去？给 `MovieInfo` 加一个轻量的**查询上下文**（不破坏现有构造）：

```python
movie = MovieInfo(query)          # 现有：番号或 CID
movie._query_title = "流浪地球"    # 新增：仅标题型刮削器读取
movie._query_year = 2019
```

> 更好的是让刮削器自己读 `movie` 上的属性，而不是依赖外部注入。
> TMDb 插件改成：优先用 `_query_title`，没有再回落到 `dvdid` —— 向后兼容。

### 2.3 混合路径（`hybrid`）

```
有番号  → 路径 A（JAV 引擎）
无番号  → 路径 B（标题执行器）
```

一条规则，不需要用户关心。

---

## 三、身份与检索：番号 vs 标题

这是你提到的核心难点。**两者必须共存，但各走各的检索键。**

| | JAV | 普通电影 | 剧集 |
|---|---|---|---|
| 唯一键 | 番号 `SSIS-001` | `tmdb_id` / `imdb_id` | `tmdb_id` + 季集 |
| 检索输入 | 番号 | 标题（+ 年份消歧） | 剧名（+ 年份） |
| 冲突场景 | 番号极少冲突 | **同名电影大量冲突**（星球大战有 1977/2015/2017…） | 同名剧集 |
| 用户介入 | 一般不需要 | **必须能手动选** | 同左 |

### 3.1 数据模型（复用现有 `films` 表，增加列）

```sql
-- 现有
dvdid TEXT          -- JAV 番号
dvdid_source TEXT   -- nfo / filename / manual

-- 新增（全部可空，不影响 JAV）
tmdb_id     TEXT
imdb_id     TEXT
title       TEXT    -- 已有
year        INTEGER -- 新增，用于消歧
external_source TEXT -- 'nfo' | 'manual' | 'scraped'
```

**JAV 影片不写这几个新列**，行为完全不变。

### 3.2 候选面板（D4 已确认：JAV 与电影共用）

#### 它是什么

按标题搜索**必然返回多条结果**，程序不能猜，必须让用户点一条。
这个「列出搜索结果、让用户选一条」的弹窗就是候选面板。

#### 触发时机

| 情况 | 是否弹面板 |
|---|---|
| 搜索只返回 1 条 | **不弹**，直接采用 |
| 搜索返回多条 | **弹**，列出候选 |
| 已有 `tmdb_id` / `imdb_id` | **不弹**，直接按 ID 取（不再搜索） |
| 搜索 0 条 | 不弹，任务失败可重试 |

#### 界面

```
┌─────────────────────────────────────────────────┐
│  选择影片 · Star Wars                            │
│  搜索： [Star Wars      ] 年份：[1977] [搜索]    │
├─────────────────────────────────────────────────┤
│  ○ 星球大战           (1977)  TMDb 11    ★ 8.2  │
│  ○ 星球大战：原力觉醒  (2015)  TMDb 140607 ★ 7.5 │
│  ○ 星球大战：最后的绝地 (2017)  TMDb 181808 ★ 7.0 │
│  ○ Star Wars Holiday Special (1978) TMDb 748 ★ 3│
├─────────────────────────────────────────────────┤
│                    [取消]   [使用选中项]         │
└─────────────────────────────────────────────────┘
```

- **默认按年份接近度排序**（取文件名/目录名里的年份）；
- 用户可改搜索词、填年份再搜；
- 选中后把 `external_id` 写入影片记录。

#### 选中后发生什么

```python
# 第一次：搜索 → 用户选择 → 记下 ID
films.tmdb_id = '11'
films.external_source = 'manual'

# 以后每次刮削：直接用 ID，不再搜索
GET /movie/11        # 无歧义
```

**这是关键收益**：歧义只发生一次。

#### 与「编辑资料」的区别

| | 编辑资料 | 候选面板 |
|---|---|---|
| 用户做什么 | 手填字段 | 从搜索结果里挑一条 |
| 字段来源 | 用户输入 | 程序自动填 |
| 适用 | 知道番号但没识别出来 | 按标题搜到多条，需要选 |

#### JAV 为什么也要共用

- 番号歧义虽少见但存在（站点改番号、FC2 编号重复）；
- 复用一套组件，避免「JAV 一套、电影一套」的分裂；
- JAV 场景下大多数时候只有 1 条结果，面板自动跳过，用户感知不到。

### 3.3 NFO 读写（保持一套实现）

现有 `_parse_nfo()` 只认 `<uniqueid type="num">`。扩展：

```python
# 读取时依次尝试
for uid in root.findall('uniqueid'):
    kind = (uid.get('type') or '').lower()
    if kind in ('num', 'dvdid'):  dvdid = uid.text
    elif kind == 'tmdb':          tmdb_id = uid.text
    elif kind == 'imdb':          imdb_id = uid.text
```

写入时按影片有哪种 id 决定写哪个 `<uniqueid>`：

- JAV → `<uniqueid type="num">SSIS-001</uniqueid>`（现状）
- 电影 → `<uniqueid type="tmdb">11</uniqueid>`（Kodi/tMM 标准）

**同一个函数，两条分支，不是两套实现。**

---

## 四、界面一致性（不做两套）

### 4.1 影片详情/编辑对话框（复用）

现有「编辑资料」对话框字段：

```
番号 [____]  标题 [____]  女优 [____]  导演 [____] ...
```

**改造**：字段名根据 `resolve_path()` 动态显示：

| 路径 | 第一行字段 |
|---|---|
| `jav` | `番号` [输入框] |
| `title` | `标题` [输入] + `年份` [输入] + `[搜索…]` 按钮 |

其余字段（标题、剧情、封面、演员、导演、发行日期…）**完全相同**。

### 4.2 刮削按钮（复用）

媒体库列表上的「刮削」「批量刮削」按钮**不变**，内部按预设路径分派。

### 4.3 预设配置（复用）

预设里的「刮削器」页签**不变** —— 仍是扁平列表。用户加 JAV 刮削器 → 走 JAV；加 TMDb → 走标题；都加 → hybrid。

**用户不需要知道自己在配置「哪条路径」。**

---

## 五、冲突与失败处理

| 场景 | JAV 行为（不变） | 电影行为（新增） |
|---|---|---|
| 无输入 | 番号识别失败 → 跳过 | 用目录名/文件名兜底 |
| 搜到多条 | 番号精确匹配，一般唯一 | **标记 `needs_choice`**，等用户选 |
| 完全搜不到 | 任务失败，可重试 | 任务失败，可重试 |
| 用户手动指定 | 编辑番号 | 编辑 tmdb_id 或从候选面板选 |

**`needs_choice` 状态**：任务不失败，而是停在「待确认」，媒体库列表显示「⚠ 需要选择」。这是新增状态，只对电影路径出现。

---

## 六、实施拆解

### 阶段 1：路径推导（纯读取，零行为变更）

| 步骤 | 文件 | 风险 |
|---|---|---|
| 1. `scrapers.resolve_path(preset_id)` | `scrapers.py` | 无 |
| 2. 媒体库 API 返回 `resolved_path` | `server.py` | 无 |
| 3. 界面在媒体库上显示路径标签（只读） | `app.js` / `library.js` | 无 |
| 4. `jav` 路径下无番号 → 跳过不建任务（D1） | `library_store.py` | 低 |

**产出**：能看到每个媒体库会走哪条路径，但刮削行为不变（除了少建无意义的失败任务）。

### 阶段 2：标题执行器

| 步骤 | 文件 |
|---|---|
| 1. `native_scrape.py` 新执行器 | 新增 |
| 2. `scrape_plan.detect_query()` 标题/年份推导（含形态判定） | 新增 |
| 3. TMDb 插件支持 `_query_title` | `scrapers.py` 的 `_TMDB_BODY` |
| 4. `tasks.py` 按 resolve_path 分派 | `tasks.py` |
| 5. NFO 读写扩展 tmdb/imdb | `library_store.py` |

**阶段 2 必须覆盖两种存放形态**（见 §1.2）：一目录一片 / 散装文件。

### 阶段 3：候选选择与手动确认

| 步骤 | 文件 |
|---|---|
| 1. `needs_choice` 状态 | `library_store.py` |
| 2. 候选面板（JAV/电影共用） | `app.js` / `library.js` |
| 3. 候选搜索 API | `server.py` |

### 阶段 4：剧集

复用阶段 2/3，增加季集维度。

---

## 七、必须守住的兼容点

| # | 兼容点 | 验证方式 |
|---|---|---|
| 1 | 现有 JAV 预设行为 100% 不变 | 现有 572 项 Python 测试全绿 |
| 2 | JAV 媒体库扫描/刮削/编辑流程不变 | 手工点测 |
| 3 | JAV NFO 格式不变 | 对比刮削前后 NFO diff |
| 4 | 旧预设（按分类 selection）仍可运行 | 兼容层测试 |
| 5 | 没有刮削器的预设报清晰错误 | 新增测试 |
| 6 | **散装文件（无独立文件夹）能正确取标题** | 新增测试：父目录名不得当标题 |
| 7 | **一目录一片仍优先用目录名** | 新增测试 |
| 8 | **分片（CD1/CD2）仍合并为一片并可用目录名** | 新增测试 |

---

## 八、已确认的决策

| # | 问题 | 结论 |
|---|---|---|
| **D1** | 无番号的 JAV 影片 | **跳过，不建任务**。典型流程是用户在「编辑资料」里手填番号后再点刮削，此时番号已存在。 |
| **D2** | 标题从哪取 | **NFO `<title>` → NFO `<originaltitle>` → 目录名 → 文件名（清洗后）**。年份同理。 |
| **D3** | TMDb 如何接收标题查询 | **给 `MovieInfo` 加 `_query_title` 属性**，仅函数内使用，**不写入 NFO**。NFO 仍用 Kodi/tMM 标准标签（`<uniqueid type="tmdb">`）。 |
| **D4** | 候选面板是否共用于 JAV | **共用**。多结果时弹出让用户选，单结果自动跳过。选中后记下 ID，后续不再搜索。 |

### D3 补充说明

`_query_title` / `_query_year` 是**内存中的临时查询上下文**，生命周期只到本次 `parse_data()` 调用结束：

```python
movie = MovieInfo('')             # 无番号
movie._query_title = '流浪地球'    # 临时属性
movie._query_year = 2019
parse_data(movie)                 # TMDb 读取它去搜索
# 之后写 NFO 时，只写标准字段，不写 _query_title
```

**绝不写入 NFO**，NFO 里只出现：
- JAV → `<uniqueid type="num">SSIS-001</uniqueid>`
- 电影 → `<uniqueid type="tmdb">535167</uniqueid>` + `<uniqueid type="imdb">tt7605074</uniqueid>`

### D1 补充说明

「跳过」的判定条件是：`resolve_path == 'jav'` 且影片没有番号。

跳过时：

- **不创建刮削任务**（避免必然失败的任务和日志噪音）；
- 媒体库列表把该影片标记为 `未刮削 · 缺番号`；
- 用户点「编辑资料」手填番号后，再点「刮削」即可正常走 JAV 路径。

这正是你说的使用场景，无需额外机制。

---

## 九、明确不做的事

- ❌ 不给媒体库加「类型」选择（由预设推导）
- ❌ 不改 JAV 引擎（vendor/JavSP 零改动）
- ❌ 不做第二套预设表单
- ❌ 不做第二套影片编辑对话框
- ❌ 不改变 JAV NFO 格式
