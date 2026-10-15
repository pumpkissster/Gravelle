# Gravelle Docker 安装与发版

支持 `linux/amd64` 和 `linux/arm64`。容器以普通用户运行，作品目录只读，资料保存到独立的数据目录。

## 从 Docker Hub 安装

镜像仓库为 [pumpkissster/gravelle](https://hub.docker.com/r/pumpkissster/gravelle)，当前版本为 `0.8.6`，也提供 `latest` 标签。Docker 会按宿主机选择 amd64 或 arm64 镜像。

可从 [GitHub Releases](https://github.com/pumpkissster/Gravelle/releases/latest) 下载 `Gravelle-Docker-0.8.6.zip` 配置包，更新内容见 [0.8.6 发行页](https://github.com/pumpkissster/Gravelle/releases/tag/v0.8.6)。

1. 将仓库中的 [compose.yaml](../compose.yaml) 和 [.env.example](../.env.example) 保存到同一固定项目目录，或继续使用已下载的配置包。
2. 将 `.env.example` 复制为 `.env`，填写以下项目：

| 配置 | 内容 |
| --- | --- |
| `GRAVELLE_IMAGE` | `pumpkissster/gravelle:0.8.6` |
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

获取完整源码后，在源码目录填写 `.env` 的目录和权限配置，执行：

```sh
docker compose -f compose.build.yaml up -d --build
```

此方式不需要 Docker Hub 账号，也不需要设置 `GRAVELLE_IMAGE`。`GRAVELLE_RELEASE_REPOSITORY` 仅在源码构建时指定更新检查仓库，默认为 `pumpkissster/Gravelle`。正式镜像内已内置发布仓库；如需要自行维护分支，在 Compose 的 `environment` 中明确添加非空的 `GRAVELLE_RELEASE_REPOSITORY: 你的账号/仓库`。

## 图片优化目录单独映射

`0.8.5` 支持管理员图片优化，默认关闭，由管理员在「设置 → 图片优化」中启用。仅命中规则的页面生成优化副本，原图保持不变。以图搜图的图片索引也默认关闭，索引数据随 `/data` 保存，不写入优化目录。

默认优化副本保存在容器 `/data/transcoded`，对应宿主机的 `DATA_PATH/transcoded`，与数据库、账号资料一起持久保存；无需单独映射。这种布局可以正常使用，没有必须拆分到另一目录的性能要求。单独映射只用于自主选择其他硬盘或文件夹、安排容量及备份，不改变原始写真目录，也不会自动迁移现有文件。

在 `.env` 中填写已经创建的独立目录，例如：

```dotenv
OPTIMIZED_PATH=/volume2/gravelle/optimized
```

**仅填写 `OPTIMIZED_PATH` 不会启用独立存储。** 将 [compose.optimization.yaml](../compose.optimization.yaml) 放在现有 Compose 项目目录，通过两个 `-f` 同时加载。容器仍使用 `/data/transcoded`，覆盖配置只增加这个子目录的可读写绑定挂载，保留 `/library` 的只读挂载及 `/data` 的原数据挂载。Compose 根据挂载的 `target` 合并条目，新增子目录不会替换 `/data`。[Docker Compose 合并规则](https://docs.docker.com/reference/compose-file/merge/#unique-resources)

源码构建使用：

```sh
docker compose -f compose.build.yaml -f compose.optimization.yaml config
docker compose -f compose.build.yaml -f compose.optimization.yaml up -d --build
docker compose -f compose.build.yaml -f compose.optimization.yaml ps
```

使用 Docker Hub 镜像时，将 `.env` 的 `GRAVELLE_IMAGE` 设为 `pumpkissster/gravelle:0.8.6`，再使用：

```sh
docker compose -f compose.yaml -f compose.optimization.yaml config
docker compose -f compose.yaml -f compose.optimization.yaml pull
docker compose -f compose.yaml -f compose.optimization.yaml up -d
docker compose -f compose.yaml -f compose.optimization.yaml ps
```

以后停止、更新和查看此项目时，继续使用同一组 `-f` 参数及原项目名称。群晖 Container Manager 用户可在项目的 Compose 编辑器中，将覆盖文件里的 `/data/transcoded` 挂载条目合入原服务的 `volumes` 列表，并填写相同的 `OPTIMIZED_PATH`；也可手动将选定宿主机目录可读写映射到容器 `/data/transcoded`。不要再创建一个服务共用同一资料目录。

已有优化副本而希望启用单独映射时，按以下顺序切换；从未启用图片优化的安装没有优化副本，无需复制该目录：

1. 使用当前部署配置停止服务，备份完整的 `DATA_PATH`。暂停后台任务后仍需停止服务，防止复制期间写入。
2. 创建独立目标目录，将旧 `DATA_PATH/transcoded/` **里面的全部内容**复制进去，保留作品 ID、子目录与文件相对结构；不要额外嵌套一层 `transcoded`。保留旧副本，核对文件数量、大小及校验值。
3. 确认 `PUID`／`PGID` 对新目录具有读取、写入和目录访问权限。新目录不能放在原始作品的只读目录中，也不要与 `DATA_PATH` 根目录使用同一路径。
4. 填写 `OPTIMIZED_PATH`，用带覆盖文件的 `config` 检查最终配置，再启动原项目。确认已完成的优化作品可读取、新任务可写入新目录后，再决定如何保留旧备份。

绑定挂载会遮住容器该路径原有的内容；把空目录直接挂到 `/data/transcoded` 会使已有数据库映射找不到副本。覆盖配置要求变量非空，并设置 `create_host_path: false`，目录不存在时启动会失败，避免错拼路径自动创建空目录。[Docker 绑定挂载](https://docs.docker.com/engine/storage/bind-mounts/#bind-mounting-over-existing-data)、[Compose 挂载选项](https://docs.docker.com/reference/compose-file/services/#long-syntax-5)

数据库、账号、阅读记录和优化映射仍保存在 `DATA_PATH`；独立优化目录必须持久保存且可读写，不能使用临时目录。分离后备份与恢复应协调保存两处目录。优化副本丢失时需要管理员重新优化，不能将其当作可随时删除的浏览器缓存；原图保持不变。

## 更新与数据保留

先停止容器并备份完整的 `DATA_PATH` 目录；若单独映射了优化目录，一并保存 `OPTIMIZED_PATH`。然后将 `.env` 中的 `GRAVELLE_IMAGE` 换成新版本，再执行 `docker compose pull` 和 `docker compose up -d`。启用单独映射的项目继续使用前述两份 `-f` 配置。原始作品、账户、书签与阅读记录保存在挂载目录中，不随容器替换而删除。设置页的“检查新版本”会提示新版，容器更新由宿主机完成。

数据升级按数据库结构执行，已有主账号迁移为管理员并保留个人记录。更新时保留原数据挂载。

旧图片搜索索引升级后自动在后台逐页补充截图／裁剪匹配特征，无需手动重建，期间仍可使用已收录页面的整图搜索。图片优化与图片索引默认共用一个处理名额；管理员可在性能设置改为 1～8，并发越高资源占用越大。

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

首次发版将 **source_tag 留空**。如果先发布了 Windows，之后补发同版本 Docker 镜像，再次运行默认分支的工作流，填写相同版本号、将 **source_tag 设为原标签（例如 `v0.8.6`）**，并勾选 publish 和 publish_docker。构建使用该标签的原始代码；已公开的安装器、便携包、源码包、Docker 配置包和校验文件五项资产保持不变，仅补发镜像并更新发行说明。若该版本已经不是 GitHub 最新版，不会把 Docker `latest` 指回旧版。

正式发布的工作流仅允许从默认分支运行；source_tag 必须是与版本号对应的既有稳定标签。发布步骤失败时不会提前更新 Docker `latest`。已有正式 Windows 资产不会被覆盖；修改程序应提升版本号，已创建的同一提交草稿可重试。版本镜像推送成功但 Release 公开失败时，该版本镜像可能已存在，草稿仍保留供排查。Release 已公开但最后的 `latest` 更新失败时，固定版本安装仍可用，确认版本镜像后再手动更新 `latest`。

源码包按 `packaging/source_manifest.json` 中审核过的文件清单生成，排除本地写真、账户资料、测试记录和内部开发计划。`packaging/package_source.py` 同时生成 Docker 配置包，并对同一发行目录内的 Windows 与 ZIP 资产生成统一的 `SHA256SUMS.txt`。

发行说明从 `docs/releases/版本号.md` 追加对应更新内容，并保留按本次发布选项生成的 Docker 镜像说明。GitHub Release 显式标记为最新版本。

### 发行文案约定

发行说明只包含当前版本实际完成的功能与修复、安装／升级说明和会影响使用的当前限制。不要写入后续更新计划、功能预告、待办清单或预计交付时间；公开 README、Docker Hub 说明及随包文档也遵循这条约定。

内部计划只在本地保存，不进入公开发布提交或源码包。准备发布提交时同时核对 Git 跟踪的文件；`.gitignore` 不会自动移除已跟踪的文件，若历史提交已包含内部文档，应在新的发布提交中移除它，而不是再次上传或添加公开链接。工作流在正式发布时检查内部计划文件未被跟踪，避免 GitHub 自动生成的源码归档带出计划；发行说明只使用审核过的说明文件，不追加自动生成的提交／PR 摘要。发布前人工检查最终生成的 `release-notes.md` 及随包文档，避免把开发记录拼入发行说明。已实现功能的使用说明和当前限制可以正常保留，无需承诺后续改动。

Docker 验证 Artifacts 为 OCI 归档，仅用于构建核查，正常安装使用 Docker Hub 镜像。Windows 安装包支持 Windows x64；Windows ARM64 与 macOS 安装包不在本次发布范围。

本地尚无 Docker 环境时，只能检查发布配置；镜像实际构建、双架构启动与安装包生成结果以 GitHub Actions 成功记录为准。

## 官方参考

- [Docker：GitHub Actions 多架构构建](https://docs.docker.com/build/ci/github-actions/multi-platform/)
- [Docker：先测试再推送](https://docs.docker.com/build/ci/github-actions/test-before-push/)
- [Docker Compose 变量插值](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
- [Docker Compose 多文件挂载合并](https://docs.docker.com/reference/compose-file/merge/#unique-resources)
- [Docker：绑定挂载与已有内容遮蔽](https://docs.docker.com/engine/storage/bind-mounts/#bind-mounting-over-existing-data)
- [Docker Compose 挂载长格式](https://docs.docker.com/reference/compose-file/services/#long-syntax-5)
- [Docker Hub：创建镜像仓库](https://docs.docker.com/docker-hub/repos/create/)
- [Docker：访问令牌](https://docs.docker.com/security/access-tokens/)
- [GitHub：手动触发工作流](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
- [GitHub CLI：创建 Release 草稿](https://cli.github.com/manual/gh_release_create)
