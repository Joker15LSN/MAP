"""
Compare scene_selection.json between two environments.

Usage:
    python -m map_core.utils.remote_config_compare.scripts.compare_scene_selection \
        --left dumped_configs/ubddev/scene_selection.json \
        --right dumped_configs/ubdprod/scene_selection.json

Output:
    Summary of diffs under the "scene_selection" key.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from map_core.utils.remote_config_compare.config_compare import (
    compare_configs,
    ConfigComparator,
)
from map_core.utils.remote_config_compare.highlight import auto_highlight


def load_json(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compare_scene_selection(
    left_path: str | Path,
    right_path: str | Path,
    *,
    ignore_paths: set[str] | None = None,
    highlight: bool = True,
) -> None:
    left = load_json(left_path)
    right = load_json(right_path)

    # 只比较 scene_selection 字段下的内容
    # ignore_paths 需要加上 only_path 前缀，因为裁剪后路径以 scene_selection. 开头
    if ignore_paths:
        ignore_paths = {f"scene_selection.{p}" for p in ignore_paths}
    result = compare_configs(left, right, only_path="scene_selection", ignore_paths=ignore_paths)

    diffs = ConfigComparator().find_diffs(result)

    # Summary
    print(f"\n{'='*60}")
    print(f"Comparing: {left_path}")
    print(f"     with: {right_path}")
    print(f"Focus: scene_selection")
    print(f"{'='*60}")

    if not diffs:
        print("✅ scene_selection 完全一致")
        return

    print(f"❌ 共发现 {len(diffs)} 处差异:\n")

    for d in diffs:
        # 去掉 scene_selection. 前缀让路径更短
        display_path = d.path.removeprefix("scene_selection.").removeprefix("scene_selection")
        print(f"  [{display_path or 'root'}]")

        left_str = d.left if isinstance(d.left, str) else repr(d.left)
        right_str = d.right if isinstance(d.right, str) else repr(d.right)
        if highlight:
            left_str, right_str = auto_highlight(left_str, right_str)
        print(f"    left :  {left_str}")
        print(f"    right:  {right_str}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare scene_selection between two JSON files"
    )
    parser.add_argument("--left", required=True, help="Left JSON file path")
    parser.add_argument("--right", required=True, help="Right JSON file path")
    parser.add_argument(
        "--ignore",
        nargs="+",
        default=None,
        help="Paths to ignore, e.g. meta.note enabled_agent_codes.HR",
    )
    parser.add_argument(
        "--no-highlight",
        action="store_true",
        help="Disable inline character-level diff highlighting",
    )
    args = parser.parse_args()

    ignore_paths = set(args.ignore) if args.ignore else None
    compare_scene_selection(
        args.left, args.right,
        ignore_paths=ignore_paths,
        highlight=not args.no_highlight,
    )


if __name__ == "__main__":
    main()
    '''
    --ignore enabled_agent_codes.IPD_RD enabled_agent_codes.Financial_Assistant
    
    '''
    
    '''
    python -m map_core.utils.remote_config_compare.scripts.compare_scene_selection --right /Users/ley/work_space/supcon/dev_projects/map-core/map_core/utils/remote_config_compare/dumped_configs/ubddev/scene_selection.json --left /Users/ley/work_space/supcon/dev_projects/map-core/map_core/eval/scene_classify/current_config/update_company_news_20260515.json  --ignore enabled_agent_codes.IPD_RD enabled_agent_codes.Financial_Assistant enabled_agent_codes.MASTER
    '''
