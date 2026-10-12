# Gravelle Windows 安装版

支持 Windows 10（1809 或更新）／Windows 11 x64。安装包自带 Python、图片处理依赖与网页程序，不需要 Docker 或手动安装 Python。

安装程序提供简体中文和 English，按 Windows 系统界面语言自动选择。升级沿用原账户与资料目录。

## 安装与启动

当前版本为 0.8.4，支持安装版和便携版；更新内容见 [0.8.4 更新说明](releases/0.8.4.md)。

1. 从 [GitHub Releases](https://github.com/pumpkissster/Gravelle/releases/latest) 下载 `Gravelle-Setup-0.8.4-x64.exe` 并安装。默认仅安装给当前 Windows 用户。
2. 打开“Gravelle”，选择写真根文件夹，使用“添加文件夹”继续添加其他目录。网页可从这些根文件夹内创建多个写真库。
3. 设置端口（默认 8080）；需要手机、平板访问时，勾选“允许局域网访问”。
4. 点击“检查目录与网络”，确认目录可读、端口未被占用。
5. 点击“启动阅读器”。服务准备好后会自动打开浏览器，启动器最小化到任务栏。首次访问注册管理员账户，再在网页创建写真库；子账号由管理员在设置中创建。

恢复任务栏窗口可查看访问地址、重新打开网页或停止服务。关闭启动器会提示并停止它创建的阅读服务；只关闭浏览器不会停止服务。程序不会结束其他软件占用端口的进程，端口冲突时可改用 8081 等端口。

便携包 `Gravelle-Windows-0.8.4-x64.zip` 解压后运行 `Gravelle/Gravelle.exe`，整个文件夹需要一起保留，不要单独移动 exe。便携版同样使用当前用户的资料目录。

## 多个写真根文件夹

启动器可保存 1 至 20 个根文件夹，支持不同磁盘、中文路径和逐项移除；重复路径只保留一项。“检查目录与网络”及启动前会检查每个目录是否存在且可读。旧版单目录配置会自动兼容，无需重建写真库。

网页创建或编辑写真库时，点击目录浏览按钮，通过“根文件夹”选择器切换到其他根目录，再选择具体目录。根文件夹只有一个时不显示此选择器。一个写真库仍可添加多个目录。

更改启动器目录前先停止阅读器，再次启动后生效。移除根文件夹只收回该目录的读取范围，不会删除源文件、写真库资料或阅读记录；相关作品暂不可读取，重新加入原路径后可恢复。不要通过移除根文件夹来代替网页的移除作品操作。

## 资料与更新

配置保存在 `%LOCALAPPDATA%\Gravelle\launcher.json`，账户、封面与阅读记录在 `%LOCALAPPDATA%\Gravelle\data`，服务日志在 `%LOCALAPPDATA%\Gravelle\logs`。程序升级和卸载保留这些资料，原始写真始终从所选文件夹读取。

启动器打开时异步检查公开 GitHub Releases。发现新版后可以下载，下载和安装前均校验 `SHA256SUMS.txt` 中的 SHA-256；只有用户点击安装并确认后，才会停止自己启动的服务并打开安装程序。离线或 GitHub 暂不可用时仍能启动阅读器。网页设置中的“检查新版本”只检查和提示，Windows 更新由启动器完成。

更新按钮位于窗口顶部版本号右侧，根据状态显示“检查更新”“下载更新”或“安装更新”。启动器采用网页同款粉色、圆角按钮与浅色卡片，次要按钮有独立填色和边框，禁用操作以灰色显示。顶部更新与底部启动操作保持固定，中部多目录与服务信息可以滚动。

版本系列从 1.8.x 调整为 0.8.x。已安装 1.8.x 的用户需要手动下载 0.8.4 覆盖安装一次：先停止旧服务并关闭旧启动器，备份整个 `%LOCALAPPDATA%\Gravelle`，然后运行新安装包并沿用安装位置和资料目录。无需卸载或删除账户资料。旧版自动更新按版本数值比较，不会把 0.8.4 当成高于 1.8.x 的版本；完成这次手动更新后，后续 0.8.x 正常检查升级。便携版用户保留数据目录，使用完整的新便携文件夹运行。

升级前先在启动器停止服务，备份整个 `%LOCALAPPDATA%\Gravelle` 文件夹。开发测试版原数据在项目 `data-windows` 时，也先停止旧服务；可将该目录内的完整内容复制到安装版的 `data` 目录再启动。保留原写真路径；移动原始写真目录不会自动迁移作品路径或阅读记录。不要让两个服务同时打开同一数据目录。

## 局域网访问

手机与电脑连接同一路由器，用启动器显示的 `http://电脑局域网IP:端口/` 访问，使用同一账户登录。电脑需开机且阅读器保持运行。

“检查目录与网络”能够确认本机绑定和连接情况，无法单靠本机检测证明其他设备已能穿过防火墙。启动器不会自动修改 Windows 防火墙；如被阻止，可由管理员仅放行本地子网、服务程序和所选端口，例如默认安装目录与端口为：

```powershell
$gravelleServerPath = Join-Path $env:LOCALAPPDATA 'Programs\Gravelle\GravelleServer.exe'
New-NetFirewallRule -Name 'Gravelle-LAN-8080' -DisplayName 'Gravelle LAN (TCP 8080)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080 -RemoteAddress LocalSubnet -Program $gravelleServerPath -Profile Any
```

更改安装位置或端口时，相应修改规则。撤销规则：`Remove-NetFirewallRule -Name 'Gravelle-LAN-8080'`。不需要关闭整个防火墙。

## 从源码构建安装包

在 Windows x64 安装 Python 3.12 或更新版本，以及官方 [Inno Setup](https://jrsoftware.org/isdl.php)，在项目目录运行：

```powershell
./desktop/build_windows.ps1 -Repository pumpkissster/Gravelle
```

脚本在项目 `work/windows-build/venv` 创建隔离环境，输出 `dist/windows/Gravelle-Setup-版本号-x64.exe`、便携 zip 和 `SHA256SUMS.txt`。若编译器不在默认位置，可以用 `-InnoCompiler 完整路径` 指定。`-PortableOnly` 仅构建便携测试版。

打包后的自动验证：

```powershell
python desktop/smoke_windows.py --executable dist/windows/portable/Gravelle/GravelleServer.exe --work-root work/windows-smoke
```

此验证使用随机端口和两个独立临时图片目录，检查注册登录、多根目录、扫描、原图、封面、批量模特关联、数据库与停止流程，不读取真实资料。
