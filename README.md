# 观影报告推送 (ViewReport) for MoviePilot

定时生成「昨日观影报告」的 MoviePilot 插件：从 Emby 的播放记录取数，**电影 + 剧集合并**成一张总榜长图，并可附带按 Emby 账号分节的文字榜单，推送到微信 / Telegram / 企业微信 / 站内信等渠道。

当前版本：**v2.0.3**（毛玻璃版式，480 宽长图：前三名海报卡片 + 第 4 名起列表）。

## 它能做什么

- 数据源：Emby 官方 **Playback Reporting** 插件的 `user_usage_stats`。
- 图片：**所有 Emby 账号汇总成一张总榜**（同名影片跨账号合并次数与时长），图片里不出现账号名。
- 文字：按账号分节列出完整榜单，无观影记录的账号不显示。
- 报告形式三选一：`纯文字` / `图片` / `文字 + 图片`（推荐，只发一条消息）。
- 零外部素材依赖：背景、玻璃卡片、渐变全部由代码绘制，封面取 Emby 原画；字体运行时自动探测，缺失只降级不崩溃；任何异常都会写日志并发站内信。

## 前置条件（缺一个就出不了图）

1. Emby 已安装官方 **Playback Reporting** 插件，且 API Key 有权限查 `user_usage_stats`。
2. **MoviePilot V2**（已在 v2.15.6 验证）。仓库提供 `plugins.v2/` + `package.v2.json`；**V3 尚未适配**，V3 的插件市场里看不到本插件。
3. MP 后台「设置 → 媒体服务器」里已配置好 Emby。
4. 用「图片」或「文字+图片」时，MP 必须有一个**公网可访问**的地址，并填进「MoviePilot域名」，否则微信/手机打不开图片外链。纯文字模式不需要。
5. 容器内要有中文字体（官方镜像一般自带 `wqy-zenhei.ttc`）。中文显示成方块时见下方「常见问题」。

## 安装

仓库地址（公开，GitHub `main` 分支）：

```
https://github.com/7tnv8n8nb7-beep/moviepilot-viewreport
```

### 方式 A：插件市场添加仓库（推荐）

1. MP 后台 →「插件」→「插件市场设置」→「插件仓库地址」，把上面的地址加入列表（多个地址用英文逗号分隔），保存。
   - 也可以在 `docker-compose` 里设环境变量 `PLUGIN_MARKET=https://github.com/7tnv8n8nb7-beep/moviepilot-viewreport`，改完重启 MP。
2. 刷新插件市场 → 搜索「观影报告推送」→ 安装。
3. **重启一次 MoviePilot 容器**（新增插件需重启才会被扫描到），再到插件页启用并填写配置。

### 方式 B：手动放文件（找不到入口 / 离线部署）

仓库里三份 `__init__.py` 内容完全一致，按 MP 版本取对应目录即可：

| MP 版本 | 仓库目录 | 市场索引 |
|---|---|---|
| V2（推荐） | `plugins.v2/viewreport/` | `package.v2.json` |
| V1 | `plugins/viewreport/`、`data/viewreport/` | `package.json` |

下载对应目录里的 `__init__.py`，放到容器插件目录下名为 `viewreport` 的文件夹（例如 V2 常见为 `/config/app/plugins/plugins.v2/viewreport/__init__.py`，不同部署可能是 `/app/app/plugins/viewreport/`）。**照着已装插件的目录层级放最稳妥**：

```bash
docker exec <容器名> find / -type d -name 'viewreport' 2>/dev/null   # 或找一个已装插件的目录
docker cp plugins.v2/viewreport <容器名>:/config/app/plugins/plugins.v2/
docker restart <容器名>
```

### 方式 C：API 安装（进阶）

```
GET /api/v1/plugin/install/viewreport?repo_url=<URL编码后的仓库地址>&force=true
```

装完可用 `GET /api/v1/plugin/file/viewreport/__init__.py` 回读文件大小，确认落到的是最新版本。

## 配置（装完必填）

| 配置项 | 建议值 | 说明 |
|---|---|---|
| 启用插件 | 开 | |
| 立即运行一次 | 开一次用于验证 | 跑完会自动置回关 |
| 执行周期 | `0 8 * * *` | 5 位 cron，每天 08:00 |
| 报告天数 | `1` | **1 = 只统计昨日 00:00~23:59**，标题与日期显示具体某天；>1 才是区间 |
| 排行数量 | `10` | 总榜取前 N 部 |
| 报告形式 | `文字 + 图片` | `纯文字` 不依赖域名，最省事；`图片` 只发图 |
| MoviePilot域名 | `http://你的域名:端口` | 图片模式**必填**、末尾不带 `/`、必须公网可达 |
| 消息类型 | `Plugin`（或 `Web`） | 只决定消息归类；**真正发到哪个渠道由「设置 → 通知」里各渠道开关决定** |
| 显示观看时长 | 是 | |
| 媒体服务器 | 选你的 Emby | 留空表示全部 Emby 服务器 |
| 中文字体路径 | 留空自动探测 | 中文方块时填绝对路径，如 `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc` |
| 黑名单媒体库 | 留空 | 多个名称用英文逗号分隔 |
| 自定义 Emby Host / ApiKey | 留空 | 留空读取 MP 里已配置的 Emby 连接信息 |

> 图片文件名是 `report_s1.jpg`（多台 Emby 依次 `s2`、`s3`…），外链为
> `http://你的域名/report_s1.jpg?_timestamp=...`。

## 验证

1. 保存配置，打开「立即运行一次」。
2. 插件日志依次出现：取到电影/剧集行数 → `有观影记录的账号 N 个` → `汇总总榜 N 部` → `已生成长图 /public/report_s1.jpg` → `汇总总榜推送成功`。
3. 收到一条消息 + 一张长图；点图片能打开原图。
4. 用手机浏览器直接打开 `http://你的域名/report_s1.jpg` 能显示，说明外链通了。

## 常见问题

- **没数据 / 日志 404**：Emby 没装 Playback Reporting，或 API Key 权限不足。
- **只有文字没有图 / 图片打不开**：「MoviePilot域名」没填、填的是内网地址，或 `/public/` 没被 MP 静态服务暴露。
- **中文显示方块**：容器缺 CJK 字体。装 `fonts-noto-cjk`，或在「中文字体路径」指定一个 `.ttf/.ttc` 绝对路径。数字优先用 Liberation Sans / DejaVu，缺失时回退中文字体自带的拉丁字形，不会崩。
- **装到旧版本**：GitHub CDN 有缓存，等几分钟或重启 MP 后用 `force=true` 重装。
- **安装后插件页不出现**：重启 MP 容器；插件目录/ID 大小写不一致时，可改试驼峰 `ViewReport`。
- **MP V3**：暂未适配，需要单独做 V3 迁移。

## 隐私提醒

本仓库公开。请不要把自己的域名、Emby API Key、通知渠道 token 写进 issue、README 或提交内容里；上表中的域名一律用占位符。
