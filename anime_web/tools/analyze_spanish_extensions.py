#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


GRADLE_KEYS = {
    "extName": re.compile(r"extName\s*=\s*['\"]([^'\"]+)['\"]"),
    "extClass": re.compile(r"extClass\s*=\s*['\"]([^'\"]+)['\"]"),
    "themePkg": re.compile(r"themePkg\s*=\s*['\"]([^'\"]+)['\"]"),
    "baseUrl": re.compile(r"baseUrl\s*=\s*['\"]([^'\"]+)['\"]"),
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def analyze_module(module_dir: Path) -> dict:
    kt_files = sorted(module_dir.rglob("*.kt"))
    gradle = module_dir / "build.gradle"
    data = {
        "module": module_dir.name,
        "kt_files": [str(path.relative_to(module_dir)) for path in kt_files],
        "kt_count": len(kt_files),
        "gradle": {},
        "classes": [],
    }

    if gradle.exists():
        text = read_text(gradle)
        for key, pattern in GRADLE_KEYS.items():
            match = pattern.search(text)
            if match:
                data["gradle"][key] = match.group(1)
        data["gradle"]["dependencies"] = re.findall(
            r"implementation\(project\(['\"]:([^'\"]+)['\"]\)\)",
            text,
        )

    for file_path in kt_files:
        text = read_text(file_path)
        for match in re.finditer(r"\bclass\s+([A-Za-z0-9_]+)", text):
            data["classes"].append(
                {
                    "name": match.group(1),
                    "file": str(file_path.relative_to(module_dir)),
                },
            )

    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Spanish Kotlin extensions.")
    parser.add_argument(
        "root",
        nargs="?",
        default="../to_be_analyzed/extensions-source-main/src/es",
        help="Path to extensions-source-main/src/es",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    modules = [
        analyze_module(path)
        for path in sorted(root.iterdir())
        if path.is_dir()
    ]

    if args.json:
        print(json.dumps({"root": str(root), "modules": modules}, indent=2, ensure_ascii=True))
        return 0

    print(f"root: {root}")
    print(f"modules: {len(modules)}")
    print("module\tkt\textName\tthemePkg\tbaseUrl\tfirst_file")
    for module in modules:
        gradle = module["gradle"]
        first_file = module["kt_files"][0] if module["kt_files"] else ""
        print(
            "\t".join(
                [
                    module["module"],
                    str(module["kt_count"]),
                    gradle.get("extName", ""),
                    gradle.get("themePkg", ""),
                    gradle.get("baseUrl", ""),
                    first_file,
                ],
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

