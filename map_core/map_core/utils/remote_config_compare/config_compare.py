from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass
class DiffResult:
    """单个字段的差异结果"""

    path: str
    left: Any
    right: Any
    is_equal: bool
    children: list[DiffResult] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.is_equal


class ConfigComparator:
    """配置比较器：支持 dict / BaseModel 的嵌套比较，支持按 path 过滤。"""

    def __init__(self, ignore_paths: set[str] | None = None):
        """
        Args:
            ignore_paths: 忽略的路径集合，格式如 {"aa.bb.cc", "xx.yy"}
        """
        self.ignore_paths = ignore_paths or set()

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def compare(
        self,
        left: dict | BaseModel,
        right: dict | BaseModel,
        *,
        path: str = "",
        only_path: str | None = None,
    ) -> DiffResult:
        """
        比较两个对象。

        Args:
            left, right: 待比较的对象（dict 或 BaseModel）
            path: 当前路径前缀（内部递归使用）
            only_path: 只比较该路径下的字段，如 "scene_agent_configs.Procurement"
        """
        left_dict = self._to_dict(left)
        right_dict = self._to_dict(right)

        # 如果指定了 only_path，先裁剪到目标子树
        if only_path:
            left_dict = self._get_by_path(left_dict, only_path)
            right_dict = self._get_by_path(right_dict, only_path)
            path = only_path

        return self._compare_dict(left_dict, right_dict, path)

    def find_diffs(self, result: DiffResult) -> list[DiffResult]:
        """展平收集所有不相等的叶子节点。"""
        diffs: list[DiffResult] = []
        self._collect_diffs(result, diffs)
        return diffs

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _to_dict(self, obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        return obj

    def _get_by_path(self, obj: dict, dot_path: str) -> Any:
        """按 aa.bb.cc 格式从 dict 中取子树。"""
        keys = dot_path.split(".")
        cur = obj
        for key in keys:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return None
        return cur

    def _is_ignored(self, full_path: str) -> bool:
        """判断路径是否被忽略（支持前缀匹配）。"""
        if full_path in self.ignore_paths:
            return True
        # 如果某个 ignore_path 是当前路径的前缀，也忽略
        for ip in self.ignore_paths:
            if full_path.startswith(ip + "."):
                return True
        return False

    def _compare_dict(self, left: Any, right: Any, path: str) -> DiffResult:
        # 1) 类型不同 → 直接不等
        if type(left) is not type(right):
            return DiffResult(path=path, left=left, right=right, is_equal=False)

        # 2) 都不是 dict → 值比较
        if not isinstance(left, dict):
            return DiffResult(
                path=path,
                left=left,
                right=right,
                is_equal=left == right,
            )

        # 3) 都是 dict → 递归比较 key
        children: list[DiffResult] = []
        all_keys = sorted(set(left.keys()) | set(right.keys()))

        for key in all_keys:
            full_path = f"{path}.{key}" if path else str(key)

            if self._is_ignored(full_path):
                continue

            if key not in left:
                children.append(
                    DiffResult(
                        path=full_path,
                        left=None,
                        right=right[key],
                        is_equal=False,
                    )
                )
            elif key not in right:
                children.append(
                    DiffResult(
                        path=full_path,
                        left=left[key],
                        right=None,
                        is_equal=False,
                    )
                )
            else:
                children.append(
                    self._compare_dict(left[key], right[key], full_path)
                )

        is_equal = all(c.is_equal for c in children)
        return DiffResult(
            path=path,
            left=left,
            right=right,
            is_equal=is_equal,
            children=children,
        )

    def _collect_diffs(self, result: DiffResult, out: list[DiffResult]) -> None:
        if not result.is_equal:
            if not result.children:
                out.append(result)
            else:
                for child in result.children:
                    self._collect_diffs(child, out)


# ---------------------------------------------------------------------- #
# 便捷函数
# ---------------------------------------------------------------------- #
def compare_configs(
    left: dict | BaseModel,
    right: dict | BaseModel,
    *,
    only_path: str | None = None,
    ignore_paths: set[str] | None = None,
) -> DiffResult:
    """一次性比较，返回 DiffResult。"""
    return ConfigComparator(ignore_paths=ignore_paths).compare(
        left, right, only_path=only_path
    )


def has_diff(
    left: dict | BaseModel,
    right: dict | BaseModel,
    *,
    only_path: str | None = None,
    ignore_paths: set[str] | None = None,
) -> bool:
    """判断是否存在差异。"""
    result = compare_configs(left, right, only_path=only_path, ignore_paths=ignore_paths)
    return not result.is_equal


def print_diffs(result: DiffResult) -> None:
    """打印所有差异字段。"""
    diffs = ConfigComparator().find_diffs(result)
    if not diffs:
        print("✅ 无差异")
        return
    for d in diffs:
        print(f"❌ [{d.path}]")
        print(f"   left : {d.left}")
        print(f"   right: {d.right}")
