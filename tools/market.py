"""插件市场索引的加载 / 校验 / 生成。

元数据字段与 tuack-ng 的插件清单 `plugin.toml`（registry）对齐：

    name / version / description / authors / license / repo_url / url / pluginapi

市场特有：`artifact_name`（release 归档名）。下载地址由 `repo_url` + `version` +
`artifact_name` 推导为对应 release 的资产；`sha256` 取自该 release 资产的 `digest`。

用 pydantic 定义与校验模型；未知字段忽略，因此 `plugin.toml` 与市场元数据可互相兼容。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
INDEX_FILE = ROOT / "index.json"

#: 索引格式版本（与 Rust 端 `MarketplaceIndex` 对应）。
SCHEMA = 1

#: 市场索引的公开地址；tuack-ng 客户端硬编码的同一个 URL。
INDEX_URL = (
    "https://raw.githubusercontent.com/tuackng/tuack-ng-plugins/master/index.json"
)

#: 插件名只允许小写字母、数字与 `.` `_` `-`。
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
#: 语义化版本（含 `1.1.0-alpha.2` 这类预发布）。
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
#: 仓库地址只接受 GitHub 的 https 形式（可带 `.git`、可带尾斜杠）。
REPO_URL_RE = re.compile(
    r"^https://github\.com/([0-9A-Za-z_.-]+)/([0-9A-Za-z_.-]+?)(?:\.git)?/?$"
)

#: 单次网络请求超时（秒）。
TIMEOUT = 30
#: 归档下载体积上限（与 Rust 端 MAX_ARCHIVE_BYTES 对齐：512 MiB）。
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class PluginMeta(BaseModel):
    """`plugins/<name>.toml`，字段与 `plugin.toml` 对齐。"""

    # 同一文件也可作为 plugin.toml（含 components 等），故忽略未知字段
    model_config = ConfigDict(extra="ignore")

    name: str
    version: str
    description: str
    authors: list[str] = Field(default_factory=list)
    license: str
    repo_url: str
    url: str | None = None
    pluginapi: str
    artifact_name: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError("只允许小写字母、数字与 . _ -")
        return value

    @field_validator("version", "pluginapi")
    @classmethod
    def _check_semver(cls, value: str | None) -> str | None:
        if value is not None and not SEMVER_RE.match(value):
            raise ValueError("必须是语义化版本")
        return value

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, value: str) -> str:
        if not REPO_URL_RE.match(value):
            raise ValueError("必须是 https://github.com/<owner>/<name>")
        return value


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def load_plugin(path: Path) -> PluginMeta:
    """读取并校验单个 `plugins/*.toml`。"""
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return PluginMeta.model_validate(data)


def load_all() -> dict[str, PluginMeta]:
    """读取 plugins/ 下全部元数据（按文件名排序）。"""
    return {path.stem: load_plugin(path) for path in sorted(PLUGINS_DIR.glob("*.toml"))}


def repo_of(repo_url: str) -> str:
    """从 `https://github.com/<owner>/<name>[.git]` 取出 `<owner>/<name>`。"""
    match = REPO_URL_RE.match(repo_url)
    if not match:
        raise ValueError(f"不是合法的 GitHub 仓库地址：{repo_url}")
    return f"{match.group(1)}/{match.group(2)}"


def normalize_version(value: str) -> str:
    """把版本号规整成便于比较的形式（去掉前缀 v 与构建元数据）。"""
    value = value.strip()
    if value.startswith(("v", "V")):
        value = value[1:]
    return value.split("+", 1)[0]


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tuack-ng-plugins-index",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch(url: str) -> bytes:
    """下载 URL，返回字节；超过上限直接报错。"""
    request = urllib.request.Request(url, headers={"User-Agent": "tuack-ng-plugins-index"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError(f"下载体积超过上限：{url}")
            chunks.append(chunk)
    return b"".join(chunks)


def gh_api(path: str) -> Any:
    """调用 GitHub REST API（`path` 形如 `/repos/o/n/releases/tags/v1`）。"""
    request = urllib.request.Request(
        "https://api.github.com" + path, headers=_github_headers()
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def release_by_tag(repo: str, version: str) -> dict:
    """按 tag 查询 release；先试原样，再试带 `v` 前缀。"""
    candidates = [version]
    if not version.startswith(("v", "V")):
        candidates.append("v" + version)
    last: Exception | None = None
    for tag in candidates:
        try:
            return gh_api(f"/repos/{repo}/releases/tags/{tag}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                last = ValueError(f"{repo}: 找不到 release `{tag}`")
                continue
            raise
    raise last if last else ValueError(f"{repo}: 找不到 release `{version}`")


def asset_digest(release: dict, artifact_name: str) -> str | None:
    """取 release 中指定资产的 sha256（GitHub 的 `digest` 形如 `sha256:...`）。"""
    for asset in release.get("assets") or []:
        if asset.get("name") != artifact_name:
            continue
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            return digest.split(":", 1)[1]
        return None
    return None


def download_url(repo: str, tag: str, artifact_name: str) -> str:
    """release 资产的下载地址。"""
    return f"https://github.com/{repo}/releases/download/{tag}/{artifact_name}"


def _find_manifest(names: list[str]) -> str | None:
    """在归档条目名里挑出 `plugin.toml`（归档根或一层子目录内，优先更浅的）。"""
    candidates = [
        (name.count("/"), name)
        for name in names
        if not name.endswith("/")
        and name.rsplit("/", 1)[-1] == "plugin.toml"
        and name.count("/") <= 1
    ]
    if not candidates:
        return None
    return min(candidates)[1]


def extract_plugin_toml(data: bytes, artifact_name: str) -> bytes | None:
    """从发布归档里取出 `plugin.toml`（归档根或一层子目录内）。"""
    lower = artifact_name.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                member = _find_manifest(zf.namelist())
                return zf.read(member) if member is not None else None
        mode = "r:gz" if lower.endswith((".tar.gz", ".tgz")) else "r:*"
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
            member = _find_manifest(tf.getnames())
            if member is None:
                return None
            handles = tf.extractfile(member)
            return handles.read() if handles is not None else None
    except (zipfile.BadZipFile, tarfile.TarError, KeyError, OSError):
        return None


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def build_entry(meta: PluginMeta) -> tuple[dict, list[str]]:
    """构造索引条目：字段对齐 plugin.toml，另加市场信息（download_url/sha256 等）。"""
    warnings: list[str] = []
    repo = repo_of(meta.repo_url)
    release = release_by_tag(repo, meta.version)
    tag = str(release.get("tag_name", ""))

    if normalize_version(tag) != normalize_version(meta.version):
        warnings.append(f"{meta.version}: release tag `{tag}` 与元数据 version 不一致")

    digest = asset_digest(release, meta.artifact_name)
    if digest is None:
        raise ValueError(
            f"{repo}: release `{tag}` 资产 `{meta.artifact_name}` 缺失或无 digest"
        )

    entry = {
        "name": meta.name,
        "version": meta.version,
        "description": meta.description,
        "authors": meta.authors,
        "license": meta.license,
        "repo_url": meta.repo_url,
        "url": meta.url,
        "pluginapi": meta.pluginapi,
        "artifact_name": meta.artifact_name,
        "download_url": download_url(repo, tag or f"v{meta.version}", meta.artifact_name),
        "sha256": digest,
    }
    return entry, warnings


def build_index(plugins: dict[str, PluginMeta]) -> tuple[dict, list[str]]:
    """把所有插件元数据编译成市场索引。"""
    entries: list[dict] = []
    warnings: list[str] = []
    for name in sorted(plugins):
        entry, warns = build_entry(plugins[name])
        entries.append(entry)
        warnings.extend(f"{name}: {w}" for w in warns)
    index = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "plugins": entries,
    }
    return index, warnings


def verify_remote(meta: PluginMeta) -> list[str]:
    """联网校验：对应 release 版本/资产、资产 digest，并下载归档核对内层 plugin.toml。"""
    errors: list[str] = []
    repo = repo_of(meta.repo_url)
    version = normalize_version(meta.version)

    try:
        release = release_by_tag(repo, meta.version)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return [f"查询 {repo} 的 release 失败：{exc}"]
    except ValueError as exc:
        return [str(exc)]

    tag = str(release.get("tag_name", ""))
    if normalize_version(tag) != version:
        errors.append(f"release tag `{tag}` 与 version `{meta.version}` 不一致")

    digest = asset_digest(release, meta.artifact_name)
    if digest is None:
        errors.append(f"release `{tag}` 资产 `{meta.artifact_name}` 缺失或无 digest")

    url = download_url(repo, tag, meta.artifact_name)
    try:
        data = fetch(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return errors + [f"下载失败 {url}：{exc}"]

    actual = hashlib.sha256(data).hexdigest()
    if digest is not None and actual != digest:
        errors.append(f"归档 sha256 与 release digest 不一致：{actual} != {digest}")

    manifest = extract_plugin_toml(data, meta.artifact_name)
    if manifest is None:
        return errors + [f"归档内未找到 plugin.toml：{meta.artifact_name}"]
    try:
        inner = tomllib.loads(manifest.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return errors + [f"归档内 plugin.toml 解析失败：{exc}"]
    if inner.get("name") != meta.name:
        errors.append(
            f"归档内 plugin.toml 的 name `{inner.get('name')}` 与元数据 name `{meta.name}` 不一致"
        )
    if normalize_version(str(inner.get("version", ""))) != version:
        errors.append(
            f"归档内 plugin.toml 的 version `{inner.get('version')}` 与元数据 version `{meta.version}` 不一致"
        )
    inner_api = str(inner.get("pluginapi", ""))
    if inner_api and normalize_version(inner_api) != normalize_version(meta.pluginapi):
        errors.append(
            f"归档内 plugin.toml 的 pluginapi `{inner_api}` 与元数据 `{meta.pluginapi}` 不一致"
        )
    return errors


__all__ = [
    "INDEX_FILE",
    "INDEX_URL",
    "MAX_ARCHIVE_BYTES",
    "NAME_RE",
    "PLUGINS_DIR",
    "PluginMeta",
    "REPO_URL_RE",
    "SCHEMA",
    "SEMVER_RE",
    "ValidationError",
    "asset_digest",
    "build_entry",
    "build_index",
    "download_url",
    "extract_plugin_toml",
    "fetch",
    "gh_api",
    "load_all",
    "load_plugin",
    "normalize_version",
    "release_by_tag",
    "repo_of",
    "verify_remote",
]
