#!/usr/bin/env python3
"""Step 0 architecture anti-regression gate (TODO 代码精简与可读性改造执行计划 Step 0).

Enforces four non-regression commitments against the committed baseline
(TODO/architecture-baseline.json):

1. changed production functions must not add C901(>12) complexity;
2. F401/F841 old debt must never increase (per project, over the same ruff
   targets the release gate lints);
3. router modules must not add private imports from sibling router modules;
4. direct provider SDK imports (openai) may only appear at locations already
   registered in the baseline (adapter consolidation happens in later steps).

The same module also produces the Step 0 evidence report:
`--mode report` writes import graph, route inventory, SDK import list and
complexity/debt snapshots as JSON.

Usage:
    python3 scripts/architecture_gate.py --repo . --mode check
    python3 scripts/architecture_gate.py --repo . --mode report --out tmp/gate-logs/architecture-report.json
    python3 scripts/architecture_gate.py --self-test
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
C901_THRESHOLD = 12

PROJECTS: dict[str, dict] = {
    "map-business-backend": {
        "root": "map-business-backend",
        "lint_targets": ["app", "tests"],
        "prod_dirs": ["app"],
        "router_dirs": ["app/api"],
    },
    "map_core": {
        "root": "map_core",
        "lint_targets": ["map_core", "tests"],
        "prod_dirs": ["map_core"],
        "router_dirs": ["map_core/routers"],
    },
    "map-observability": {
        "root": "map-observability/map-observability-backend",
        "lint_targets": ["app", "tests"],
        "prod_dirs": ["app"],
        "router_dirs": ["app/routers"],
    },
}

PROVIDER_SDK_ROOTS = {"openai": "OpenAI-compatible provider SDK"}
OBSERVED_SDK_ROOTS = dict(PROVIDER_SDK_ROOTS)
OBSERVED_SDK_ROOTS.update(
    {
        "agentscope": "AgentScope framework SDK",
        "motor": "MongoDB async driver",
        "pymongo": "MongoDB driver",
    }
)

DEFAULT_BASELINE = "TODO/architecture-baseline.json"


class GateError(Exception):
    pass


@dataclass
class Context:
    repo: Path
    baseline_path: Path
    baseline: dict
    baseline_sha: str | None


# ---------------------------------------------------------------- git helpers

def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True
    )
    return result.stdout


def _git_lines(repo: Path, *args: str) -> list[str]:
    raw = _git_bytes(repo, *args).decode("utf-8")
    return [line for line in raw.splitlines() if line]


def repo_git_sha(repo: Path) -> str:
    try:
        return _git_lines(repo, "rev-parse", "HEAD")[0]
    except (subprocess.CalledProcessError, IndexError):
        return "unknown"


def repo_git_branch(repo: Path) -> str:
    try:
        return _git_lines(repo, "branch", "--show-current")[0]
    except (subprocess.CalledProcessError, IndexError):
        return "unknown"


def changed_python_files(repo: Path, baseline_sha: str | None) -> set[str]:
    """Repo-relative .py paths changed in the range under audit.

    FINAL mode (baseline set): baseline..HEAD, committed range.
    Developer mode: uncommitted worktree drift including untracked files.
    """
    paths: set[str] = set()
    if baseline_sha:
        paths.update(
            _git_lines(
                repo,
                "diff",
                "--name-only",
                "--diff-filter=ACMR",
                f"{baseline_sha}",
                "HEAD",
            )
        )
    else:
        paths.update(
            _git_lines(repo, "diff", "--name-only", "--diff-filter=ACMR", "HEAD")
        )
        status = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "-z"],
            capture_output=True,
            check=True,
        ).stdout
        for record in status.split(b"\0"):
            if len(record) < 4:
                continue
            paths.add(record[3:].decode("utf-8"))
    return {p for p in paths if p.endswith(".py")}


def is_in_prod_dirs(path: str, prod_dirs: list[str]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in prod_dirs)


# ---------------------------------------------------------------- ruff data

def run_ruff(
    project_root: Path,
    targets: list[str],
    select: str,
    *,
    max_complexity: int | None = None,
) -> list[dict]:
    cmd = ["uv", "run", "ruff", "check", *targets, "--select", select, "--output-format", "json"]
    if max_complexity is not None:
        cmd += ["--config", f"lint.mccabe.max-complexity = {max_complexity}"]
    result = subprocess.run(
        cmd, cwd=project_root, capture_output=True, text=True, check=False
    )
    if result.returncode not in (0, 1):
        raise GateError(
            f"ruff failed for {project_root} select={select}: {result.stderr.strip()}"
        )
    return json.loads(result.stdout or "[]")


def _normalize_ruff_file(project_root: Path, repo: Path, filename: str) -> str:
    path = Path(filename)
    if not path.is_absolute():
        path = project_root / path
    return str(path.resolve().relative_to(repo))


def ruff_f401_f841_counts(repo: Path, project_root: Path, targets: list[str]) -> dict:
    rows = run_ruff(project_root, targets, "F401,F841")
    counts: dict[str, int] = {}
    files: dict[str, list[dict]] = {}
    for row in rows:
        code = row["code"]
        counts[code] = counts.get(code, 0) + 1
        path = _normalize_ruff_file(project_root, repo, row["filename"])
        files.setdefault(path, []).append(
            {"line": row["location"]["row"], "column": row["location"]["column"], "code": code}
        )
    return {"counts": counts, "files": files}


def ruff_c901_gt(repo: Path, project_root: Path, targets: list[str], threshold: int) -> list[dict]:
    rows = run_ruff(project_root, targets, "C901", max_complexity=threshold)
    out = []
    for row in rows:
        message = row.get("message", "")
        name = message.split("`", 1)[1].split("`", 1)[0] if "`" in message else "?"
        out.append(
            {
                "file": _normalize_ruff_file(project_root, repo, row["filename"]),
                "line": row["location"]["row"],
                "name": name,
                "complexity": _complexity_from_message(message),
            }
        )
    return out


def _complexity_from_message(message: str) -> int:
    try:
        return int(message.rsplit("(", 1)[1].split()[0])
    except (IndexError, ValueError):
        return 0


# ---------------------------------------------------------------- AST scans

def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def module_has_router(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "APIRouter":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "APIRouter":
                return True
    return False


def router_import_edges(router_dir: Path) -> list[dict]:
    py_files = sorted(p for p in router_dir.glob("*.py") if p.name != "__init__.py")
    router_names = {}
    for p in py_files:
        tree = _parse(p)
        if module_has_router(tree):
            router_names[p.stem] = p
    edges: list[dict] = []
    for stem, p in router_names.items():
        tree = _parse(p)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1 and node.module:
                    target = node.module.split(".")[0]
                elif node.level == 0 and node.module:
                    parts = node.module.split(".")
                    target = parts[-1] if parts[0] in ("app", "map_core") else parts[-1]
                else:
                    continue
                if target in router_names and target != stem:
                    edges.append(
                        {
                            "project": str(router_dir.relative_to(router_dir.parents[2])),
                            "importer": str(p.relative_to(router_dir.parents[1])),
                            "target": str(router_names[target].relative_to(router_dir.parents[1])),
                            "line": node.lineno,
                        }
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name.split(".")[-1]
                    if target in router_names and target != stem:
                        edges.append(
                            {
                                "project": str(router_dir.relative_to(router_dir.parents[2])),
                                "importer": str(p.relative_to(router_dir.parents[1])),
                                "target": str(router_names[target].relative_to(router_dir.parents[1])),
                                "line": node.lineno,
                            }
                        )
    return sorted(edges, key=lambda e: (e["importer"], e["target"]))


def sdk_imports(prod_dir: Path, repo: Path, roots: dict[str, str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in sorted(prod_dir.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        tree = _parse(p)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in roots:
                        out.setdefault(top, []).append(
                            {"file": str(p.relative_to(repo)), "symbol": alias.name, "line": node.lineno}
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    top = node.module.split(".")[0]
                    if top in roots:
                        out.setdefault(top, []).append(
                            {"file": str(p.relative_to(repo)), "symbol": node.module, "line": node.lineno}
                        )
    return out


def import_graph(prod_dir: Path, repo: Path) -> list[dict]:
    rows = []
    for p in sorted(prod_dir.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        tree = _parse(p)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({"module": alias.name, "level": 0, "line": node.lineno})
            elif isinstance(node, ast.ImportFrom):
                imports.append(
                    {"module": node.module or "", "level": node.level, "line": node.lineno}
                )
        rows.append({"file": str(p.relative_to(repo)), "imports": imports})
    return rows


def route_inventory(router_dirs: list[Path], repo: Path) -> list[dict]:
    routes = []
    for router_dir in router_dirs:
        for p in sorted(router_dir.glob("*.py")):
            if p.name == "__init__.py":
                continue
            tree = _parse(p)
            routers: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    value = node.value
                    if isinstance(value, ast.Call):
                        func = value.func
                        is_router = (
                            isinstance(func, ast.Name) and func.id == "APIRouter"
                        ) or (isinstance(func, ast.Attribute) and func.attr == "APIRouter")
                        if is_router:
                            for t in targets:
                                if isinstance(t, ast.Name):
                                    routers[t.id] = str(p)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    func = dec.func
                    router_name = None
                    method = None
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        router_name = func.value.id
                        method = func.attr
                    if router_name in routers and method:
                        path = None
                        if dec.args:
                            arg = dec.args[0]
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                path = arg.value
                        routes.append(
                            {
                                "file": str(p.relative_to(repo)),
                                "router": router_name,
                                "method": method.upper(),
                                "path": path,
                                "handler": node.name,
                                "line": node.lineno,
                            }
                        )
    return routes


# ---------------------------------------------------------------- baseline

def default_baseline(repo: Path) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "git_sha": repo_git_sha(repo),
        "f401_f841": {},
        "c901_gt12": {},
        "cross_router_imports": [],
        "direct_sdk_imports": {},
    }


def load_baseline(path: Path) -> dict:
    if not path.exists():
        return default_baseline(path.parent.parent)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise GateError(
            f"baseline {path} schema_version={data.get('schema_version')!r}, expected {SCHEMA_VERSION}"
        )
    return data


# ---------------------------------------------------------------- check mode

def check_f401_f841(current: dict[str, dict], baseline: dict) -> list[str]:
    problems = []
    baseline_counts = baseline.get("f401_f841", {})
    for project, data in sorted(current.items()):
        current_counts = data["counts"]
        old_counts = baseline_counts.get(project, {})
        for code in ("F401", "F841"):
            now = current_counts.get(code, 0)
            old = old_counts.get(code, 0)
            if now > old:
                problems.append(
                    f"{project}: {code} debt increased {old} -> {now}; "
                    "clean the new violation or lower the committed baseline in the same change"
                )
    return problems


def new_c901_violations(
    project: str,
    rows: list[dict],
    baseline_by_file: dict[tuple[str, str], set[str]],
    threshold: int = C901_THRESHOLD,
) -> list[str]:
    """Violations not registered for (project, file) in the baseline."""
    return [
        f"{project}: changed/new function {row['file']}:{row['line']} "
        f"`{row['name']}` has C901={row['complexity']} > {threshold} "
        "and is not registered in the architecture baseline"
        for row in rows
        if row["name"] not in baseline_by_file.get((project, row["file"]), set())
    ]


def check_c901_changed(
    ctx: Context, current: dict[str, list[dict]]
) -> list[str]:
    problems = []
    baseline_by_file: dict[tuple[str, str], set[str]] = {}
    for project, rows in (ctx.baseline.get("c901_gt12") or {}).items():
        for row in rows:
            baseline_by_file.setdefault((project, row["file"]), set()).add(row["name"])
    changed = changed_python_files(ctx.repo, ctx.baseline_sha)
    for project, config in PROJECTS.items():
        prod_changed = [p for p in changed if is_in_prod_dirs(p, config["prod_dirs"])]
        if not prod_changed:
            continue
        project_root = ctx.repo / config["root"]
        prefix = config["root"] + "/"
        local_targets = [
            p[len(prefix):] if p.startswith(prefix) else p for p in prod_changed
        ]
        rows = ruff_c901_gt(ctx.repo, project_root, local_targets, C901_THRESHOLD)
        problems += new_c901_violations(project, rows, baseline_by_file)
    return problems


def check_cross_router(
    ctx: Context, current: list[dict]
) -> list[str]:
    baseline = {
        (e.get("project"), e["importer"], e["target"])
        for e in ctx.baseline.get("cross_router_imports", [])
    }
    problems = []
    for edge in current:
        key = (edge.get("project"), edge["importer"], edge["target"])
        if key not in baseline:
            problems.append(
                f"new private cross-router import: {edge['importer']} -> {edge['target']} "
                f"(line {edge['line']})"
            )
    return problems


def check_direct_sdk(
    ctx: Context, current: dict[str, list[dict]]
) -> list[str]:
    problems = []
    baseline_locations: dict[str, set[str]] = {}
    for provider, rows in (ctx.baseline.get("direct_sdk_imports") or {}).items():
        baseline_locations[provider] = {row["file"] for row in rows}
    current = {k: v for k, v in current.items() if k in PROVIDER_SDK_ROOTS}
    for provider, rows in sorted(current.items()):
        for row in rows:
            if row["file"] not in baseline_locations.get(provider, set()):
                problems.append(
                    f"new direct SDK import: {row['file']}:{row['line']} "
                    f"imports {row['symbol']} ({PROVIDER_SDK_ROOTS.get(provider, provider)})"
                )
    return problems


def collect(repo: Path) -> dict:
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": repo_git_sha(repo),
        "branch": repo_git_branch(repo),
        "projects": {},
        "cross_router_imports": [],
        "direct_sdk_imports": {},
        "routes": [],
        "import_graph": {},
    }
    for project, config in PROJECTS.items():
        root = repo / config["root"]
        ruff_paths = config["lint_targets"]
        fdata = ruff_f401_f841_counts(repo, root, ruff_paths)
        c901 = ruff_c901_gt(repo, root, config["prod_dirs"], C901_THRESHOLD)
        out["projects"][project] = {
            "root": config["root"],
            "f401_f841": fdata,
            "c901_gt12": c901,
        }
        for edge in router_import_edges(root / config["router_dirs"][0]):
            out["cross_router_imports"].append(edge)
        for provider, rows in sdk_imports(root / config["prod_dirs"][0], repo, OBSERVED_SDK_ROOTS).items():
            out["direct_sdk_imports"].setdefault(provider, []).extend(rows)
        for rdir in config["router_dirs"]:
            out["routes"].extend(route_inventory([root / rdir], repo))
        for pdir in config["prod_dirs"]:
            out["import_graph"][f"{project}:{pdir}"] = import_graph(root / pdir, repo)
    return out


def run_check(repo: Path, baseline_path: Path, baseline_sha: str | None) -> int:
    baseline = load_baseline(baseline_path)
    ctx = Context(repo=repo, baseline_path=baseline_path, baseline=baseline, baseline_sha=baseline_sha)
    current = collect(repo)
    problems = []
    f401_f841 = {p: v["f401_f841"] for p, v in current["projects"].items()}
    problems += check_f401_f841(f401_f841, baseline)
    problems += check_c901_changed(ctx, {p: v["c901_gt12"] for p, v in current["projects"].items()})
    problems += check_cross_router(ctx, current["cross_router_imports"])
    problems += check_direct_sdk(ctx, current["direct_sdk_imports"])
    if problems:
        print("[architecture-gate] FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("[architecture-gate] PASSED (F401/F841 debt <= baseline, no new C901>12 in changed functions, no new cross-router/SDK imports)")
    return 0


def run_report(repo: Path, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(collect(repo), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[architecture-gate] report written: {out}")
    return 0


def self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        api = root / "api"
        api.mkdir()
        (api / "a.py").write_text(
            "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/a')\ndef a(): ...\n"
        )
        (api / "b.py").write_text(
            "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/b')\ndef b(): ...\n"
        )
        edges = router_import_edges(api)
        assert edges == [], edges
        (api / "b.py").write_text(
            "from fastapi import APIRouter\nfrom .a import a\nrouter = APIRouter()\n"
        )
        edges = router_import_edges(api)
        assert len(edges) == 1 and edges[0]["target"].endswith("a.py"), edges
        (root / "p.py").write_text("from openai import AsyncOpenAI\n")
        sdk = sdk_imports(root, root, {"openai": "x"})
        assert "openai" in sdk, sdk

        assert check_f401_f841(
            {"p": {"counts": {"F401": 2}}},
            {"f401_f841": {"p": {"F401": 1}}},
        ), "debt increase must fail"
        assert not check_f401_f841(
            {"p": {"counts": {"F401": 1}}},
            {"f401_f841": {"p": {"F401": 1}}},
        ), "equal debt must pass"

        assert new_c901_violations(
            "p",
            [{"file": "p/a.py", "line": 1, "name": "new_big", "complexity": 13}],
            {("p", "p/a.py"): {"old_big"}},
        ), "new complex function must fail"
        assert not new_c901_violations(
            "p",
            [{"file": "p/a.py", "line": 1, "name": "old_big", "complexity": 13}],
            {("p", "p/a.py"): {"old_big"}},
        ), "registered complex function must pass"
    print("[architecture-gate] self-test PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--baseline-sha", type=str, default=None)
    parser.add_argument("--mode", choices=["check", "report", "self-test"], default="check")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    if args.mode == "self-test":
        return self_test()
    baseline = repo / (args.baseline or DEFAULT_BASELINE)
    if args.mode == "report":
        out = args.out or repo / "tmp" / "architecture-report.json"
        return run_report(repo, out)
    return run_check(repo, baseline, args.baseline_sha)


if __name__ == "__main__":
    raise SystemExit(main())
