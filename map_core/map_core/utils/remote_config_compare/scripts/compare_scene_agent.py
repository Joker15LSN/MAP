"""
Compare scene agent configs between two environments or two specific files.

Usage:
    # 1v1 单文件对比 (--left/--right 传 .json 文件)
    python -m map_core.utils.remote_config_compare.scripts.compare_scene_agent \
        --left dumped_configs/ubddev/<ts>/scene_conf/Procurement.json \
        --right dumped_configs/ubdprod/<ts>/scene_conf/Procurement.json

    # 批量全场景对比 (--left/--right 传目录)
    python -m map_core.utils.remote_config_compare.scripts.compare_scene_agent \
        --left dumped_configs/ubddev/<ts>/scene_conf \
        --right dumped_configs/ubdprod/<ts>/scene_conf

Output:
    Summary of diffs under the "scene_agent_configs.{scene_code}" key.
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


def compare_single_scene(
    left_path: str | Path,
    right_path: str | Path,
    *,
    ignore_paths: set[str] | None = None,
    scene_code: str | None = None,
) -> list:
    """Compare two scene conf files and return diffs.

    Compares both scene_agent_configs.{scene_code} and tool_context.{scene_code}.
    """
    left = load_json(left_path)
    right = load_json(right_path)

    # Auto-detect scene_code from filename if not provided
    if scene_code is None:
        scene_code = Path(left_path).stem

    # Build full ignore paths for both sections
    full_ignore_paths: set[str] = set()
    if ignore_paths:
        for section in ("scene_agent_configs", "tool_context"):
            full_ignore_paths.update({f"{section}.{scene_code}.{p}" for p in ignore_paths})

    diffs: list = []
    for section in ("scene_agent_configs", "tool_context"):
        only_path = f"{section}.{scene_code}"
        result = compare_configs(left, right, only_path=only_path, ignore_paths=full_ignore_paths)
        diffs.extend(ConfigComparator().find_diffs(result))

    return diffs


def print_diff_summary(
    diffs: list,
    left_label: str,
    right_label: str,
    scene_code: str,
    *,
    highlight: bool = True,
) -> None:
    """Print formatted diff summary."""
    prefix = f"scene_agent_configs.{scene_code}."

    print(f"\n{'=' * 60}")
    print(f"Scene: {scene_code}")
    print(f"Left : {left_label}")
    print(f"Right: {right_label}")
    print(f"{'=' * 60}")

    if not diffs:
        print("✅ 完全一致")
        return

    print(f"❌ 共发现 {len(diffs)} 处差异:\n")
    for d in diffs:
        # Strip both possible section prefixes for cleaner display
        display_path = d.path
        for section in ("scene_agent_configs.", "tool_context."):
            prefix = f"{section}{scene_code}."
            display_path = display_path.removeprefix(prefix)
        print(f"  [{display_path}]")

        left_str = d.left if isinstance(d.left, str) else repr(d.left)
        right_str = d.right if isinstance(d.right, str) else repr(d.right)
        if highlight:
            left_str, right_str = auto_highlight(left_str, right_str)
        print(f"    left :  {left_str}")
        print(f"    right:  {right_str}")
        print()


def compare_batch(
    left_dir: str | Path,
    right_dir: str | Path,
    *,
    ignore_paths: set[str] | None = None,
    highlight: bool = True,
) -> None:
    """Batch compare all scene conf files in two directories."""
    left_dir = Path(left_dir)
    right_dir = Path(right_dir)

    if not left_dir.exists():
        raise FileNotFoundError(f"left_dir not found: {left_dir.absolute()}")
    if not right_dir.exists():
        raise FileNotFoundError(f"right_dir not found: {right_dir.absolute()}")

    left_files = {p.stem: p for p in left_dir.glob("*.json")}
    right_files = {p.stem: p for p in right_dir.glob("*.json")}

    if not left_files:
        raise ValueError(f"No *.json files found in left_dir: {left_dir.absolute()}")
    if not right_files:
        raise ValueError(f"No *.json files found in right_dir: {right_dir.absolute()}")

    all_scenes = sorted(set(left_files.keys()) | set(right_files.keys()))

    total_diff_scenes = 0
    total_diff_count = 0

    for scene_code in all_scenes:
        left_path = left_files.get(scene_code)
        right_path = right_files.get(scene_code)

        if left_path is None:
            print(f"\n⚠️  [{scene_code}] 仅在 right 存在，left 缺失")
            total_diff_scenes += 1
            continue
        if right_path is None:
            print(f"\n⚠️  [{scene_code}] 仅在 left 存在，right 缺失")
            total_diff_scenes += 1
            continue

        diffs = compare_single_scene(
            left_path, right_path,
            ignore_paths=ignore_paths,
            scene_code=scene_code,
        )
        print_diff_summary(diffs, str(left_path), str(right_path), scene_code, highlight=highlight)

        if diffs:
            total_diff_scenes += 1
            total_diff_count += len(diffs)

    # Final summary
    print(f"\n{'=' * 60}")
    print(f"汇总: {len(all_scenes)} 个场景，{total_diff_scenes} 个有差异，共 {total_diff_count} 处不同")
    print(f"{'=' * 60}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare scene agent configs between environments"
    )

    parser.add_argument(
        "--left",
        required=True,
        help="Left path: a JSON file (1v1 mode) or a directory (batch mode)",
    )
    parser.add_argument(
        "--right",
        required=True,
        help="Right path: a JSON file (1v1 mode) or a directory (batch mode)",
    )
    parser.add_argument(
        "--ignore",
        nargs="+",
        default=None,
        help="Paths to ignore under scene_agent_configs.{scene} or tool_context.{scene}, e.g. prompt llm_config.api_key",
    )
    parser.add_argument(
        "--no-highlight",
        action="store_true",
        help="Disable inline character-level diff highlighting",
    )
    args = parser.parse_args()

    ignore_paths = set(args.ignore) if args.ignore else None
    highlight = not args.no_highlight

    left_path = Path(args.left)
    right_path = Path(args.right)

    if not left_path.exists():
        parser.error(f"--left 路径不存在: {left_path.absolute()}")
    if not right_path.exists():
        parser.error(f"--right 路径不存在: {right_path.absolute()}")

    left_is_dir = left_path.is_dir()
    right_is_dir = right_path.is_dir()

    if left_is_dir != right_is_dir:
        parser.error(
            f"--left 和 --right 类型不匹配: "
            f"left={'dir' if left_is_dir else 'file'}, right={'dir' if right_is_dir else 'file'}"
        )

    if left_is_dir:
        # Batch mode
        compare_batch(left_path, right_path, ignore_paths=ignore_paths, highlight=highlight)
    else:
        # 1v1 mode
        diffs = compare_single_scene(left_path, right_path, ignore_paths=ignore_paths)
        print_diff_summary(diffs, str(left_path), str(right_path), left_path.stem, highlight=highlight)


if __name__ == "__main__":
    main()
    '''

      1v1 单文件对比
  python -m map_core.utils.remote_config_compare.scripts.compare_scene_agent \
      --left dumped_configs/ubddev/<ts>/scene_conf/Procurement.json \
      --right dumped_configs/ubdprod/<ts>/scene_conf/Procurement.json \
      --ignore llm_config.api_key
    '''
    '''
  批量全场景对比
  python -m map_core.utils.remote_config_compare.scripts.compare_scene_agent \
      --left dumped_configs/ubddev/<ts>/scene_conf \
      --right dumped_configs/ubdprod/<ts>/scene_conf \
      --ignore llm_config.api_key

    python -m map_core.utils.remote_config_compare.scripts.compare_scene_agent \
        --left map_core/utils/remote_config_compare/dumped_configs/ubddev/<ts>/scene_conf \
        --right map_core/utils/remote_config_compare/dumped_configs/ubdprod/<ts>/scene_conf  \
        --ignore llm_config.api_key wenshu_agent.selected_data_model_ids llm_config.base_url \
        ask_database_agent
    '''
