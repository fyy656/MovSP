# 刮削器「搭积木」架构说明（开发者）

> 面向维护者：本文说明刮削器（爬虫）如何注册、启用、配置、测试，以及怎样继续添加新的
> 刮削器（例如带 API Key 的 TMDb、其它影视数据库、离线资料源）。用户使用说明请见界面内的
 「刮削器管理」页与「刮削预设 → 爬虫」页。

## 一、四个概念

| 概念 | 位置 | 说明 |
|---|---|---|
| **积木定义（目录）** | `movsp/scrapers.py` → `CATALOG` | 后台预先登记好每块积木：稳定 id、展示名、类型、推荐分类、说明、配置项 schema、默认开关。 |
| **积木源码** | 内置：`vendor/JavSP/javsp/web/*.py`；插件：`scrapers.py` → `PLUGIN_SOURCES` | 内置站点爬虫用引擎自带源码（只读）；插件源码随本程序发布，激活时物化到 `data/crawlers/<name>.py`。 |
| **积木状态** | `data/crawler-settings.json` | 每块积木的启用/停用、专项配置（如 API Key）、最近一次测试/体检状态、展示信息覆盖。 |
| **积木使用** | 预设 `crawler.selection.<分类>` | 用户在预设页勾选启用的积木，保存为有序列表；任务启动时按列表并发调用。 |

数据流：

```
CATALOG + PLUGIN_SOURCES
   │ ensure_seed_data() / ensure_plugin_files()
   ▼
data/crawlers/<name>.py  +  data/crawler-settings.json
   │ /api/scrapers（目录、开关、配置、状态、源码）
   ▼
刮削器管理页：启用 / 编辑 / 测试 / 体检
   │ 预设页勾选 → preset.form.crawler.selection
   ▼
tasks._preset_config_data() 过滤停用积木、校验
   │ _build_task_config() → data/task-config/<id>.yml
   ▼
引擎子进程 `python -m javsp -c <yml>`，PYTHONPATH 含 data/crawlers
   │ 环境变量 MOVSP_SCRAPER_SETTINGS 指向 crawler-settings.json
   ▼
import_crawlers() → parse_data(movie) → info_summary() 汇总
```

## 二、每块积木的契约

- **id = 模块名**：必须匹配 `^[a-z][a-z0-9_]*$`，且不能与已登记的名字重复。
- **入口**：模块级 `parse_data(movie: MovieInfo)`，就地填充 `MovieInfo` 字段（返回值为空）。
  这是 JavSP 引擎唯一的调用约定（`vendor/JavSP/javsp/__main__.py` 的 `parallel_crawler`）。
- **异常**：优先抛 `MovieNotFoundError` / `SiteBlocked` / `CredentialError` 等
  （`javsp/web/exceptions.py`），让引擎正确区分「没找到」与「网络失败」。
- **配置读取**：插件通过 `os.environ['MOVSP_SCRAPER_SETTINGS']` 指向的 JSON 文件读取
  自己的配置。`scrapers.py` 的 `_PLUGIN_COMMON` 已封装 `_settings()` 助手，
  新插件直接复用即可。**不要把密钥写进命令行参数或日志。**
- **网络**：用 `requests`，代理可复用 `Cfg().network.proxy_server`（`_PLUGIN_COMMON._proxies()`）。

`crawler.selection` 的类型是 `Dict[str, List[str]]`，值是纯字符串（模块名），
因此新增任意名字都能通过 `Cfg.model_validate()`；无需改动引擎的 config 模型。

## 三、添加一块新积木（三步）

以「再添加一个带 API 的刮削器」为例：

1. **`CATALOG` 加一条定义**（`movsp/scrapers.py`）：

   ```python
   _def("my_api", "我的 API", "plugin", "api", ["normal"],
        "说明文字…", "https://example.com",
        ["title", "cover", "plot"],
        [_field("api_key", "API Key", secret=True, required=True)],
        enabled_by_default=False),
   ```

   - `kind="plugin"`：随程序发布、可在管理页编辑源码；
   - `type="api"` / `"site"` / `"local"`：仅用于界面分组与徽标；
   - `groups`：推荐用于哪些影片分类（`normal`/`fc2`/`cid`/`getchu`/`gyutto`，或自定义分类 id）；
   - `settings_schema`：管理页自动生成输入框，`secret=True` 的字段对非管理员打码；
   - `enabled_by_default=False`：默认停用，用户手动启用（推荐给需要密钥 / 额外配置的积木）。

2. **`PLUGIN_SOURCES` 加源码**：在 `scrapers.py` 里新增一段以 `_PLUGIN_COMMON` 开头的
   源码字符串，实现 `parse_data(movie)`，用 `_settings()` 读取配置。可参考内置的
   `tmdb`（REST API）与 `local_json`（离线文件）两个范例。

3. **无需其它改动**：`ensure_plugin_files()` 会在服务启动 / 任务启动 / 测试时把源码写入
   `data/crawlers/`，引擎按模块名导入即可。前端目录、开关、编辑框、测试面板全部自动生成。

> 已登记的内置站点爬虫（19 个）只需在 `CATALOG` 里有展示定义，源码由 vendor 提供，
> 不需要写 `PLUGIN_SOURCES`。

### 用户手工新增的积木

管理页「＋ 新增积木」会创建一个 `parse_data` 模板文件，用户可在界面里直接写 Python 并保存。
`data/crawlers/<name>.py.revision` 用于区分「随程序发布的源码」与「用户改过的源码」：

- 存在 revision 且哈希一致 → 程序升级时可安全覆盖更新；
- 无 revision 或哈希不一致 → 视为用户所有，**永不覆盖**；
- 插件可用「恢复原始代码」按钮重新写回随程序发布的版本。

## 四、开关如何生效

- `/api/scrapers/{name}/enable` 写入 `crawler-settings.json` 的 `enabled` 映射。
- `scrapers.disabled_names()` 合并三类来源：`disabled_built_ins`（兼容旧数据）、
  `enabled` 显式映射、`enabled_by_default=False` 的默认值。
- `storage.get_disabled_built_in_crawlers()` 已改为读取同一份数据（兼容旧调用）。
- `tasks._preset_config_data()` 在任务启动时把停用名字从 `crawler.selection` 里移除，
  因此**停用是硬生效**：即使旧预设里还留着这个名字，任务也不会调用它。
- 旧的 `/api/crawler-config/built-in/{name}` DELETE / restore 已映射到同一开关，
  第三方集成不会失效。

## 五、配置与密钥安全

- 密钥只写 `data/crawler-settings.json`（`data/` 已被 gitignore），不进预设、不进日志、
  不进命令行。
- 引擎 / 测试子进程只收到**文件路径**（`MOVSP_SCRAPER_SETTINGS`），由插件自行读取。
- `GET /api/scrapers` 对非管理员把 `secret=True` 的字段替换为 `********`，并隐藏源码编辑入口；
  `GET /api/scrapers/{name}/source` 要求管理员。
- 管理页提交 `********` 表示「保留原值」，避免回显打码值把密钥覆盖掉。

## 六、测试与状态

| 操作 | 后端 | 说明 |
|---|---|---|
| 单块「测试抓取」 | `POST /api/scrapers/{name}/test` `{"input_value": "..."}` | 复用 `/api/crawler-config/test` 的子进程，返回全部 MovieInfo 字段 + 运行输出，并把状态写回目录。 |
| 单块「仅体检」 | 同上 `{"check_only": true}` | 只检查开关、必填配置、源码语法，不联网。 |
| 「全部体检」 | `POST /api/scrapers/check-all` | 逐块体检已启用的积木，统一展示结果。 |
| 状态徽标 | `scrapers.status_of()` | `ok` / `error` / `warning` / `unknown`，含时间与说明，持久化在 crawler-settings.json。 |

## 七、兼容与回归

- 旧接口全部保留：`/api/crawler-config`、`/names`、`/{name}`、`/custom`、
  `/built-in/{name}` DELETE / restore、`/test`。
- `data/crawler-settings.json` 沿用旧文件；旧的 `{"disabled_built_ins": [...]}` 仍可读取。
- 回归测试：`tests/test_scraper_registry.py`（后端 16 项）、`tests/test_scraper_client.cjs`（前端 3 项）。
- 改了 `movsp/web/assets/*` 后需 `python scripts/build_demo.py` 重建演示站。
