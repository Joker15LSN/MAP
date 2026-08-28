#!/usr/bin/env python3
"""Pretty-print JSON with path-based filtering and type selection."""

import argparse
import json
import sys
from typing import Any


def print_value(path: str, value: Any, target_type: str | None) -> None:
    """Print a path and its value if type matches."""
    if target_type is not None and target_type != "all":
        if target_type == "str" and not isinstance(value, str):
            return
        if target_type == "int" and not isinstance(value, int):
            return
        if target_type == "float" and not isinstance(value, float):
            return
        if target_type == "bool" and not isinstance(value, bool):
            return
        if target_type == "list" and not isinstance(value, list):
            return
        if target_type == "dict" and not isinstance(value, dict):
            return
        if target_type == "none" and value is not None:
            return

    print(path)
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    print()


def walk(data: Any, prefix: str, target_path: str | None, target_type: str | None) -> None:
    """Recursively walk JSON and print matching entries."""
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if target_path is not None and not (path == target_path or path.startswith(target_path + ".")):
                if not target_path.startswith(path + "."):
                    continue
            if isinstance(value, (dict, list)):
                walk(value, path, target_path, target_type)
            else:
                print_value(path, value, target_type)
        # Also print the dict itself if type matches and path matches exactly
        if target_path is None or prefix == target_path:
            print_value(prefix, data, target_type)
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            path = f"{prefix}[{idx}]"
            if target_path is not None and not (path == target_path or path.startswith(target_path + ".") or path.startswith(target_path + "[")):
                if not target_path.startswith(path):
                    continue
            if isinstance(item, (dict, list)):
                walk(item, path, target_path, target_type)
            else:
                print_value(path, item, target_type)
        # Also print the list itself if type matches and path matches exactly
        if target_path is None or prefix == target_path:
            print_value(prefix, data, target_type)
    else:
        if target_path is None or prefix == target_path:
            print_value(prefix, data, target_type)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pretty-print JSON with path and type filtering")
    parser.add_argument("file", nargs="?", help="JSON file to read (default: stdin)")
    parser.add_argument("-p", "--path", help="Only print values under this path")
    parser.add_argument("-t", "--type", default="str", help="Target type: str, int, float, bool, list, dict, none, or all (default: str)")
    args = parser.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    walk(data, "", args.path, args.type)


if __name__ == "__main__":
    main()

# Usage: python -m map_core.utils.remote_config_compare.scripts.pretty_print [file] [-p PATH] [-t TYPE]
