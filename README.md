# Gravelle

Gravelle 是支持 Windows、Docker 和 NAS 的个人写真阅读器，也能读取图片漫画。电脑、手机和平板通过浏览器连接同一个服务，共用进度、书签、收藏和作品资料。使用 SQLite 保存资料，无需额外数据库服务。

[下载与发行说明](https://github.com/pumpkissster/Gravelle/releases) · [Windows 使用说明](docs/windows.md) · [Docker 部署说明](docs/docker-release.md)

## 能做什么

- 读取 JPG/JPEG、PNG、WebP、GIF、AVIF 散装图片，以及 CBZ/ZIP 图片作品。压缩包无需预先解压；不支持 CBR/RAR、PDF。图片能否显示也取决于浏览器支持。
- 创建多个写真库，每个库可选择多个目录，分别设置默认阅读方式、方向、排除规则和默认模特。
- 单页、双页、条漫阅读，支持全屏、页码跳转、图片适配和页码书签。双页支持封面独页、跨页图独页与配对调整。
- 编辑名称、日期、封面、阅读设置和模特关联；支持排序、搜索、收藏和连续阅读下一本。
- 个人账户支持用户名、头像和密码。所有设备登录同一账户，共享资料；没有多用户隔离。

新建写真库按单本作品浏览，已有漫画类型继续兼容。扫描、编辑资料和移除作品都不会删除或改写原图。

## Windows 安装

支持 Windows 10（1809 或更新）／Windows 11 x64，无需安装 Python 或 Docker。

1.8.3 起，安装程序按系统界面语言选择简体中文或 English，启动器采用项目 SVG 字标生成的品牌图。

1. 在 [GitHub Releases](https://github.com/pumpkissster/Gravelle/releases) 下载 `Gravelle-Setup-版本号-x64.exe` 并安装。
2. 打开 Gravelle，选择写真根文件夹，设置端口（默认 `8080`）。需要手机、平板访问时勾选「允许局域网访问」。
3. 点击「检查目录与网络」，再点击「启动阅读器」。浏览器会自动打开，启动器最小化到任务栏。
4. 首次访问注册账户，在网页创建写真库，选择根文件夹内的目录，保存后自动扫描。

启动器可查看地址、打开网页和停止服务。关闭浏览器不会停止服务；关闭启动器会提示停止服务。便携 ZIP 解压后运行 `Gravelle/Gravelle.exe`，整个文件夹需一起保留。

## Docker / NAS 部署

Docker Hub 镜像为 [`pumpkissster/gravelle`](https://hub.docker.com/r/pumpkissster/gravelle)，固定版本使用 `pumpkissster/gravelle:1.8.3`，支持 `linux/amd64`、`linux/arm64`。

下载 [Release](https://github.com/pumpkissster/Gravelle/releases/tag/v1.8.3) 中的 Docker 配置包，解压后复制 `.env.example` 为 `.env`，确认镜像地址并填写宿主机路径和运行用户：

```dotenv
GRAVELLE_IMAGE=pumpkissster/gravelle:1.8.3
COMICS_PATH=/volume1/photosets
DATA_PATH=/volume1/docker/sumi-reader/data
PUID=1000
PGID=1000
WEB_PORT=8080
```

创建目录，确保运行用户可读写真、可读写资料。UID/GID 用 `id 你的用户名` 查询，然后运行：

```sh
docker compose pull
docker compose up -d
```

打开 `http://服务器局域网IP:8080`，注册并创建写真库。群晖 Container Manager 可在「项目」中使用配置目录的 `compose.yaml` 与 `.env`；UID/GID 填实际具有目录权限的用户，不要直接套用示例数值。

宿主机 `COMICS_PATH` 只读挂载到 **`/library`**，网页目录请选择 `/library` 或其子目录；`DATA_PATH` 挂载到 **`/data`**。其他磁盘需先挂载到 `/library/子目录` 才能在网页选择。已有安装保留原数据路径、运行用户与项目名称。

也可下载完整源码，执行 `docker compose -f compose.build.yaml up -d --build` 自行构建。详细权限、更新与排查见 [Docker 部署说明](docs/docker-release.md)。

## 整理文件夹与自动资料

每个直接包含图片的文件夹是一部独立作品，父目录不会自动合并子目录。所选根目录直接放图片时也会生成作品。图片按文件名自然排序，例如 `1.jpg → 2.jpg → 10.jpg`。

```text
写真合集/
├── 模特甲/
│   ├── 2026-10-09 秋日作品/
│   │   ├── 001.jpg
│   │   └── 002.webp
│   └── 2026-10-10 室内作品.cbz
└── 模特乙/
    └── 2025-07-12 夏日作品/
        ├── 1.png
        └── 2.jpg
```

留空默认模特时，合集根目录下按「模特／作品」识别第一级目录；选择单个模特目录时，使用根目录名，去掉两端装饰括号和空白。根目录直接放图片或超过上述两级时不猜测模特。ZIP/CBZ 使用相同规则。

默认模特优先于目录推断；单本手动关联保留，作品可关联多人。模特改名不影响关联，删除模特不删除作品。

作品原名以有效的 **`YYYY-MM-DD`** 开头时自动提取发布日期，需补零、符合真实日历且后面不紧接数字。库设置可隐藏名称日期前缀，保留日期字段和手工名称；手动填写或清空的日期、模特不会被重扫覆盖。

封面默认使用首图，也可选择内页或上传 JPEG、PNG、WebP（最多 6 MiB），另存到资料目录。首次入库的加入时间不随重扫改变；**移动或重命名作品路径会生成新记录，原进度不会自动迁移。**

排除规则支持 `*`、`?`、`[]`，忽略大小写，匹配相对路径或文件／目录名；目录命中时跳过子目录。例如 `预览/`、`*preview*`、`*.gif`，空行和 `#` 注释忽略。默认每 300 秒扫描，也可手动扫描。

## 同步、资料与备份

| 运行方式 | 资料位置 |
| --- | --- |
| Windows 安装版／便携版 | `%LOCALAPPDATA%\Gravelle\data` |
| Docker | 容器 `/data`，对应 `.env` 的 `DATA_PATH` |
| 直接运行服务端 | `DATA_PATH`，默认项目 `data` |

资料包含账户、库、元数据、模特、封面、头像、进度、书签和收藏。Windows 配置与日志也在 `%LOCALAPPDATA%\Gravelle`，升级、卸载保留此目录；原图单独备份。

**升级前停止服务，备份整个资料目录。** SQLite 可能有 WAL/SHM 文件，不要运行时只复制数据库。Docker 同时备份 `.env`，沿用原挂载和项目名称。避免两个服务共用资料目录；迁移时保留服务内绝对路径与作品相对路径。

另一设备刷新并打开同一本即可续读，并发阅读以服务端最后保存为准。作品阅读方式和方向同步；主题、图片适配与性能偏好保存在当前浏览器。同步需要连接服务。

## 局域网访问与更新

Windows 在启动器勾选「允许局域网访问」后启动，其他设备打开显示的 `http://电脑局域网IP:端口`。Docker 使用宿主机 IP 和 `WEB_PORT`。设备需能互相访问，防火墙应允许相应端口；Windows 启动器不会自动修改防火墙，具体放行方式见 [Windows 文档](docs/windows.md#局域网访问)。

网页「设置 → 服务与数据 → 检查新版本」检查 GitHub 正式发行版并提供发行页链接，不会自动升级服务。Windows 启动器还可下载新版，经 SHA-256 校验后，由你确认停止服务并打开安装器。网络失败不影响现有阅读服务。Docker 用户备份后手动构建或替换镜像。

如使用 HTTPS 反向代理，请转发域名根路径，保留原始 `Host`（含非默认端口），设置 `X-Forwarded-Proto: https`。暂不支持部署在 `/manga/` 等子路径。

## 开发与验证

源码运行需要 Python 3.12+；Pillow 是图片处理依赖，Node.js 只用于前端测试。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node --test tests/test_reader_model.js tests/test_performance_settings.js tests/test_reader_end.js tests/test_avatar_ui.js
```

测试使用临时图片和独立资料目录，可用 `SUMI_TEST_TEMP` 指定临时父目录。安装包构建见 [Windows 文档](docs/windows.md#从源码构建安装包)，发布流程见 [Docker 文档](docs/docker-release.md#发布者配置-github-actions)。

## 许可

代码采用 [MIT License](LICENSE)。内嵌 Libre Caslon Text 字体采用独立的 [SIL Open Font License 1.1](static/fonts/OFL.txt)，依赖说明见 [第三方声明](desktop/THIRD_PARTY_NOTICES.md)。阅读交互参考了[拷贝漫画增强](https://greasyfork.org/zh-CN/scripts/421371-%E6%8B%B7%E8%B4%9D%E6%BC%AB%E7%94%BB%E5%A2%9E%E5%BC%BA)的使用体验，项目代码独立实现。
