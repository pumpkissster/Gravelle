# Gravelle Docker 安装与发版

支持 `linux/amd64` 和 `linux/arm64`。容器以普通用户运行，作品目录只读，资料保存到独立的数据目录。

## 从 Docker Hub 安装

镜像仓库为 [pumpkissster/gravelle](https://hub.docker.com/r/pumpkissster/gravelle)，正式安装使用固定版本 `pumpkissster/gravelle:1.8.2`。Docker 会按宿主机选择 amd64 或 arm64 镜像。

1. 从 [GitHub Release](https://github.com/pumpkissster/gravelle/releases/tag/v1.8.2) 下载 `Gravelle-Docker-1.8.2.zip`，解压到固定的项目目录。
2. 将 `.env.example` 复制为 `.env`，填写以下项目。如果已有配置包中的镜像地址为空，手动填写下列固定版本即可：

| 配置 | 内容 |
| --- | --- |
| `GRAVELLE_IMAGE` | `pumpkissster/gravelle:1.8.2` |
| `COMICS_PATH` | 宿主机写真根目录，例如 `/volume1/photosets` |
| `DATA_PATH` | 宿主机资料目录，模板为 `/volume1/docker/sumi-reader/data`；已有安装保留原值 |
| `PUID` / `PGID` | 对作品目录有读取权限、对资料目录有读写权限的用户 UID / GID |
| `WEB_PORT` | 外部访问端口，默认 `8080` |

3. 提前创建两个目录，确认运行用户能读取写真、写入资料。
4. 在项目目录运行：

```sh
docker compose pull
docker compose up -d
docker compose ps
```

浏览器打开 `http://服务器局域网IP:8080`；若更改了 `WEB_PORT`，使用相应端口。创建账户后，在网页创建写真库，选择容器内的 `/library` 或其子目录。局域网访问需要宿主机防火墙允许该端口。

群晖可使用 Container Manager 的“项目”功能，在同一项目目录导入 `compose.yaml` 和 `.env`。`PUID`、`PGID` 可通过 SSH 执行 `id 用户名` 查询；不要随意把资料目录改成所有人可写。

作品目录只读挂载到 `/library`，资料目录可写挂载到 `/data`。宿主机路径与网页容器路径不同，例如 `/volume1/photosets/模特甲` 在网页中选择 `/library/模特甲`。启动前确认目录已存在，避免错误路径被创建为空目录。公开镜像的安装不需要自行创建 Docker Hub 仓库或配置推送令牌。

一个宿主机根目录下可以存放多个写真库。若作品分散在不同磁盘，给 Compose 增加只读挂载到 `/library/目录名`，再在网页选择对应容器路径。

## 从源码构建

下载完整源码包后，在源码目录填写 `.env` 的目录和权限配置，执行：

```sh
docker compose -f compose.build.yaml up -d --build
```

此方式不需要 Docker Hub 账号，也不需要设置 `GRAVELLE_IMAGE`。`GRAVELLE_RELEASE_REPOSITORY` 仅在源码构建时指定更新检查仓库，默认为 `pumpkissster/gravelle`。正式镜像内已内置发布仓库；如需要自行维护分支，在 Compose 的 `environment` 中明确添加非空的 `GRAVELLE_RELEASE_REPOSITORY: 你的账号/仓库`。

## 更新和旧版迁移

先停止容器并备份完整的 `DATA_PATH` 目录，然后将 `.env` 中的 `GRAVELLE_IMAGE` 换成新版本，再执行 `docker compose pull` 和 `docker compose up -d`。原始作品、账户、书签与阅读记录保存在挂载目录中，不随容器替换而删除。设置页的“检查新版本”会提示新版，容器更新由宿主机完成。

旧版的 Compose 服务名 `sumi-reader`、`COMICS_PATH` 变量和已有 `/data` 布局继续保留。升级时使用原来的项目目录或项目名称，保留原 `DATA_PATH`、`COMICS_PATH` 与运行用户配置，再替换 Compose 文件；不要创建第二个项目同时使用同一资料目录。历史资料目录名含 `sumi-reader` 无需改名。

如果升级后的数据库结构发生变化，回退时恢复升级前的完整资料备份，并使用对应旧镜像版本。

## 发布者配置 GitHub Actions

将代码上传到公开 GitHub 仓库，更新检查和 Windows 启动器使用同一仓库的 Releases。工作流从当前仓库名称注入更新地址，不需要把访问令牌放进程序。

在仓库 Settings → Secrets and variables → Actions 配置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Variable | `DOCKERHUB_IMAGE` | `pumpkissster/gravelle`，不带版本号；维护分支改成自己的仓库 |
| Variable | `DOCKERHUB_USERNAME` | 对目标仓库有推送权限的 Docker ID（用户名） |
| Secret | `DOCKERHUB_TOKEN` | 具备该镜像推送权限的 Docker Hub access token |

Docker 支持使用 GitHub 账户注册或登录，但仍需确认自己的 Docker ID，并在 Docker Hub 创建对应的公开镜像仓库；GitHub 仓库不会自动变成 Docker Hub 镜像仓库。CI 使用 Docker ID 和 Docker Hub access token，不使用 GitHub 密码或令牌。[Docker 账户说明](https://docs.docker.com/accounts/individual/create-account/)

令牌只填 GitHub Secret，不写入代码或 `.env`。可在 Settings → Environments 中为 `release` 环境限制默认分支并添加发布审核人。

在 GitHub 的 Actions → **Build and release Gravelle** → Run workflow 中填写与 `server.py` 的 `VERSION` 完全一致的版本号：

- **publish 不勾选**：执行前后端测试，构建 Windows 安装包和便携包，构建、启动验证 amd64 与 arm64 Docker 镜像，保存在本次运行的 Artifacts；不会发布 Release 或推送镜像，尚未注册 Docker Hub 也可使用。
- **publish 勾选、publish_docker 不勾选**：独立发布 Windows 版本。测试与构建全部通过后，创建 GitHub Release 草稿，上传安装包、便携包、Docker 配置包、源码包及 SHA-256 校验值，再公开 Release。发行说明明确注明 Docker Hub 镜像尚未发布，不需要 Docker Hub 账号。
- **publish 和 publish_docker 都勾选**：要求 Docker Hub 配置完整，上传草稿资产并推送双架构版本镜像，全部成功后才公开 Release，再更新镜像 `latest`。

首次发版将 **source_tag 留空**。如果先发布了 Windows，之后补发同版本 Docker 镜像，再次运行默认分支的工作流，填写相同版本号、将 **source_tag 设为原标签（例如 `v1.8.2`）**，并勾选 publish 和 publish_docker。构建使用该标签的原始代码；已公开的安装器、便携包、源码包、Docker 配置包和校验文件五项资产保持不变，仅补发镜像并更新发行说明。若该版本已经不是 GitHub 最新版，不会把 Docker `latest` 指回旧版。

正式发布的工作流仅允许从默认分支运行；source_tag 必须是与版本号对应的既有稳定标签。发布步骤失败时不会提前更新 Docker `latest`。已有正式 Windows 资产不会被覆盖；修改程序应提升版本号，已创建的同一提交草稿可重试。版本镜像推送成功但 Release 公开失败时，该版本镜像可能已存在，草稿仍保留供排查。Release 已公开但最后的 `latest` 更新失败时，固定版本安装仍可用，确认版本镜像后再手动更新 `latest`。

源码包按 `packaging/source_manifest.json` 中审核过的文件清单生成，排除本地写真、账户资料和测试记录。`packaging/package_source.py` 同时生成 Docker 配置包，并对同一发行目录内的 Windows 与 ZIP 资产生成统一的 `SHA256SUMS.txt`。

Docker 验证 Artifacts 为 OCI 归档，仅用于构建核查，正常安装使用 Docker Hub 镜像。Windows 安装包支持 Windows x64；Windows ARM64 与 macOS 安装包不在本次发布范围。

本地尚无 Docker 环境时，只能检查发布配置；镜像实际构建、双架构启动与安装包生成结果以 GitHub Actions 成功记录为准。

## 官方参考

- [Docker：GitHub Actions 多架构构建](https://docs.docker.com/build/ci/github-actions/multi-platform/)
- [Docker：先测试再推送](https://docs.docker.com/build/ci/github-actions/test-before-push/)
- [Docker Compose 变量插值](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
- [Docker Hub：创建镜像仓库](https://docs.docker.com/docker-hub/repos/create/)
- [Docker：访问令牌](https://docs.docker.com/security/access-tokens/)
- [GitHub：手动触发工作流](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
- [GitHub CLI：创建 Release 草稿](https://cli.github.com/manual/gh_release_create)
