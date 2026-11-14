# 设计文档：刮削器能力模型与统一刮削链路

> 状态：**一期已实现（v2 修订版）**
> 目标版本：v2.4.x
> 作者：AI 助手 · 2026-11-07
> 前置阅读：[scraper-building-blocks.md](./scraper-building-blocks.md)（已实现的「搭积木」注册表）

## 实现状态（2026-11-07，v2 修订）

> **v2 修订要点**：按用户实机反馈重做了前端职责划分。
> 管理页只做「显示 + 连接状态检测」；用哪些刮削器**只在刮削预设里选**；
> 能力（能刮番号还是演员）变成**后端内部概念**，用户不可见、不可改。

| 项 | 状态 | 位置 |
|---|---|---|
| `provides` / `fields` / `sample` 取代 `groups` | ✅ | `movsp/scrapers.py` |
| 能力改为**后端内部**（`set_overrides` 拒绝 `provides`） | ✅ | `scrapers.set_overrides()` |
| 11 种细粒度状态 + `http_status` / `elapsed_ms` | ✅ | `scrapers.STATUS_STATES` |
| 严格可用性判定 | ✅ | `scrapers.evaluate_result()` |
| **预设扁平刮削器列表**（`crawler.scrapers`，默认空） | ✅ | `server._normalize_form()` / `_public_preset()` |
| **扁平列表展开到引擎分类结构** | ✅ | `tasks._expand_selection()` |
| 旧预设（按分类 `selection`）自动压平 | ✅ | `server._flatten_selection()` |
| 管理页：单一扁平网格，只有显示 + 状态检测 | ✅ | `app.js` `scraperGridMarkup()` |
| 预设页：扁平有序列表，默认为空，可增删/排序 | ✅ | `app.js` `crawlerConfigTagsMarkup()` |
| 「影片分类」移入高级折叠 | ✅ | `ADVANCED_TABS` |
| 原生执行器（普通电影刮削） | ⬜ 二期 | — |
| 剧集刮削 | ⬜ 三期 | — |

回归基线：**Python 572 项 + 前端 14 个文件全绿**。
真实端到端检测：JavBus 用 `SSIS-001` 返回 `ok`。

---

## 零、v2 修订：职责边界

这是本设计最重要的一条，也是 v1 做得不对的地方。

| 界面 | 职责 | 不该做的事 |
|---|---|---|
| **刮削器管理** | 列出所有刮削器；开关启用；填配置（API Key 等）；编辑源码；**检测连接状态** | 不应该让用户按能力分区；不应该让用户勾「它能刮番号还是演员」 |
| **刮削预设 → 刮削器** | **一份扁平的刮削器有序列表**，默认空，由用户按需添加 | 不应该按影片分类分组；不应该预设一堆默认刮削器 |

### 用户心智（这才是目标）

```
「这个目录全是 JAV」  → 建预设 A，把 JAV 刮削器全加进去
「这个目录是普通电影」 → 建预设 B，只加 TMDb
```

用户**永远不需要知道**某个刮削器内部是「刮番号」还是「刮演员」。
选中几个，它们就**并发执行**，结果按顺序逐字段合并（上游 `info_summary` 的既有行为）。

### 能力为什么还在后端

能力没有消失，只是**降级为后端内部概念**：

- 只用于「可用性检测」挑合适的样本（JavBus 用 `SSIS-001`，AVSOX 用 `030919_047`，TMDb 用「流浪地球」）；
- 不再出现在界面，不参与用户选择。

---

## 一、要解决的问题

### 1.1 用户视角

现在用户要刮削时，必须先理解两件**他并不关心**的事：

1. **这是 JAV 还是普通电影？** —— 决定用哪个预设、哪条链路；
2. **这个文件属于哪个「影片分类」？** —— 决定用哪组爬虫。

而用户的真实心智是：「我有一个视频文件，我选一个刮削器，它就给我抓资料。」

### 1.2 技术视角

当前调度键是**媒体分类**，这是上游 JavSP 引擎的历史设计：

```
文件 → scanner.media_types 正则识别 → movie.data_src（如 "normal"/"fc2"/"cid"）
     → crawler.selection[data_src] → 该分类下的爬虫列表
```

只在两处消费，但形成硬约束：

| 位置 | 作用 |
|---|---|
| [`avid.py:190`](../../vendor/JavSP/javsp/avid.py#L190) `guess_av_type()` | 用正则/CID 规则把文件名判成分类 id |
| [`__main__.py:168`](../../vendor/JavSP/javsp/__main__.py#L168) | `crawler.selection.get(movie.data_src, fallback)` |

后果：

- 归入「番号分类」的影片**只能**用该分类下的爬虫；
- TMDb 这类按标题查询、不需要番号分类的刮削器，**无处安放**；
- JAV 与普通电影被迫走两套预设。

### 1.3 参考实现（JavBoss）

`Solr159/JavBoss` 用**能力矩阵**完全替代了分类分组：

```go
// 每个 provider 只实现它支持的方法；能力由接口断言自动推导
type MovieLookup interface { LookupJavByCode(ctx, code) (*JavInfo, error) }
type ActressNameLookup interface { LookupActressByName(ctx, name) (*ActressInfo, error) }

type Capabilities struct {
    Movie, ActressByCode, ActressByName bool
    ActressURL, SeriesURL, StudioURL    bool
}
func CapabilitiesFor(provider Provider) Capabilities { /* 类型断言 */ }
```

它**没有 media_types、没有分类分组**，靠「provider 能做什么 + 用户输入什么」直接配对。

这正是我们要的方向：**用「能力」取代「分类」作为调度键。**

---

## 二、设计目标与非目标

### 目标

| # | 目标 | 验收标准 |
|---|---|---|
| G1 | 刮削器用**能力矩阵**声明自己能做什么 | 新增刮削器无需填写任何分类 |
| G2 | 调度按**输入形态**自动分派，用户不用选 JAV/电影 | 同一个预设可同时刮 JAV 与普通电影 |
| G3 | 管理页状态**细粒度**，能区分 DNS/403/解析失败 | 状态 ≥ 8 种，前端逐条翻译 |
| G4 | 「影片分类」从用户必答题降级为高级选项 | 默认折叠；不改也能正常工作 |
| G5 | 引擎侧**零改动**（vendor/JavSP 不动） | diff 中无 vendor 变更 |

### 非目标（本期不做）

- ❌ 剧集（季/集）刮削 —— 需要独立链路，放到第三期；
- ❌ 改写 JavSP 引擎的 `media_types` 机制 —— 用兼容层绕过；
- ❌ 删除「影片分类」配置项 —— 上游引擎仍需要它兜底；
- ❌ 刮削器市场 / 在线分发。

---

## 三、核心概念

### 3.1 能力（Capability）—— 刮削器「能做什么」

沿用 JavBoss 的思路，但用字符串常量（Python 侧无接口断言）：

| 能力 id | 语义 | 典型实现 |
|---|---|---|
| `movie_by_code` | 按番号取影片资料 | JavBus、JavDB、FANZA、FC2 |
| `movie_by_title` | 按标题搜影片 | TMDb（电影） |
| `tv_by_title` | 按标题搜剧集 | TMDb（TV） |
| `actress_by_code` | 按番号取演员信息 | JavBus、JavDB |
| `actress_by_name` | 按演员名取资料 | AVWiki、MinnanoAV |
| `series_by_code` | 按番号取系列 | JavDB |
| `genre_catalog` | 能拉取分类目录 | JavBus（genre 页） |

> 能力是**刮削器自身属性**，新增能力只需在常量表加一行。

### 3.2 输入形态（Input Kind）—— 文件「能提供什么」

| 输入形态 | 来源 | 说明 |
|---|---|---|
| `code` | 文件名正则识别 / 手动填写 | 番号，如 `SSIS-001`、`FC2-123456` |
| `title` | 文件名 / 目录名 / NFO | 标题，普通电影靠它 |
| `actress_name` | 用户输入 | 演员名，用于补全演员资料 |
| `season` | 剧集目录结构 | 季集号（第三期） |

### 3.3 匹配规则（唯一的调度逻辑）

```
候选刮削器 = { s ∈ 已启用刮削器 | capabilities(s) ∩ required(inputs) ≠ ∅ }
```

其中 `required(inputs)` 由「文件能提供什么」推导：

| 文件提供 | 需要的能力 |
|---|---|
| `code` | `movie_by_code` |
| `title` | `movie_by_title` / `tv_by_title` |
| `code + actress_name` | `actress_by_code` 或 `actress_by_name` |

**至此，「分类」完全退出调度。**

---

## 四、数据模型变更

### 4.1 刮削器定义（`scrapers.py` 的 `_def()`）

**Before**

```python
_def("javbus", "JavBus", "built_in", "site", ["normal"],
     "JavBus 影片资料…", "https://www.seedmm.help",
     ["title", "cover", "genre", "actress", "preview_pics"])
#                                  ↑ groups（分类）      ↑ capabilities（字段，与能力混淆）
```

**After**

```python
_def("javbus", "JavBus", "built_in", "site",
     provides=["movie_by_code", "actress_by_code"],
     fields=["title", "cover", "genre", "actress", "preview_pics"],
     description="JavBus 影片资料…", homepage="https://www.seedmm.help")
```

| 字段 | 变化 | 说明 |
|---|---|---|
| `groups` | **删除** | 由 `provides` 推导，不再手填 |
| `capabilities` | **改名 `fields`** | 原名与「能力」冲突；它实际表示「能产出哪些 MovieInfo 字段」 |
| `provides` | **新增** | 能力列表，调度依据 |
| `type` | 保留 | `site/api/local`，仅用于 UI 徽标 |

### 4.2 状态模型（`crawler-settings.json` → `status`）

**Before**：`{"state": "ok|error|warning|unknown", "message": "..."}`

**After**（对齐 JavBoss）：

```json
{
  "state": "ok | not_found | invalid_response | http_error | timeout | dns_error | tls_error | network_error | request_error | canceled | unknown",
  "http_status": 403,
  "elapsed_ms": 1064,
  "message": "访问被拒绝",
  "checked_at": "2026-11-07T13:42:11+08:00",
  "mode": "check | test"
}
```

前端逐条翻译（截图效果）：

| state | 中文 | 颜色 |
|---|---|---|
| `ok` | 可用 | 绿 |
| `not_found` | 未获取到测试数据 | 黄 |
| `invalid_response` | 数据解析失败或不完整 | 黄 |
| `http_error` 401 | 需要认证 | 黄 |
| `http_error` 403 | 访问被拒绝 | 黄 |
| `http_error` 407 | 代理需要认证 | 黄 |
| `http_error` 429 | 请求过于频繁 | 黄 |
| `timeout` | 连接超时 | 红 |
| `dns_error` | 域名解析失败 | 红 |
| `tls_error` | 证书验证失败 | 红 |
| `network_error` | 网络或代理连接失败 | 红 |
| `unknown` | 未检测 | 灰 |

### 4.3 预设的刮削器列表（v2 核心变更）

**Before（v1，按分类分组 —— 用户反馈「非常混乱」）**

```yaml
crawler:
  selection:
    normal: [avsox, javbus, javdb, javmenu, javlib, mgstage, prestige]
    fc2:    [fc2, avsox, javdb, javmenu, fc2ppvdb]
    cid:    [avsox]
    getchu: [dl_getchu]
    gyutto: [gyutto]
```

**After（v2，一份扁平有序列表，默认空）**

```yaml
crawler:
  scrapers: [javbus, javdb, avsox]     # ← 用户只维护这一行
```

运行期由 `tasks._expand_selection()` 展开成引擎需要的分类结构：

```python
# 每个分类都用同一份列表 —— 用户选了什么就并发跑什么
{'normal': ['javbus','javdb','avsox'],
 'fc2':    ['javbus','javdb','avsox'],
 'cid':    ['javbus','javdb','avsox'], ...}
```

**兼容性**

- 旧预设的 `selection` 在读取时自动压平（`server._flatten_selection()`，按 `normal` 优先）；
- 保存时只写 `scrapers`，不再写 `selection`；
- 未选任何刮削器 = 每个分类都是空列表，任务不会调用任何刮削器（符合「默认空」语义）。

### 4.4 兼容字段（旧版说明，已由 4.3 取代）

~~`crawler.selection` 的分类结构保留，但改为自动推导~~ —— v2 已改为「扁平列表 + 运行期展开」。

---

## 五、调度与执行

### 5.1 输入形态判定

新增 `movsp/scrape_plan.py`：

```python
def detect_inputs(path: Path, film: dict | None) -> dict:
    """返回 {'code': 'SSIS-001'} 或 {'title': '某电影'} 或两者都有。"""
```

判定顺序：

1. 媒体库里有手动填写的 `dvdid` → `code`
2. 引擎 `get_id()` 从文件名识别出番号 → `code`
3. NFO 里有 `<title>` / 目录名 → `title`
4. 都没有 → `title`（用文件名兜底）

### 5.2 候选刮削器计算

```python
def candidates(inputs: dict, scrapers: list[dict]) -> list[dict]:
    """按能力 ∩ 输入 过滤，保持用户设定的顺序。"""
```

### 5.3 两条执行器

| 执行器 | 触发条件 | 实现 |
|---|---|---|
| **JAV 引擎** | 候选中有 `movie_by_code` 的刮削器 | 现有链路：写 `task-config.yml` → 子进程 `python -m javsp` |
| **原生执行器**（新增） | 候选中有 `movie_by_title` 等，但**没有**可用番号 | 主进程内直接调 `parse_data`，产出统一结果 |

原生执行器复用现有 `parse_data(movie)` 契约（不改插件写法），只是不再起子进程：

```python
# movsp/native_scrape.py（新增）
def scrape(task, candidates) -> MovieInfo:
    movie = MovieInfo(...)
    for scraper in candidates:
        try: module.parse_data(movie)
        except ...:
            continue   # 单源失败不影响整体
    return movie
```

> 二期实现。一期只做能力模型 + 状态，执行器保持现状。

### 5.4 完整流程

```
用户点「刮削」
   ↓
detect_inputs(file)  →  {code: "SSIS-001"}  或  {title: "流浪地球"}
   ↓
candidates(inputs)   →  [JavBus, JavDB, TMDb]（按预设顺序）
   ↓
有可用番号？
   ├── 是 → JAV 引擎（现有链路，selection 自动填充）
   └── 否 → 原生执行器（二期）
   ↓
结果写入 NFO / 媒体库
```

---

## 六、UI 改造

### 6.1 刮削器管理页（`crawler-config`，v2）

**只做三件事：显示、开关、检测状态。**

```
┌──────────────────────────────────────────────────────┐
│ 刮削器管理                    [全部体检] [刷新] [＋] │
│ 这里只负责显示与检测连接状态。用哪几个，去预设里选。  │
├──────────────────────────────────────────────────────┤
│ [全部 21] [已启用 18] [已停用 3] [站点] [API] [本地] │
│                                      [搜索……]        │
├──────────────────────────────────────────────────────┤
│ ┌────────────────────┐ ┌────────────────────┐        │
│ │●可用  [开关] 已启用│ │●访问被拒绝 [开关]  │        │
│ │JavBus        javbus│ │TMDb          tmdb  │        │
│ │[站点爬虫] [内置]   │ │[API刮削器] [插件] │        │
│ │JavBus 影片资料…    │ │通用影视元数据…     │        │
│ │资料解析正常        │ │访问被拒绝          │        │
│ │HTTP 200  1754 ms   │ │HTTP 403  1064 ms   │        │
│ │[编辑信息][检测]    │ │[编辑信息][检测]    │        │
│ └────────────────────┘ └────────────────────┘        │
└──────────────────────────────────────────────────────┘
```

- **单一扁平网格**，不按能力分区；
- 卡片**不显示能力徽标**（能力是后端内部概念）；
- 编辑弹窗只有：展示名称、说明、主页、专项配置（API Key 等）；
- 状态徽标细粒度（见 §4.2），显示 HTTP 码、耗时、检测时间。

### 6.2 刮削预设页（`crawler` 页签，v2）

**一份扁平有序列表，默认空。**

```
┌──────────────────────────────────────────────────────┐
│ 刮削器  选择用哪些刮削器取资料。                      │
├──────────────────────────────────────────────────────┤
│ 本预设使用的刮削器（按顺序并发执行，靠前的优先）      │
│  [1] JavBus javbus  ↑↓ ×                            │
│  [2] JavDB  javdb   ↑↓ ×                            │
│  [3] AVSOX  avsox   ↑↓ ×                            │
│                                                      │
│ 可添加的刮削器                                        │
│  [＋ MGStage] [＋ JavLibrary] [＋ TMDb]              │
│  [输入或选择刮削器名称                    ] [＋]      │
└──────────────────────────────────────────────────────┘
```

- **默认为空**（新建预设不预置任何刮削器）；
- 序号即优先级，可用 ↑↓ 调整；
- 下方列出所有「已启用但尚未加入」的刮削器，一键添加；
- **完全没有影片分类的概念**。

### 6.3 状态枚举前端映射

```javascript
const SCRAPER_STATUS_LABELS = {
  ok: '可用', not_found: '未获取到测试数据', invalid_response: '数据解析失败',
  http_error: 'HTTP 响应异常', timeout: '连接超时', dns_error: '域名解析失败',
  tls_error: '证书验证失败', network_error: '网络或代理连接失败',
  request_error: '请求异常', canceled: '已取消', unknown: '未检测',
};
// http_error 再按 http_status 细分（401/403/407/429）
```

---

## 七、检测逻辑（对齐 JavBoss）

### 7.1 能力感知的样本

每个刮削器**必须声明检测样本**，而不是统一用一个番号：

```python
_def("javbus", ..., sample="SSIS-001")          # movie_by_code
_def("avsox",  ..., sample="030919_047")        # 无码番号格式不同
_def("tmdb",   ..., sample="流浪地球")           # movie_by_title
_def("avwiki", ..., sample="三上悠亜", sample_capability="actress_by_name")
```

`sample` 缺省时按能力取默认值；**没有样本 = 无法检测**（前端禁用检测按钮）。

### 7.2 严格的成功判定

```
ok  ⟺  无异常 且 解析出数据 且 关键字段非空
```

对 `movie_by_code`：`title` 非空且不等于查询词（防止站点返回搜索页）。
对 `movie_by_title`：`title` 非空且 `tmdb_id` 存在。
对 `actress_by_name`：姓名匹配且至少一个扩展字段非空。

### 7.3 错误分类

复用 `_run_crawler_test` 子进程输出，但**解析异常类型**：

| 异常 | state |
|---|---|
| `requests.exceptions.Timeout` | `timeout` |
| `requests.exceptions.SSLError` | `tls_error` |
| `socket.gaierror` / DNS 相关 | `dns_error` |
| `requests.exceptions.ConnectionError` | `network_error` |
| 响应有 HTTP 状态码且 ≥400 | `http_error` |
| 解析出空数据 | `invalid_response` |
| 明确的「未找到」 | `not_found` |
| 其它 | `request_error` |

---

## 八、实施计划

### 一期：能力模型 + 状态（不动引擎、不动执行器）

| 步骤 | 文件 | 工作量 |
|---|---|---|
| 1. `_def()` 加 `provides`/`fields`/`sample`，删除 `groups` | `scrapers.py` | 中 |
| 2. 21 块积木补 `provides` / `sample` | `scrapers.py` | 中 |
| 3. 状态模型扩展 + 异常分类 | `scrapers.py`、`server.py` | 中 |
| 4. `/api/scrapers` 返回新字段 | `server.py` | 小 |
| 5. 管理页：按能力分区、能力徽标、状态翻译、删除 groups 编辑 | `app.js`、`overrides.css` | 大 |
| 6. `selection` 自动推导（兼容层） | `tasks.py` | 小 |
| 7. 「影片分类」移入高级折叠 | `app.js`、`index.html` | 小 |
| 8. 测试 | `tests/` | 中 |

**一期结束状态**：用户不再接触「影片分类」，新增刮削器只需声明能力。JAV 刮削链路完全不变。

### 二期：原生执行器（支持普通电影）

| 步骤 | 文件 |
|---|---|
| 1. `detect_inputs()` 输入形态判定 | `scrape_plan.py`（新） |
| 2. `candidates()` 候选计算 | `scrape_plan.py` |
| 3. `native_scrape.py` 主进程执行器 | 新增 |
| 4. `task_type='native'` 任务分支 | `tasks.py` |
| 5. 结果写入 NFO / 媒体库 | 复用 `library_store` |

### 三期：剧集刮削 + 预设合并

- `tv_by_title` + 季集结构 → `tvshow.nfo` / `SxxExx.nfo`
- 预设表单按刮削器动态显示字段（B 方案）

---

## 九、已确认的决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 管理页布局 | ~~按能力分区~~ → **单一扁平网格**（v2 按用户反馈修正） |
| D2 | `groups` 删除方式 | **硬删除**，旧数据读取时忽略 |
| D3 | 预设列表形态 | ~~按分类分组~~ → **一份扁平有序列表，默认空**（v2） |
| D4 | 一期范围 | 纯增量（能力模型 + 状态 + 扁平列表），不改刮削执行行为 |
| D5 | 能力归属 | **后端内部**，用户不可见、不可编辑（v2） |

> v1 曾计划「按能力分三区」，实机验证后发现仍然混乱 —— 用户还要理解「番号/标题/演员」
> 这层抽象。v2 彻底去掉，管理页只剩展示 + 状态，选择权完全交给预设。

---

## 十、风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 能力矩阵写错，导致某分类无刮削器 | 任务失败 | 兼容层默认回退到 `movie_by_code` 全集 |
| `selection` 自动推导覆盖用户配置 | 配置丢失 | D3 选 A：手写优先 |
| 原生执行器与引擎 NFO 格式不一致 | 媒体库读不到 | 复用 `library_store` 的 NFO 写入函数 |
| 状态枚举扩展导致旧数据不兼容 | UI 显示未知 | 未知值统一降级为 `unknown` |
| 分类彻底移除后引擎校验失败 | 任务无法启动 | 兼容层始终生成合法的 `selection` |

---

## 十一、验收清单

### 一期

- [x] 新增一个刮削器，只需在 `CATALOG` 声明 `provides`/`sample`，无需填分类
- [x] 管理页是单一扁平网格，只显示 + 状态检测，无能力徽标/分区
- [x] 状态能区分 `dns_error` / `http_error` / `timeout` 等 11 种
- [x] 预设里是一份扁平有序列表，**默认为空**，可增删与排序
- [x] 「影片分类」默认折叠，不改也能正常刮削
- [x] 现有 21 个刮削器全部迁移
- [x] 旧预设（按分类 `selection`）读取时自动压平
- [x] Python 572 项 + 前端 14 个文件全绿

### 二期

- [ ] 无番号的视频能被 TMDb 刮削
- [ ] 同一个预设可同时处理 JAV 与普通电影
- [ ] 刮削结果正确写入 NFO

---

## 附：与现状的差异对照

| 维度 | v2.3.2 原始 | 本期 v2 |
|---|---|---|
| 调度键 | `media_types` 分类 | 预设里用户选的扁平列表 |
| 预设存储 | `selection: {分类: [...]}` | `scrapers: [...]`（默认空） |
| 刮削器声明 | `groups` + `capabilities`(字段) | `provides`(能力,内部) + `fields` + `sample` |
| 能力可见性 | — | **仅后端内部**，界面不显示 |
| 状态 | 4 种 | 11 种 + HTTP 码 + 耗时 |
| 检测 | 跑一次 `parse_data` | 能力感知样本 + 严格判定 + 异常分类 |
| 管理页职责 | 编辑代码 + 按分类配爬虫 | **只显示 + 检测状态** |
| 用户心智 | 先选 JAV/电影，再选分类 | 建预设 → 勾几个刮削器 |
| 引擎改动 | — | **零** |
