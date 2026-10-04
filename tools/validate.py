#!/usr/bin/env python3
"""校验 `plugins/` 下的插件元数据（pydantic 模型）。

用法：
    uv run python tools/validate.py            # 结构与字段校验（离线）
    uv run python tools/validate.py --fetch    # 额外查询 release、核对资产 digest 与内层 plugin.toml
    uv run python tools/validate.py ccr loj-uoj
"""

from __future__ import annotations

import sys
from pathlib import Path

import market
from pydantic import ValidationError


def main(argv: list[str]) -> int:
    fetch = "--fetch" in argv
    names = [arg for arg in argv if not arg.startswith("-")]

    if names:
        paths = [market.PLUGINS_DIR / f"{Path(name).stem}.toml" for name in names]
    else:
        paths = sorted(market.PLUGINS_DIR.glob("*.toml"))

    if not paths:
        print("没有插件元数据需要校验。")
        return 0

    failed = 0
    for path in paths:
        if not path.is_file():
            print(f"[FAIL] {path.name}：文件不存在")
            failed += 1
            continue

        try:
            meta = market.load_plugin(path)
        except ValidationError as exc:
            print(f"[FAIL] {path.name}：结构校验失败")
            for error in exc.errors():
                loc = ".".join(str(part) for part in error["loc"]) or "<root>"
                print(f"        {loc}: {error['msg']}")
            failed += 1
            continue

        print(f"[ OK ] {path.name}：{meta.name} {meta.version}（pluginapi {meta.pluginapi}）")

        if fetch:
            try:
                errors = market.verify_remote(meta)
            except Exception as exc:  # noqa: BLE001 - 汇总网络/资产错误
                errors = [f"校验过程出错：{exc}"]
            for error in errors:
                print(f"[FAIL] {path.name}：{error}")
            if errors:
                failed += 1

    total = len(paths)
    if failed:
        print(f"\n{failed}/{total} 个插件元数据未通过。")
        return 1
    print(f"\n{total} 个插件元数据全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
