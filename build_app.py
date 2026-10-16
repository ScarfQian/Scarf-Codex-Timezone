#!/usr/bin/env python3
"""构建本机可点击的 macOS 应用。日期：2026-10-15；创建人：ScarfQian。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import sys


ROOT = Path(__file__).resolve().parent
VERSION = "0.2.1"


def build_app(output: Path, python: Path) -> Path:
    if sys.platform != "darwin":
        raise ValueError("应用包构建只支持 macOS。")
    python = python.expanduser().resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"Python 可执行文件不存在：{python}")
    output = output.expanduser().absolute()
    if output.suffix != ".app":
        raise ValueError("输出路径必须以 .app 结尾。")
    if output.exists():
        raise ValueError(f"输出已存在，未覆盖：{output}；请选择新的 --output 路径。")
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = output / "Contents"
    resources = contents / "Resources"
    macos = contents / "MacOS"
    resources.mkdir(parents=True)
    macos.mkdir()
    for name in ("launcher.py", "timezone_probe.py", "auto_mode.py", "README.md", "LICENSE", "config.example.json"):
        shutil.copyfile(ROOT / name, resources / name)
    info = {
        "CFBundleName": "Scarf-Codex-Timezone",
        "CFBundleDisplayName": "Scarf-Codex-Timezone",
        "CFBundleIdentifier": "io.github.scarfqian.codex-timezone",
        "CFBundleExecutable": "Scarf-Timezone",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": "3",
        "LSUIElement": True,
        "LSMultipleInstancesProhibited": True,
        "NSHighResolutionCapable": True,
    }
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(info, stream)
    entry = macos / "Scarf-Timezone"
    entry.write_text(
        "#!/bin/zsh\n"
        "set -euo pipefail\n"
        'RESOURCE_DIR="${0:A:h:h}/Resources"\n'
        f'exec {shlex.quote(str(python))} -B "$RESOURCE_DIR/launcher.py" --gui "$@"\n',
        encoding="utf-8",
    )
    entry.chmod(0o755)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="在本机打包 Scarf-Codex-Timezone；不包含 Python 运行时")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "Scarf-Codex-Timezone.app")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="应用入口使用的已有 Python 3.9+ 路径")
    args = parser.parse_args()
    try:
        app = build_app(args.output, args.python)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"构建未完成：{exc}\n")
    print(f"已构建：{app}\n请先配置本机代理组，再双击运行。应用依赖上述 Python 路径，不包含运行时。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
