#!/usr/bin/env python3
"""生成 `index.json`（插件市场索引）。

用法：
    uv run python tools/build_index.py            # 联网查询 release 并重建索引
    uv run python tools/build_index.py --check    # 只校验并比较，不写文件

索引结构（与 tuack-ng 客户端的 `MarketplaceIndex` 对应）：

    {
      "schema": 1,
      "generated_at": "2026-09-24T10:01:44Z",
      "plugins": [ { name, version, description, authors, license,
                     repo_url, url, pluginapi, artifact_name,
                     download_url, sha256 }, ... ]
    }
"""

from __future__ import annotations

import json
import sys

import market
from pydantic import ValidationError


def main(argv: list[str]) -> int:
    check_only = "--check" in argv

    plugins: dict[str, market.PluginMeta] = {}
    failed = False

    for path in sorted(market.PLUGINS_DIR.glob("*.toml")):
        try:
            meta = market.load_plugin(path)
        except ValidationError as exc:
            print(f"[FAIL] {path.name}：结构校验失败")
            for error in exc.errors():
                loc = ".".join(str(part) for part in error["loc"]) or "<root>"
                print(f"        {loc}: {error['msg']}")
            failed = True
            continue

        if meta.name != path.stem:
            print(f"[FAIL] {path.name}：内部 name `{meta.name}` 与文件名不一致")
            failed = True
            continue

        plugins[meta.name] = meta

    if failed:
        return 1

    if not plugins:
        print("[FAIL] plugins/ 下没有任何插件元数据")
        return 1

    try:
        index, warnings = market.build_index(plugins)
    except Exception as exc:  # noqa: BLE001 - 汇总网络/资产错误
        print(f"[FAIL] 生成索引失败：{exc}")
        return 1

    for warning in warnings:
        print(f"[WARN] {warning}")

    if check_only:
        print("[ OK ] 索引已生成（未写入）" if not unchanged(index) else "索引内容无变化。")
        return 0

    if unchanged(index):
        print("索引内容无变化，跳过更新。")
        return 0

    market.INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 {market.INDEX_FILE.name}，共 {len(index['plugins'])} 个插件。")
    return 0


def unchanged(index: dict) -> bool:
    """除 `generated_at` 外内容与现有 index.json 相同时返回 True。"""
    if not market.INDEX_FILE.exists():
        return False
    try:
        existing = json.loads(market.INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        existing.get("schema") == index["schema"]
        and existing.get("plugins") == index["plugins"]
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
