"""DomainとEngineの依存方向を固定するContract Test。"""

import ast
from pathlib import Path

import pytest


@pytest.mark.contract
def test_domain_and_engine_do_not_import_external_adapters() -> None:
    """純粋層からLLM、DB、Web frameworkへの依存を拒否する。"""

    forbidden = {
        "fastapi",
        "sqlalchemy",
        "psycopg",
        "ai_rpg.llm",
        "ai_rpg.infrastructure",
        "ai_rpg.api",
    }
    violations: list[str] = []
    for package in ("domain", "engine"):
        for path in Path(f"src/ai_rpg/{package}").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = (alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names = (node.module or "",)
                else:
                    continue
                for name in names:
                    if any(name == item or name.startswith(f"{item}.") for item in forbidden):
                        violations.append(f"{path}:{node.lineno}: {name}")
    assert violations == []
