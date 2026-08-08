"""
Dump scene agent configs from different environments into JSON files.

Usage:
    python -m map_core.utils.remote_config_compare.scripts.dump_scene_conf \
        --scenes Procurement Inventory --envs ubddev ubdtest

Each scene + env combination gets its own JSON file under
    map_core/utils/remote_config_compare/dumped_configs/
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from map_core.utils.remote_config_compare.default_consts import (
    SCENE_CODE_2_NAME,
)
from map_core.utils.remote_config_compare.sacne_config_fetch import (
    DEFAULT_ENVS,
    DEFAULT_SCENES,
    afetch_agent_configs_by_refs,
)

DUMP_DIR = Path(__file__).resolve().parent.parent / "dumped_configs"


async def dump_scene(
    scene_code: str,
    env: str,
    timestamp: str,
    *,
    dump_dir: Path = DUMP_DIR,
) -> Path | None:
    """Fetch config for a single scene+env and write to JSON."""
    try:
        result = await afetch_agent_configs_by_refs([scene_code], env=env)
    except Exception as exc:
        print(f"[ERROR] {scene_code} @ {env}: {exc}")
        return None

    env_dir = dump_dir / env / timestamp / "scene_conf"
    env_dir.mkdir(parents=True, exist_ok=True)
    file_path = env_dir / f"{scene_code}.json"

    # result is a SceneAgentConfigFetchResult; dump its dict representation
    payload = result.model_dump() if hasattr(result, "model_dump") else result
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] {scene_code} @ {env} -> {file_path}")
    return file_path


async def generate_scene_selection_config(
    env: str,
    timestamp: str,
    *,
    dump_dir: Path = DUMP_DIR,
) -> Path | None:
    """
    Generate scene_selection.json for a given environment.

    Reads all dumped scene configs and MASTER config to build:
    - scene_selection.big_scene_system_prompt_template  <- from MASTER.prompt
    - scene_selection.sub_scene_user_prompt_template    <- hard-coded template
    - scene_selection.enabled_agent_codes               <- from each scene's description
    - scene_description_versions                        <- from each scene config
    """
    env_scene_dir = dump_dir / env / timestamp / "scene_conf"
    if not env_scene_dir.exists():
        print(f"[WARN] No scene configs found for {env}, run dump first.")
        return None

    # 1. Load MASTER config for big_scene_system_prompt_template
    master_path = env_scene_dir / "MASTER.json"
    master_prompt = ""
    additional_user_prompt = ""
    if master_path.exists():
        with open(master_path, "r", encoding="utf-8") as f:
            master_data = json.load(f)
        master_conf = master_data.get("scene_agent_configs", {}).get("MASTER", {})
        master_prompt = master_conf.get("prompt", "")
        additional_user_prompt = master_conf.get("additional_user_prompt", "")

    # 2. Load all scene configs to build enabled_agent_codes & versions
    enabled_agent_codes: dict[str, dict[str, str]] = {}
    scene_description_versions: dict[str, str] = {}

    for scene_file in sorted(env_scene_dir.glob("*.json")):
        scene_code = scene_file.stem
        with open(scene_file, "r", encoding="utf-8") as f:
            scene_data = json.load(f)

        scene_conf = scene_data.get("scene_agent_configs", {}).get(scene_code, {})
        description = scene_conf.get("description", "")

        enabled_agent_codes[scene_code] = {
            "agent_name": SCENE_CODE_2_NAME.get(scene_code, scene_code),
            "agent_description": description,
        }

        # version from scene_post_summary or default v1
        version = "v1"
        scene_post_summary = scene_conf.get("scene_post_summary", {})
        if isinstance(scene_post_summary, dict):
            version = scene_post_summary.get("version", "v1")
        scene_description_versions[scene_code] = version

    # 3. Build output structure
    output = {
        "scene_selection": {
            "big_scene_system_prompt_template": master_prompt,
            "sub_scene_user_prompt_template": additional_user_prompt,
            "enabled_agent_codes": enabled_agent_codes,
        },
        "scene_description_versions": scene_description_versions,
        "meta": {
            "note": f"Generated from {env} scene configs"
        },
    }

    # 4. Write output
    env_dir = dump_dir / env / timestamp
    env_dir.mkdir(parents=True, exist_ok=True)
    out_path = env_dir / "scene_selection.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[OK] scene_selection @ {env} -> {out_path}")
    return out_path


async def main(
    scenes: list[str] | None = None,
    envs: list[str] | None = None,
    generate_selection: bool = True,
) -> None:
    scenes = scenes or DEFAULT_SCENES
    envs = envs or DEFAULT_ENVS

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Step 1: dump individual scene configs
    tasks = [
        dump_scene(scene, env, timestamp)
        for env in envs
        for scene in scenes
    ]
    await asyncio.gather(*tasks)

    # Step 2: generate scene_selection.json per env
    if generate_selection:
        for env in envs:
            await generate_scene_selection_config(env, timestamp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dump scene configs from remote envs")
    parser.add_argument(
        "--scenes",
        nargs="+",
        default=None,
        help=f"Scene codes to dump (default: {DEFAULT_SCENES})",
    )
    parser.add_argument(
        "--envs",
        nargs="+",
        default=None,
        help=f"Environments to query (default: {DEFAULT_ENVS})",
    )
    parser.add_argument(
        "--no-selection",
        action="store_true",
        help="Skip generating scene_selection.json",
    )
    args = parser.parse_args()

    asyncio.run(main(
        scenes=args.scenes,
        envs=args.envs,
        generate_selection=not args.no_selection,
    ))

    '''
    
     python -m map_core.utils.remote_config_compare.scripts.dump_scene_conf
    '''
