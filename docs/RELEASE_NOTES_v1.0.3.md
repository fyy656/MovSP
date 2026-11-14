# MovSP 1.0.3

首个正式版本。MovSP 是一个 NAS 媒体库：刮削资料、整理文件、浏览播放。

与只服务单一媒体的工具不同，它同时支持两条刮削路径——按番号的 **JAV**，以及按标题或 TMDb/IMDb ID 的**电影与剧集**，配置逻辑完全一致。

## 主要功能

**媒体库**
- 以磁盘为唯一真源，多库多文件夹、后台扫描、增量更新
- 按番号 / 标题 / 演员 / 路径搜索，按演员 / 片商 / 年份分类浏览
- 海报竖版 / 横版切换、四档尺寸
- 分类与剧集支持「智能返回上一页」，恢复筛选、搜索、分页与滚动位置

**影片详情页**
- 超大横版大图 + 信息表 + 播放
- 底部图片墙：竖版海报、横版图、剧照，点击全屏查看
- 演员、片商可点击跳转同类作品
- 编辑入口独立成「资料」页

**视频预览帧**
- 没刮到海报的影片，用视频画面代替空白
- 缩略图存 `data/thumbnails/`，不写影片目录，不影响刮削与 Emby
- 需要 ffmpeg（可选）；没有时自动退回文字占位
- 工具栏可随时关闭

**剧集**
- 季编号：一季一个目录时，指定 TMDb 取哪一季
- 分集剧照下载为 `<视频名>-thumb.jpg`
- 「剧 → 季 → 集」三级浏览

**刮削可靠性**
- 图片按 TMDb 支持的尺寸下载并自动重试
- 必填项未取到会在任务上标出缺失项
- 图片下载失败原因直接显示在任务卡片

**其他**
- 刮削器积木化：后台定义 21 个模块，前端按需勾选
- AI 刮削与翻译、CookieCloud、定时扫描、qBittorrent 联动
- 连接 Emby / Jellyfin，整理后刷新媒体库

## 安装

**Windows**：下载 `MovSP-1.0.3.exe`，双击运行，浏览器打开 `http://127.0.0.1:8090/login`。
首次账号密码均为 `admin`，登录后立即修改。

**Docker**：

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
    volumes:
      - ./data:/app/data
      - /你的影片目录:/media
```

## 关于版本号

之前的 1.0.0 ~ 1.0.2 已从仓库移除。1.0.3 是重新整理的单一提交，包含全部功能。

## 来源与版权

基于上游 **JavSP**（[Yuukiy](https://github.com/Yuukiy/JavSP)）与 **JavSP-Web**（[APecme](https://github.com/APecme/JavSP-Web)）二次开发。

遵循 GNU General Public License v3.0，原始著作权、许可证与署名均予以保留。
