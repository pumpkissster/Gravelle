# macOS 开发版

此分支提供 macOS 开发构建，尚未正式发版，版本号暂沿用 0.8.6。安装包由原生 Mac runner 生成，具体可用架构和检查结果见对应的构建报告。自动服务及界面检查不替代真实 Mac 上的 Finder 安装、完整点击操作、Retina 显示和外部磁盘访问验收。

## 获取开发测试包

打开仓库的 [Mac 开发构建](https://github.com/pumpkissster/Gravelle/actions/workflows/macos-build.yml)，选择 `dev/macos-preview` 分支的构建记录。在 Artifacts 中下载 `gravelle-macos-arm64-development`（M 系列）或 `gravelle-macos-x64-development`（Intel）；仅通过检查的架构会保留安装包。

解压下载的归档后，打开其中的 `.dmg`；另附的 `.zip` 是同一应用的备用格式，`SHA256SUMS.txt` 可核对两个文件。开发产物在 Actions 保留 14 天，不会改变正式 Release 或 Docker 镜像。默认开发包使用 ad-hoc 签名，未完成 Apple 公证。

## 安装与使用

原生安装包分别面向 Apple Silicon（`arm64`）和 Intel（`x64`）。首轮构建与验证边界为 macOS 15 或更新系统；更早系统的兼容性须另行验证。

DMG 包含 `Gravelle.app` 和 `Applications` 入口。将应用拖到“应用程序”后，从那里打开。ZIP 是同一应用的另一种分发形式，保留应用包的符号链接和文件权限，不是源码压缩包。

启动器配置写真根文件夹、端口和局域网访问，然后启动服务并打开浏览器。文件夹可以添加多个。启动器管理其自身创建的服务进程，不会结束占用同一端口的其他程序。关闭配置窗口后，服务可继续由菜单栏入口管理；退出应用时停止其服务进程。

账户、阅读记录、缓存等数据保存于当前用户的 `~/Library/Application Support/Gravelle`，独立于 `.app`，应用更新不会覆盖数据。配置文件为该目录下的 `launcher.json`，数据库为 `data/library.sqlite3`，优化副本位于 `data/transcoded`。原始写真图片仍保存在用户选择的文件夹内。

访问外部磁盘或受系统保护的文件夹时，由 macOS 提示相应访问权限；应根据实际需要授权。启用局域网访问后，其他设备使用启动器显示的局域网地址，并需要允许相应的本地网络及防火墙访问。端口可以绑定不代表另一台设备已验证连通。

## 在 Mac 上构建

使用与目标相同架构的 64 位 Python 3.12 或更新版本和 Xcode Command Line Tools。构建命令不会修改 `server.VERSION`，也不会上传 GitHub Release 或 Docker 镜像。

```sh
python3 -m venv work/macos-venv
source work/macos-venv/bin/activate
python -m pip install -r desktop/requirements-macos-build.txt
python desktop/build_macos.py --validate-config --arch arm64
python desktop/build_macos.py --arch arm64 --output-dir dist/macos-arm64
```

Intel Mac 使用 `--arch x64`。脚本拒绝在 Windows/Linux 上生成假安装包，也拒绝通过不匹配的 Python 架构交叉构建。实际构建在 `work/macos-build` 下创建独立临时构建目录，生成的应用路径记录在 `build-report.json`。

输出文件：

- `Gravelle-macOS-<版本>-arm64.dmg` 或 `...-x64.dmg`
- 同名 `.zip`
- `SHA256SUMS.txt`
- `build-report.json`，记录架构、最低系统、签名方式及是否完成公证

DMG 提供拖到“应用程序”的安装入口，应用内包含项目 LICENSE、所带运行时的许可证和第三方说明。图标来自用户提供的 PNG，启动器字标由用户的 SVG 在构建时生成；不依赖系统字体重绘。

## 签名与公证

未配置 Developer ID 时，PyInstaller 生成 ad-hoc 签名的开发测试包。它不具备可核验的开发者身份，也不等同于 Apple 已公证的正式安装包。Apple Silicon 上的 ad-hoc 签名是运行二进制所需的基础处理。正式对外分发前，应在 Mac 上验证 Developer ID Application 签名和 Apple 公证。

构建脚本可以读取已导入钥匙串的身份和已存储的 notarytool profile：

```sh
python desktop/build_macos.py --arch arm64 \
  --codesign-identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notary-profile gravelle
```

密码和私钥不作为脚本参数传递。脚本先提交并公证应用，staple 应用，再制作 ZIP 和 DMG；DMG 也单独签名、公证、staple。只有 Apple 返回 `Accepted` 并通过 ticket 检查后，报告才标记为已公证。

签名只为 PyObjC 的 Objective-C/Python 回调生成启用 `com.apple.security.cs.allow-jit`，依据 [PyObjC 官方签名说明](https://pyobjc.readthedocs.io/en/latest/notes/codesigning.html)。不启用关闭库验证或关闭可执行内存保护的广泛例外；实际签名包仍需执行原生界面 smoke 检查。

## 开发验证工作流

`.github/workflows/macos-build.yml` 通过手动 `workflow_dispatch` 或推送到指定开发分支 `dev/macos-preview` 运行，其他分支的推送不会触发。开发分支触发用于在不改动默认分支的情况下验证第一版工作流。它分别使用原生 `macos-15`（Apple Silicon）和 `macos-15-intel`，默认仅生成开发包，并将产物保存为 GitHub Actions artifacts 14 天。工作流没有发布 Release 或推送镜像的权限。

选择签名和公证时需预先配置这些 GitHub Actions secrets：`APPLE_CERTIFICATE_P12_BASE64`、`APPLE_CERTIFICATE_PASSWORD`、`APPLE_CODESIGN_IDENTITY`、`APPLE_ID`、`APPLE_TEAM_ID`、`APPLE_APP_SPECIFIC_PASSWORD`。只在签名步骤使用这些值；临时证书以受限权限存储，临时钥匙串及证书在任务结束时清理。不要将任何密码、令牌或 `.p12` 文件提交到源码。

工作流运行冻结服务与图片 worker 的 smoke 检查，验证架构及校验和。原生界面 smoke 若检测到 runner 没有图形会话，会以退出码 77 和 `status: unsupported` 生成报告，明确发出 warning；此时保留开发包供真机测试，不能视为界面验证通过。普通非零退出码仍使构建失败。仍需真实 Mac 上检查 Finder 启动、Retina 清晰度、文件夹选择、菜单栏、外部磁盘访问和局域网连接。自动更新会检查 GitHub Releases 并提供匹配架构的 DMG，暂不静默覆盖正在运行的应用。

构建与分发依据：[GitHub runner 架构表](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[PyInstaller macOS 打包及签名选项](https://www.pyinstaller.org/en/stable/usage.html#macos-specific-options)、[PyObjC 支持的平台](https://pyobjc.readthedocs.io/en/latest/supported-platforms.html)、[Apple 公证流程](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)。
