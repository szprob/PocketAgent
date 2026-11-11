"""同步用的存储后端：只要能「列目录、读、写、删」就行。

第一版给两个：
- WebDavBackend：坚果云的 WebDAV 接口（免费版每月 1GB 上传 / 3GB 下载，国内直连）。
- LocalDirectoryBackend：指向一个本来就在同步的目录（iCloud 云盘、OneDrive、NAS 挂载点）。
  不需要任何账号和 API，测试也用它。
- GitHubBackend：把私有仓库当存储，一个文件一个 blob，走 Contents API（Token 要 Contents 读写）。
云端只存两样东西：<prefix>/<相对路径> 的数据文件，和 devices/<设备 id>.json 的心跳。
"""

import base64
import hashlib
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

import httpx

DAV_NAMESPACE = "DAV:"
PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getetag/><d:getcontentlength/><d:getlastmodified/>"
    "</d:prop></d:propfind>"
)


class SyncError(Exception):
    """网络、鉴权、远端状态不对：消息直接给用户看。"""


class RemoteEntry:
    """远端一个文件。etag 只用来判断「上次同步之后远端有没有变」，不要求是内容哈希。"""

    def __init__(self, path: str, etag: str, size: int = 0) -> None:
        self.path = path
        self.etag = etag
        self.size = size

    def __repr__(self) -> str:
        return f"RemoteEntry(path={self.path!r}, etag={self.etag!r}, size={self.size})"


class SyncBackend(Protocol):
    def ping(self) -> None: ...

    def list_files(self, prefix: str = "") -> list[RemoteEntry]: ...

    def read(self, path: str) -> bytes: ...

    def write(self, path: str, data: bytes) -> str: ...

    def delete(self, path: str) -> None: ...


def _etag_or_hash(response: httpx.Response, data: bytes) -> str:
    etag = response.headers.get("etag", "").strip().strip('"')
    if etag:
        return etag
    # 服务器不给 ETag 时退回内容哈希：下次还能靠它判断远端有没有变，只是读的时候要多下一遍
    return "sha256:" + hashlib.sha256(data).hexdigest()


class LocalDirectoryBackend:
    """把一个本地目录当远端。iCloud / OneDrive 的同步由系统负责，这里只管读写。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _resolve(self, path: str) -> Path:
        normalized = path.strip("/")
        target = (self.root / normalized).resolve()
        if self.root != target and self.root not in target.parents:
            raise SyncError(f"非法路径：{path}")
        return target

    def ping(self) -> None:
        if not self.root.is_dir():
            raise SyncError(f"远端目录不存在：{self.root}")

    def list_files(self, prefix: str = "") -> list[RemoteEntry]:
        root = self._resolve(prefix) if prefix else self.root
        if not root.is_dir():
            # 前缀目录还没建出来（第一次同步）当成空的
            return []
        entries: list[RemoteEntry] = []
        for item in sorted(root.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(self.root).as_posix()
            entries.append(RemoteEntry(relative, self._etag(item), item.stat().st_size))
        return entries

    def _etag(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(block)
        return digest.hexdigest()

    def read(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.is_file():
            raise SyncError(f"远端没有这个文件：{path}")
        return target.read_bytes()

    def write(self, path: str, data: bytes) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        return self._etag(target)

    def delete(self, path: str) -> None:
        target = self._resolve(path)
        if target.is_file():
            target.unlink()


GITHUB_API_BASE = "https://api.github.com"


class GitHubBackend:
    """用 GitHub 私有仓库当远端。

    一个文件一个 object，提交历史就是版本历史；ETag 用 blob sha，内容变了 sha 一定变。
    要求仓库已经有初始提交（Contents API 不能往空仓库里写）。
    """

    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = "main",
        api_base: str = GITHUB_API_BASE,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.repo = normalize_repo(repo)
        self.branch = (branch or "main").strip() or "main"
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _contents_url(self, path: str) -> str:
        quoted = "/".join(quote(part) for part in path.strip("/").split("/") if part)
        return f"{self.api_base}/repos/{self.repo}/contents/{quoted}"

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise SyncError(f"连不上 GitHub：{error}") from error
        if response.status_code == 401:
            raise SyncError("GitHub Token 无效或已过期：重新生成一个带 Contents 读写权限的 Token")
        if response.status_code == 403:
            text = response.text.lower()
            if "rate limit" in text:
                raise SyncError("GitHub 接口限流了（未登录 60 次/小时，Token 5000 次/小时），过一会儿再试")
            raise SyncError("GitHub 拒绝访问（403）：Token 缺少 Contents 读写权限")
        return response

    def ping(self) -> None:
        response = self._request("GET", f"{self.api_base}/repos/{self.repo}")
        if response.status_code == 404:
            raise SyncError(f"GitHub 仓库不存在或 Token 没有访问权限：{self.repo}")
        if response.status_code >= 400:
            raise SyncError(f"GitHub 连接失败（{response.status_code}）：{self.repo}")
        data = response.json()
        permissions = data.get("permissions") or {}
        if permissions and not permissions.get("push", True):
            raise SyncError("Token 对这个仓库只有读权限：需要 Contents 读写")

    def list_files(self, prefix: str = "") -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        pending = [prefix.strip("/")]
        visited: set[str] = set()
        while pending:
            directory = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            url = f"{self._contents_url(directory)}?ref={quote(self.branch)}"
            response = self._request("GET", url)
            if response.status_code == 404:
                if not directory:
                    raise SyncError(f"GitHub 仓库或分支不存在：{self.repo}@{self.branch}")
                continue
            if response.status_code >= 400:
                raise SyncError(f"列目录失败（{response.status_code}）：{directory or '/'}")
            items = response.json()
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if item.get("type") == "dir":
                    pending.append(item.get("path", ""))
                elif item.get("type") == "file":
                    entries.append(
                        RemoteEntry(item.get("path", ""), str(item.get("sha") or ""), int(item.get("size") or 0))
                    )
        return entries

    def read(self, path: str) -> bytes:
        url = f"{self._contents_url(path)}?ref={quote(self.branch)}"
        response = self._request("GET", url, headers={"Accept": "application/vnd.github.raw"})
        if response.status_code == 404:
            raise SyncError(f"远端没有这个文件：{path}")
        if response.status_code >= 400:
            raise SyncError(f"下载失败（{response.status_code}）：{path}")
        return response.content

    def write(self, path: str, data: bytes) -> str:
        body = {
            "message": f"pocketagent: update {path}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
        }
        sha = self._file_sha(path)
        if sha:
            body["sha"] = sha
        response = self._request("PUT", self._contents_url(path), json=body)
        if response.status_code == 404:
            raise SyncError(f"分支不存在：{self.branch}（先在仓库里建好这个分支）")
        if response.status_code == 422:
            raise SyncError(f"GitHub 不接受这个文件（422）：{path}；空仓库要先有一次提交")
        if response.status_code >= 400:
            raise SyncError(f"上传失败（{response.status_code}）：{path}")
        payload = response.json().get("content") or {}
        return str(payload.get("sha") or "sha256:" + hashlib.sha256(data).hexdigest())

    def delete(self, path: str) -> None:
        sha = self._file_sha(path)
        if not sha:
            return
        response = self._request(
            "DELETE",
            self._contents_url(path),
            json={"message": f"pocketagent: delete {path}", "sha": sha, "branch": self.branch},
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise SyncError(f"删除失败（{response.status_code}）：{path}")

    def _file_sha(self, path: str) -> str:
        response = self._request("GET", f"{self._contents_url(path)}?ref={quote(self.branch)}")
        if response.status_code == 404:
            return ""
        if response.status_code >= 400:
            raise SyncError(f"读取文件信息失败（{response.status_code}）：{path}")
        data = response.json()
        return str(data.get("sha") or "") if isinstance(data, dict) else ""


def normalize_repo(repo: str) -> str:
    text = repo.strip().rstrip("/")
    if text.startswith("http://") or text.startswith("https://"):
        path = urlparse(text).path.strip("/")
        text = path[:-4] if path.endswith(".git") else path
    if text.endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if len(parts) != 2:
        raise SyncError(f"GitHub 仓库要写成 owner/repo：{repo!r}")
    return "/".join(parts)


class WebDavBackend:
    """WebDAV 客户端。坚果云要的是「第三方应用密码」，不是登录密码。"""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify_tls: bool = True,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = url.rstrip("/") + "/"
        self.base_path = unquote(urlparse(self.base_url).path).rstrip("/") + "/"
        self._client = client or httpx.Client(
            auth=(username, password),
            verify=verify_tls,
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._known_directories: set[str] = {""}

    def close(self) -> None:
        self._client.close()

    def _url(self, path: str) -> str:
        normalized = path.strip("/")
        if not normalized:
            return self.base_url
        quoted = "/".join(quote(part) for part in normalized.split("/"))
        return self.base_url + quoted

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, self._url(path), **kwargs)
        except httpx.HTTPError as error:
            raise SyncError(f"连不上 {self.base_url}：{error}") from error
        if response.status_code == 401:
            raise SyncError("WebDAV 鉴权失败：坚果云要填「第三方应用密码」，不是登录密码")
        if response.status_code == 403:
            raise SyncError(f"WebDAV 拒绝访问（403）：{path}")
        return response

    def list_files(self, prefix: str = "") -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        pending = [prefix.strip("/")]
        visited: set[str] = set()
        while pending:
            directory = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            response = self._request(
                "PROPFIND",
                directory,
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                content=PROPFIND_BODY.encode("utf-8"),
            )
            if response.status_code == 404:
                if not directory:
                    raise SyncError(f"远端目录不存在：{self.base_url}；先在网盘里建好这个目录再同步")
                # 数据目录还没建出来（第一次同步），当成空的
                return entries
            if response.status_code >= 400:
                raise SyncError(f"列目录失败（{response.status_code}）：{directory or '/'}")
            for item in _parse_propfind(response.text, self.base_path):
                if item["is_directory"]:
                    if item["path"]:
                        pending.append(item["path"])
                    continue
                entries.append(RemoteEntry(item["path"], item["etag"], item["size"]))
        return entries

    def ping(self) -> None:
        response = self._request(
            "PROPFIND",
            "",
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            content=PROPFIND_BODY.encode("utf-8"),
        )
        if response.status_code == 404:
            raise SyncError(f"远端目录不存在：{self.base_url}；先在网盘里建好这个目录再同步")
        if response.status_code >= 400:
            raise SyncError(f"远端连不上（{response.status_code}）：{self.base_url}")

    def read(self, path: str) -> bytes:
        response = self._request("GET", path)
        if response.status_code == 404:
            raise SyncError(f"远端没有这个文件：{path}")
        if response.status_code >= 400:
            raise SyncError(f"下载失败（{response.status_code}）：{path}")
        return response.content

    def write(self, path: str, data: bytes) -> str:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        self._ensure_directories(parent)
        response = self._request("PUT", path, content=data, headers={"Content-Type": "application/octet-stream"})
        if response.status_code >= 400:
            raise SyncError(f"上传失败（{response.status_code}）：{path}")
        etag = response.headers.get("etag", "").strip().strip('"')
        if not etag:
            # 坚果云的 PUT 不回 ETag，得单独问一次；否则下次同步会把刚传的文件当成「远端改过」
            etag = self._etag_for(path)
        return etag or _etag_or_hash(response, data)

    def _etag_for(self, path: str) -> str:
        response = self._request(
            "PROPFIND",
            path,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            content=PROPFIND_BODY.encode("utf-8"),
        )
        if response.status_code >= 400:
            return ""
        for item in _parse_propfind(response.text, self.base_path):
            if item["path"] == path.strip("/"):
                return item["etag"]
        return ""

    def delete(self, path: str) -> None:
        response = self._request("DELETE", path)
        if response.status_code >= 400 and response.status_code != 404:
            raise SyncError(f"删除失败（{response.status_code}）：{path}")

    def _ensure_directories(self, directory: str) -> None:
        parts = [part for part in directory.strip("/").split("/") if part]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            if current in self._known_directories:
                continue
            response = self._request("MKCOL", current)
            # 201 = 建好；405 = 已经有了。其他状态码说明父目录权限之类有问题
            if response.status_code not in (201, 405):
                raise SyncError(f"建目录失败（{response.status_code}）：{current}")
            self._known_directories.add(current)


def _parse_propfind(xml_text: str, base_path: str) -> list[dict]:
    """把 PROPFIND 的 XML 变成相对路径。坚果云返回的 href 是 URL 编码的绝对路径。"""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise SyncError(f"WebDAV 返回的不是合法 XML：{error}") from error
    results: list[dict] = []
    for node in root.findall(f"{{{DAV_NAMESPACE}}}response"):
        href = node.findtext(f"{{{DAV_NAMESPACE}}}href")
        if not href:
            continue
        item_path = unquote(urlparse(href).path)
        if not item_path.startswith(base_path):
            continue
        relative = item_path[len(base_path) :].strip("/")
        properties = node.find(f"{{{DAV_NAMESPACE}}}propstat/{{{DAV_NAMESPACE}}}prop")
        is_directory = False
        etag = ""
        size = 0
        if properties is not None:
            resource_type = properties.find(f"{{{DAV_NAMESPACE}}}resourcetype")
            collection = None if resource_type is None else resource_type.find(f"{{{DAV_NAMESPACE}}}collection")
            is_directory = collection is not None
            etag = (properties.findtext(f"{{{DAV_NAMESPACE}}}getetag") or "").strip().strip('"')
            raw_size = properties.findtext(f"{{{DAV_NAMESPACE}}}getcontentlength") or "0"
            try:
                size = int(raw_size)
            except ValueError:
                size = 0
        if not relative:
            continue
        results.append({"path": relative, "is_directory": is_directory, "etag": etag, "size": size})
    return results
