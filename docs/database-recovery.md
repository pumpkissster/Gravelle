# 数据库升级备份与恢复

Gravelle 首次打开需要升级结构的旧资料库时，会先在资料目录的 `backups` 子目录保存一份 SQLite 快照，文件名以 `before-schema-upgrade-` 开头。快照包含 SQLite 日志里已经提交的记录。升级成功后的常规启动无需重复备份。

升级失败时，启动器或服务日志会显示快照路径及失败原因。重新尝试同一次升级时会沿用原快照。需要回到旧版时，请先停止阅读器，再用下方命令恢复数据库。将示例中的 `BACKUP_FILENAME.sqlite3` 换成日志里显示的备份**文件名**。

恢复会用快照中的状态替换当前数据库，并将恢复前的数据库另外保存为 `before-database-restore-` 快照。在工具报告恢复成功后，再使用创建快照时对应的旧版启动。原有的作品路径与资料目录继续沿用。

## Windows

在 PowerShell 里运行。便携版或自定义安装请将程序路径改为实际安装目录下的 `runtime\GravelleServer.exe`。

```powershell
$server = Join-Path $env:LOCALAPPDATA 'Programs\Gravelle\runtime\GravelleServer.exe'
$data = Join-Path $env:LOCALAPPDATA 'Gravelle\data'
& $server --restore-database-backup --data $data --backup 'BACKUP_FILENAME.sqlite3'
```

## macOS

在「终端」运行本次升级使用的应用程序。如果应用安装在其他目录，请替换命令中的应用路径。

```sh
/Applications/Gravelle.app/Contents/MacOS/Gravelle --restore-database-backup \
  --data "$HOME/Library/Application Support/Gravelle/data" \
  --backup BACKUP_FILENAME.sqlite3
```

## Docker

在 Compose 配置所在目录操作，沿用本次升级的镜像、资料挂载和运行用户。下面使用默认服务名 `gravelle`。如果部署时使用了其他服务名或 Compose 覆盖文件，请沿用原来的配置。

```sh
docker compose stop gravelle
docker compose run --rm --no-deps gravelle python server.py --restore-database-backup \
  --data /data --backup BACKUP_FILENAME.sqlite3
```

恢复成功后，将 Compose 镜像版本切回原版本，再运行 `docker compose up -d gravelle`。

数据库快照包含账号、作品资料、书签、阅读进度和分享记录。封面、优化副本、证书及分享密码密钥等文件由资料目录单独保存。升级前仍请备份完整资料目录，并保留原始媒体。
