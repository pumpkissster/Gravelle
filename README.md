# Gravelle

Gravelle 是属于你自己的写真库阅读器，支持 Windows、macOS 和 Docker。电脑、手机和平板通过浏览器阅读，同一账号共用进度、书签和收藏。

当前版本 **0.9.1** · [下载与更新说明](https://github.com/pumpkissster/Gravelle/releases/latest)

## 主要功能

- 读取图片文件夹、ZIP／CBZ、PDF 和图片型 EPUB，支持单页、双页、连续滚动及单页上下翻页。
- 多个写真库与多个根文件夹，支持搜索、模特关联、收藏及批量整理。
- 管理员与子账号、写真库访问授权，各账号独立保存阅读记录和库的显示偏好。
- 按规则优化大图，各设备共用优化副本，保留原图。
- 在自己的写真库中以图搜图，支持完整截图和轻中度裁剪匹配。
- 管理员创建带密码的单本分享，有效期可选 1／3／7 天，在设置中管理链接与查看访问统计。
- 网络设置支持安全入口、独立分享端口和公网分享地址。详见 [网络设置说明](docs/network.md)。
- 图片索引失败列表、写真库合成封面和缓存管理。

EPUB 面向图片写真集，不支持纯文字书或 DRM；PDF 不支持加密文档。PDF、EPUB 暂不参加图片优化和以图搜图。扫描、编辑资料和移除作品不会删除或改写原文件。

## 下载与安装

从 [GitHub Releases](https://github.com/pumpkissster/Gravelle/releases/latest) 选择对应平台：

| 平台 | 安装方式 |
| --- | --- |
| Windows 10（1809+）／11 x64 | 下载安装程序，或解压便携 ZIP 后运行 `Gravelle/Gravelle.exe`。详见 [Windows 使用说明](docs/windows.md)。 |
| macOS 15 及以上 | Apple 芯片选 `arm64`，Intel 选 `x64`；打开 DMG，将 Gravelle 拖入“应用程序”。详见 [Mac 使用说明](docs/macos.md)。 |
| Docker | 拉取 [Docker Hub 镜像](https://hub.docker.com/r/pumpkissster/gravelle)，支持 amd64 和 arm64。详见 [部署说明](docs/docker-release.md)。 |

桌面版打开启动器后，添加写真根文件夹，设置端口并启动阅读器；浏览器会自动打开。首次访问注册管理员账号，然后创建写真库。子账号由管理员在设置中创建。

需要手机或平板访问时，开启局域网访问，使用启动器显示的局域网地址；Docker 使用宿主机 IP 和映射端口。

Windows 安装程序未做代码签名，macOS 包未完成 Apple 公证，系统可能显示安全提示。请从项目发行页下载，并核对 `SHA256SUMS.txt`。

## Docker 快速部署

可将以下内容保存为 `compose.yaml`，将 `/path/to/...` 替换为实际宿主机目录。默认访问端口为 8080，可自行修改。群晖用户也可直接填入 Container Manager 的“项目”配置。

```yaml
services:
  gravelle:
    image: pumpkissster/gravelle:latest  # 使用最新版本镜像
    container_name: gravelle            # 容器名称
    user: "0:0"                        # 运行用户 UID:GID，按目录权限调整
    ports:
      - "8080:8080"                    # 左侧为宿主机访问端口，可自行修改；右侧保持 8080
      # 可选：独立分享端口；需要时取消下一行注释，并在网页中开启独立分享
      # - "8081:8081"                  # 左侧宿主机端口可改；右侧容器端口固定为 8081
    restart: unless-stopped            # 除非手动停止，否则自动重启
    environment:
      SCAN_INTERVAL: "300"             # 定时扫描间隔，单位秒；设为 0 关闭定时扫描
    volumes:
      # 资料目录：保存账号、作品信息、阅读记录及缓存
      - "/path/to/data:/data"
      # 写真目录：只读挂载，不会修改原始文件；可继续添加 /library 的其他子目录
      - "/path/to/photos:/library/photos:ro"
      # 可选：把图片优化副本单独保存到其他文件夹或磁盘
      # 不启用时，副本默认保存在上方资料目录的 transcoded 子目录
      # 启用时取消下一行开头的 #，并修改左侧宿主机路径
      # - "/path/to/transcoded:/data/transcoded"
```

创建对应的宿主机文件夹后，在配置文件所在目录执行：

```sh
docker compose pull
docker compose up -d
```

镜像可选 `pumpkissster/gravelle:latest`。写真目录只读映射到 `/library` 或 `/library` 的子目录，资料目录映射到 `/data`；网页创建库时选择 `/library` 或其子目录。

按默认端口配置，打开 `http://服务器IP:8080`；修改了左侧端口时使用对应端口。网页创建库时选择 `/library/photos`。也可使用发行页的 Docker 配置包；其他路径与权限设置见 [Docker／NAS 部署说明](docs/docker-release.md)。

需要独立分享时，取消上方 `8081:8081` 映射的注释，重新创建容器，再到“设置 → 网络设置”开启“独立分享端口”。仅增加映射不会自动开启分享服务。左侧为宿主机分享端口，可自行修改；右侧容器端口保持 `8081`。公网分享地址填写访客实际使用的域名与端口，详见 [网络设置](docs/network.md#独立分享端口)。

图片优化副本默认位于 `/data/transcoded`。如果已有优化副本，启用独立映射前先按 [迁移说明](docs/docker-release.md#图片优化目录单独映射) 移动已有副本，再切换挂载。

## 资料、备份与更新

| 平台 | 资料位置 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Gravelle` |
| macOS | `~/Library/Application Support/Gravelle` |
| Docker | 容器 `/data` 对应的宿主机目录 |

升级前停止服务，备份完整资料目录，并沿用原作品路径和挂载。资料包含账号、作品信息、进度、书签、封面及分享密码密钥；原始写真文件需另行备份。

网页设置可检查新版本；桌面启动器可检查并下载对应平台安装包，由用户确认安装。Docker 用户备份后拉取新镜像并重建容器。

分享链接需要访客能连接服务地址；本机 `127.0.0.1` 链接不能供另一台设备使用。图片加密不能防截图，也不能代替 HTTPS；公网访问应使用受信任的 HTTPS 入口。

详细操作：[用户与权限](docs/users.md) · [PDF](docs/pdf.md) · [EPUB](docs/epub.md) · [单本分享](docs/sharing.md) · [图片优化](docs/transcoding.md) · [以图搜图](docs/image-search.md) · [缓存管理](docs/cache.md) · [性能设置](docs/performance.md)

## 许可

项目采用 [MIT License](LICENSE)，字体及依赖许可见 [字体授权](static/fonts/OFL.txt) 与 [第三方声明](desktop/THIRD_PARTY_NOTICES.md)。阅读交互参考了[拷贝漫画增强](https://greasyfork.org/zh-CN/scripts/421371-%E6%8B%B7%E8%B4%9D%E6%BC%AB%E7%94%BB%E5%A2%9E%E5%BC%BA)的使用体验，项目代码独立实现。
