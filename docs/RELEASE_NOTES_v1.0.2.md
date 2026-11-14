# MovSP 1.0.2

**重要修复：普通影视无法刮削。**

1.0.0 引入的回归问题——TMDb 刮削器执行时报 `NameError`，导致所有电影/剧集刮削失败，显示「所有刮削器都没有返回可用资料」。JAV 不受影响。

升级后重启服务即可，插件文件会自动重新生成。

## 其他

- ffmpeg 查找范围扩大（winget / scoop / chocolatey 的默认位置都能识别）
- 支持 `MOVSP_FFMPEG` 环境变量指定 ffmpeg 位置
