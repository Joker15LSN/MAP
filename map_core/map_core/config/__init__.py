import importlib
import os

from loguru import logger

from .common import *  # noqa: F403

_env_config_loaded = False


def load_actual_config():
    """
    加载实际的环境特定配置。
    此函数应该在主程序设置了 os.environ['ENV'] 之后被调用。
    """
    global _env_config_loaded
    if _env_config_loaded:
        logger.warning("环境配置已加载，跳过重复加载。")
        return

    env_value = os.environ.get("ENV") or "dev"

    module_to_load_name = None
    if env_value == "dev":
        module_to_load_name = ".dev"
    elif env_value == "test":
        module_to_load_name = ".test"
    elif env_value == "pre":
        module_to_load_name = ".pre"
    elif env_value == "prod":
        module_to_load_name = ".prod"
    else:
        raise NotImplementedError(
            f"未实现针对环境 '{env_value}' 的配置。请检查 config 目录下是否有对应的 {env_value}.py 文件，并在 configs.__init__ 处添加映射。"
        )

    if module_to_load_name:
        try:
            env_specific_module = importlib.import_module(
                module_to_load_name, package=__package__
            )

            # 导入模块中的非私有的属性
            for attr_name in dir(env_specific_module):
                if not attr_name.startswith("_"):
                    globals()[attr_name] = getattr(env_specific_module, attr_name)

            globals()["cfg"] = env_specific_module

            _env_config_loaded = True
            logger.info(f"成功为环境 '{env_value}' 加载配置模块: {module_to_load_name}")
        except ImportError as e:
            logger.error(
                f"无法导入环境 '{env_value}' 的配置模块 'config{module_to_load_name}': {e}"
            )
            raise ImportError(
                f"无法导入环境 '{env_value}' 的配置模块 'config{module_to_load_name}' (原始错误: {e})。请确保文件存在且无导入错误。"
            ) from e
