![MovSP](./movsp/web/assets/movsp-logo.png)

# MovSP

**NAS 媒体库一站式方案**：刮削资料、整理文件、浏览播放。

同时支持两条刮削路径——按番号的 **JAV**，以及按标题/TMDb/IMDb ID 的**电影与剧集**。一个界面管理全部媒体，配置逻辑完全一致。

> 本项目是上游开源项目的衍生修改版本，不是原作者的官方项目，也不代表原作者的观点或背书。
> 来源、署名与授权说明见下方 [来源与版权](#来源与版权)。

---

## 为什么是 MovSP

MovSP 从 JavSP-Web-Next 演进而来。早期它只是 JavSP 的网页界面，如今已经覆盖媒体库的完整链路：索引磁盘、刮削资料、整理文件、浏览播放。

关键区别在于**不再只服务一种媒体**：

| | JAV | 电影 / 剧集 |
|---|---|---|
| 识别依据 | 番号 | 片名或 TMDb / IMDb ID |
| 刮削来源 | JavSP 引擎 + 多个资料站 | TMDb API |
| 整理与裁剪 | 支持 | 不做 |
| 详情页 | 大图 + 信息 + 播放 + 分类跳转 | 同左 |
| 配置界面 | 完整 JavSP 选项 | 只显示真正生效的项 |

两条路径由**预设类型**自动决定，用户不需要（也无法）手动切换——选了「电影 / 剧集」预设，就永远走标题路径。

---

## 主要功能

### 媒体库

- 直接索引磁盘上的视频、NFO 与图片，**以磁盘为唯一真源**，不建立独立数据库副本。
- 多库多文件夹、后台扫描、增量更新、文件丢失检测。
- 按番号 / 标题 / 演员 / 路径搜索，按演员 / 片商 / 年份分类浏览。
- 隐私模式模糊封面；海报支持竖版（2:3）与横版（16:9）切换、四档尺寸。
- 「上一页」智能返回：恢复进入前的库、筛选、搜索词、分页与滚动位置。

### 影片详情页

- 超大横版大图，hover 出现播放按钮。
- 信息表：番号 / 发行日期 / 时长 / 片商 / 导演 / TMDb / IMDb。
- 底部图片墙：竖版海报、横版图、剧照全部列出，点击全屏查看。
- 演员、片商可点击，直接跳到同类作品。
- 「编辑资料」独立成页，只负责编辑与刮削，不干扰浏览。

### 刮削器（搭积木）

- 后台定义 21 个刮削器模块及其能力，前端按需勾选启用。
- 按「能做什么」自动分派，不必再区分 JAV 或普通影视。
- 影视预设隐藏 8 个 JAV 专用选项（番号爬虫列表、JavDB 封面策略、FC2 镜像等），避免误设。

### 剧集

- 一季一个目录时，在资料页填写**季编号**，即可精确获取该季资料。
  TMDb 一个剧 ID 含全部季，不填只会默认取第 1 季。
- 分集剧照下载为 `<视频名>-thumb.jpg`，Emby / Jellyfin 可识别。
- 「剧 → 季 → 集」三级浏览，识别顺序 NFO > 目录名 > 文件名。

### 刮削可靠性

- 图片按 TMDb 支持的尺寸下载（`w500` / `w1280` / `w780`），失败自动重试。
- 预设可勾选「必须取得的资料」，未取到（含封面未落盘）会在任务上标出缺失项。
- 任务名显示剧名而非集号；失败原因、缺失字段都能直接在界面看到。

### 其他

- 多结果候选：搜索到多部同名影片时暂停，由用户确认是哪一部。
- 定时扫描、qBittorrent 联动、下载完成后自动刮削。
- 连接 Emby / Jellyfin，整理后刷新媒体库。
- AI 刮削与翻译、上下文自动压缩。
- CookieCloud、代理、路径映射、封面重试、任务文件还原。
- 个人中心支持用户名 + 6 位 PIN 的匿名评论身份。

---

## 快速开始

### Windows

1. 从 [Releases](https://github.com/fyy656/MovSP/releases/latest) 下载 `MovSP.exe`。
2. 双击运行，浏览器打开 `http://127.0.0.1:8090/login`。
3. 首次账号密码均为 `admin`，登录后立即在「系统设置 → 用户管理」修改。

配置与任务记录默认保存在程序旁的 `data` 文件夹，升级时保留即可。

#### 局域网访问

默认只允许本机访问。需要手机、平板或局域网其他设备访问时：

1. 登录后进入「系统设置 → 网络访问」，勾选「允许局域网访问」并保存
   （Windows 会自动尝试添加防火墙规则，需要管理员权限）。
2. 重启程序，面板会显示形如 `http://192.168.x.x:8090` 的地址。

HTTP 为明文传输，请仅在可信家庭网络使用；公网访问务必配置 HTTPS 反向代理。
Docker 部署本身已可通过 `http://服务器地址:8090` 访问，监听地址由 `MOVSP_HOST` 决定。

### Docker Compose

安装 Docker 和 Compose，在空目录创建 `docker-compose.yml`：

```yaml
services:
  movsp:
    image: ghcr.io/fyy656/movsp:latest
    container_name: movsp
    restart: unless-stopped
    ports:
      - "8090:8090"
    environment:
      MOVSP_HOST: 0.0.0.0
      MOVSP_PORT: 8090
      MOVSP_TIMEZONE: Asia/Shanghai
      TZ: Asia/Shanghai
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./data:/app/data
      - /你的影片目录:/media
```

启动后打开 `http://服务器地址:8090/login`。
`/app/data` 保存配置与任务记录，**务必挂载**到宿主机；影片目录按实际路径挂载。

#### 从旧版本迁移

环境变量已由 `JAVSP_WEB_*` 改为 `MOVSP_*`，但**仍兼容旧变量名**——
用旧配置启动不会丢数据，建议之后逐步换成新名。

---

## 可选：安装 ffmpeg

未刮削的影片会用视频画面当封面预览。这个功能需要 **ffmpeg**，没有时会自动退回文字占位，不影响其他功能。

程序按以下顺序查找：

1. 环境变量 `MOVSP_FFMPEG`（填目录或 exe 都行）
2. 系统 `PATH`
3. 常见安装目录 —— winget / scoop / chocolatey 的默认位置

所以**大多数情况下装完即可用，不需要手动配 PATH**：

```bash
# Windows
winget install Gyan.FFmpeg

# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg
```

如果装在自定义位置，设置环境变量指向它：

```bash
MOVSP_FFMPEG=D:\tools\ffmpeg\bin        # 目录
MOVSP_FFMPEG=D:\tools\ffmpeg\bin\ffmpeg.exe   # 或直接指定文件
```

> Docker 镜像已内置 ffmpeg，无需额外操作。
> 装好后不用重启服务，下次请求会重新检测。

---

## 文档

完整的安装、配置、刮削、AI、自动化、故障排查与接口说明：

- 仓库内：`docs/docs.html`
- 在线：<https://fyy656.github.io/MovSP/docs.html>

---

## 来源与版权

### 上游项目

- **JavSP**（刮削核心）：<https://github.com/Yuukiy/JavSP>
  - 版权归原作者 **Yuukiy** 及 JavSP 贡献者所有。
- **JavSP-Web**（网页端）：<https://github.com/APecme/JavSP-Web>
  - 版权归原作者 **APecme** 及 JavSP-Web 贡献者所有。

MovSP 在上述项目基础上做了修改与二次开发，包括：

- 媒体库索引、批量刮削、失败重试、分类浏览、NFO 统一；
- **双路径刮削**（JAV 与电影/剧集）与声明式刮削器注册表；
- 影片详情页、图片墙、剧集季编号、智能返回；
- 界面、交互与文档的整体重构。

上游代码与资源按原有许可证分发，**原始著作权、许可证与署名均予以保留**，
未声明为原作者官方版本。

### 许可证

本项目及其衍生的上游代码遵循 **GNU General Public License v3.0**：

- 仓库根目录 `LICENSE` — 完整许可证文本
- `vendor/JavSP/LICENSE` — 上游刮削核心许可证
- `THIRD-PARTY-NOTICES.md` — 第三方组件许可证

### 免责声明

- 本项目与上游项目、任何影片站点、媒体服务器厂商均无隶属或合作关系；相关名称与商标归各自所有者。
- 本项目仅供个人学习与技术研究使用。使用者应自行确认所处理内容的合法性，并对自己使用本软件的行为负责。
- 软件按「现状」提供，不附带任何明示或暗示的担保；因使用本软件造成的任何损失由使用者自行承担。
- 请勿将本软件用于侵犯版权、传播违法内容或其他违反当地法律法规的用途。

---

## 参与开发

- 反馈与建议请通过本仓库 Issues 提交。
- 提交日志、截图或配置时请**自行脱敏**（去掉本地路径、域名、账号、番号等隐私信息）。

提交代码前请运行测试：

```bash
# 后端
python -m unittest discover -s tests -p "test_*.py"

# 前端
for f in tests/*.cjs; do node "$f"; done
```

---

## 目录结构

```
movsp/               Web 服务与前端资源
  web/                 前端页面与静态资源
  scrapers.py          刮削器注册表（搭积木）
  library_store.py     媒体库索引（以磁盘为准）
  tasks.py             任务调度与引擎调用
  native_scrape.py     标题路径的原生刮削
  scrape_plan.py       标题/年份/季号推导
vendor/JavSP/          上游刮削核心（保留原许可证）
docs/                  文档站点（GitHub Pages）与发布说明
scripts/               构建、演示站点与维护脚本
tests/                 单元测试与前端回归测试
```
