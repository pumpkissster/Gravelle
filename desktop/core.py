"""Configuration, local diagnostics and trusted release discovery."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class LauncherError(Exception):
    """A concise error suitable for the launcher's status area."""


def user_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "Gravelle"


@dataclass(frozen=True)
class LauncherConfig:
    library_path: str = str(Path.home() / "Pictures")
    port: int = 8080
    lan_enabled: bool = False

    @property
    def host(self) -> str:
        return "0.0.0.0" if self.lan_enabled else "127.0.0.1"

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def validate(self, require_directory: bool = True) -> "LauncherConfig":
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise LauncherError("端口需要是 1 至 65535 之间的整数。")
        if not isinstance(self.lan_enabled, bool):
            raise LauncherError("局域网访问设置无效。")
        if not isinstance(self.library_path, str) or not self.library_path.strip():
            raise LauncherError("请选择写真文件夹。")
        path = Path(self.library_path).expanduser()
        if not path.is_absolute():
            raise LauncherError("写真文件夹需要使用完整路径。")
        if require_directory:
            try:
                if not path.is_dir():
                    raise LauncherError("写真文件夹不存在。请选择可访问的文件夹。")
                with os.scandir(path) as entries:
                    next(entries, None)
            except OSError as error:
                raise LauncherError("无法读取写真文件夹，请检查访问权限。") from error
        return self


def load_config(path: Path) -> LauncherConfig:
    if not path.exists():
        return LauncherConfig()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return LauncherConfig(**{key: value[key] for key in asdict(LauncherConfig()) if key in value}).validate(False)
    except (OSError, ValueError, TypeError, LauncherError) as error:
        raise LauncherError(f"启动器配置无法读取：{path.name}。") from error


def save_config(path: Path, config: LauncherConfig) -> None:
    config.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="launcher-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(config), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_port(config: LauncherConfig) -> dict:
    """A bind check never takes over or stops an existing listener."""
    config.validate(False)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            if os.name == "nt":
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # ReaderServer.allow_reuse_address permits a restart while
                # previous connections are in TIME_WAIT. Match that on POSIX;
                # an active listener still prevents this bind. On Windows keep
                # exclusive binding: SO_REUSEADDR can otherwise take over an
                # existing listener's address.
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind((config.host, config.port))
    except OSError as error:
        return {"available": False, "message": f"端口 {config.port} 已被占用或不可用，请更换端口。", "error": str(error)}
    return {"available": True, "message": f"端口 {config.port} 可用。"}


def lan_addresses() -> list[str]:
    # The server already filters virtual/loopback addresses and uses the default
    # route on Windows. Keeping that implementation shared avoids two answers.
    from server import _lan_ipv4_addresses
    result = []
    for value in _lan_ipv4_addresses():
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.is_private and not address.is_loopback and not address.is_link_local and not address.is_unspecified:
            if str(address) not in result:
                result.append(str(address))
    return result


def network_diagnostics(config: LauncherConfig, running: bool = False) -> dict:
    config.validate(False)
    addresses = lan_addresses() if config.lan_enabled else []
    urls = [f"http://{address}:{config.port}/" for address in addresses]
    result = {"local_url": config.local_url, "lan_enabled": config.lan_enabled,
              "lan_urls": urls, "port": config.port, "firewall_verified": False}
    if running:
        checks = []
        for address in ["127.0.0.1", *addresses]:
            try:
                with socket.create_connection((address, config.port), timeout=1):
                    reachable = True
            except OSError:
                reachable = False
            checks.append({"address": address, "reachable_from_this_device": reachable})
        result["connections"] = checks
        result["message"] = ("本机服务端口连接正常。" if checks[0]["reachable_from_this_device"]
                             else "本机服务端口暂时无法连接。")
        if config.lan_enabled:
            result["message"] += " 请在其他设备打开局域网地址验证访问；此检查无法确认入站防火墙规则。"
    else:
        result.update(check_port(config))
    return result


def version_key(value: str) -> tuple:
    match = re.fullmatch(r"[vV]?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value.strip())
    if not match:
        raise LauncherError("发布版本号格式无效。")
    parts = tuple(int(match.group(index)) for index in (1, 2, 3))
    suffix = match.group(4)
    prerelease = tuple((0, int(piece)) if piece.isdigit() else (1, piece)
                       for piece in suffix.split(".")) if suffix else ()
    return (*parts, 1 if suffix is None else 0, prerelease)


@dataclass(frozen=True)
class ReleaseConfig:
    repository: str
    asset_name: str = "Gravelle-Setup-{version}-x64.exe"
    sha256_asset_name: str = "SHA256SUMS.txt"

    def validate(self) -> "ReleaseConfig":
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", self.repository):
            raise LauncherError("更新仓库配置无效。")
        for name in (self.asset_name.replace("{version}", "1.0.0"), self.sha256_asset_name):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,150}", name):
                raise LauncherError("更新文件名称配置无效。")
        if not self.asset_name.casefold().endswith(".exe"):
            raise LauncherError("更新安装程序必须是 EXE 文件。")
        return self

    @property
    def releases_url(self) -> str:
        return f"https://github.com/{self.repository}/releases"


def load_release_config(resource_root: Path) -> ReleaseConfig | None:
    # Only installation resources / administrator environment configure the
    # update origin. The writable launcher.json cannot change it.
    repository = os.environ.get("GRAVELLE_RELEASE_REPOSITORY", "").strip()
    if repository:
        return ReleaseConfig(repository).validate()
    path = resource_root / "desktop" / "release.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        if not value.get("repository"):
            return None
        return ReleaseConfig(**{key: value[key] for key in ("repository", "asset_name", "sha256_asset_name") if key in value}).validate()
    except (OSError, ValueError, TypeError) as error:
        raise LauncherError("更新来源配置无法读取。") from error


def check_update(config: ReleaseConfig | None, current_version: str) -> dict:
    if config is None:
        return {"configured": False, "available": False, "message": "尚未配置发布仓库，自动更新暂不可用。"}
    config.validate()
    request = Request(f"https://api.github.com/repos/{config.repository}/releases/latest",
                      headers={"User-Agent": "Gravelle-Launcher/" + current_version,
                               "Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=8) as response:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != "api.github.com":
                raise LauncherError("更新服务器返回了不受信任的地址。")
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise LauncherError("更新信息过大。")
        release = json.loads(payload)
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise LauncherError("未找到正式发布版本。")
        latest = str(release.get("tag_name", ""))
        if not re.fullmatch(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})", latest):
            raise LauncherError("发布标签不是受支持的稳定版本。")
        available = version_key(latest) > version_key(current_version)
        # Do not navigate to html_url or execute assets supplied by the API.
        # Construct the release page from the fixed trusted repository instead.
        return {"configured": True, "available": available, "version": latest.lstrip("vV"), "tag": latest,
                "url": config.releases_url,
                "message": f"发现新版本 {latest.lstrip('vV')}。" if available else "当前已是最新版本。"}
    except HTTPError as error:
        message = "尚未发布正式版本。" if error.code == 404 else "检查更新失败，请稍后重试。"
        return {"configured": True, "available": False, "message": message, "error": str(error)}
    except (URLError, OSError, ValueError, LauncherError) as error:
        return {"configured": True, "available": False, "message": "暂时无法检查更新，不影响启动阅读器。", "error": str(error)}


DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com",
                            "objects.githubusercontent.com", "github-releases.githubusercontent.com"})


def validate_download_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise LauncherError("更新下载地址不受信任。")
    return url


class TrustedReleaseRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        validate_download_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _download(url: str, maximum: int, destination=None):
    validate_download_url(url)
    request = Request(url, headers={"User-Agent": "Gravelle-Launcher", "Accept": "application/octet-stream"})
    opener = build_opener(TrustedReleaseRedirect())
    chunks = []
    total = 0
    with opener.open(request, timeout=15) as response:
        validate_download_url(response.geturl())
        length = response.headers.get("Content-Length")
        if length and int(length) > maximum:
            raise LauncherError("更新文件超过允许的大小。")
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise LauncherError("更新文件超过允许的大小。")
            if destination is None:
                chunks.append(chunk)
            else:
                destination.write(chunk)
    return b"".join(chunks) if destination is None else total


def checksum_for(checksums: str, filename: str) -> str:
    hashes = []
    for line in checksums.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match and match.group(2) == filename:
            hashes.append(match.group(1).lower())
    if len(hashes) != 1:
        raise LauncherError("更新校验文件未包含唯一匹配的安装程序。")
    return hashes[0]


def verify_installer(path: Path, expected: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected


def download_update(config: ReleaseConfig, release: dict, directory: Path) -> dict:
    config.validate()
    tag = str(release.get("tag", ""))
    version_key(tag)
    version = tag.lstrip("vV")
    filename = config.asset_name.replace("{version}", version)
    base = f"https://github.com/{config.repository}/releases/download/{quote(tag, safe='')}/"
    temporary = None
    directory.mkdir(parents=True, exist_ok=True)
    try:
        checksums = _download(base + quote(config.sha256_asset_name), 512 * 1024).decode("utf-8-sig")
        expected = checksum_for(checksums, filename)
        with tempfile.NamedTemporaryFile("wb", dir=directory, prefix="update-", suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            _download(base + quote(filename), 512 * 1024 * 1024, stream)
        if not verify_installer(temporary, expected):
            raise LauncherError("更新文件校验失败，已取消安装。")
        target = directory / filename
        os.replace(temporary, target)
        return {"path": str(target.resolve()), "sha256": expected, "version": version}
    except (OSError, ValueError, URLError) as error:
        raise LauncherError("更新下载失败，请稍后重试。") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SingleInstanceLock:
    """An OS-owned file lock is released automatically after a process crash."""
    def __init__(self, path: Path, mutex_name: str | None = None):
        self.path = path
        self.stream = None
        self.mutex_name = mutex_name
        self.mutex_handle = None

    def _close_mutex(self):
        if self.mutex_handle is not None:
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(self.mutex_handle)
            self.mutex_handle = None

    def acquire(self) -> bool:
        if self.stream is not None:
            return True
        if os.name == "nt" and self.mutex_name:
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            self.mutex_handle = kernel.CreateMutexW(None, False, self.mutex_name)
            if not self.mutex_handle:
                raise LauncherError("无法创建启动器实例锁。")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                self._close_mutex()
                return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("a+b")
        except OSError:
            self._close_mutex()
            raise
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            self._close_mutex()
            return False
        self.stream = stream
        return True

    def release(self) -> None:
        if self.stream is None:
            self._close_mutex()
            return
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
            self._close_mutex()
