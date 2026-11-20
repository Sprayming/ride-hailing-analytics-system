"""指标目录：模型能看到什么、能选什么，全部由这份 JSON 决定。

对照小哲电商的 Catalog.java：
- 静态加载 semantic-model.json；
- 通过 read_catalog 工具把「指标 + 维度 + 表 + 关系 + 歧义词」交给模型；
- 模型看不到原始 schema，也看不到自由 SQL 的入口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

RESOURCE_DIR = Path(__file__).resolve().parent / "resources"


def load_resource(name: str) -> str:
    """读取 resources 下的文本资源（policy / schema / catalog）。"""
    return (RESOURCE_DIR / name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Catalog:
    """一份不可变的指标目录。"""

    models: Dict[str, Dict[str, Any]]
    relationships: List[str]
    metrics: Dict[str, Dict[str, Any]]
    ambiguous_terms: Dict[str, str]
    dimension_definitions: Dict[str, str]

    @classmethod
    def load(cls, path: Path | None = None) -> "Catalog":
        payload = json.loads((path or RESOURCE_DIR / "semantic-model.json").read_text(encoding="utf-8"))
        return cls(
            models=payload.get("models", {}),
            relationships=payload.get("relationships", []),
            metrics=payload.get("metrics", {}),
            ambiguous_terms=payload.get("ambiguousTerms", {}),
            dimension_definitions=payload.get("dimensionDefinitions", {}),
        )

    def read_catalog(self) -> str:
        """返回给模型的目录视图。

        刻意不暴露 source / expression / condition——模型只需要知道
        「有哪些指标、支持哪些维度、涉及哪些表」，SQL 表达式由编译器独占。
        """
        definitions = {}
        for key, metric in self.metrics.items():
            definitions[key] = {
                "name": metric.get("name"),
                "aliases": metric.get("aliases", []),
                "description": metric.get("description"),
                "dimensions": sorted(metric.get("dimensions", [])),
                "tables": sorted(metric.get("tables", [])),
                "format": metric.get("format"),
            }
        return json.dumps(
            {
                "models": self.models,
                "relationships": self.relationships,
                "metrics": definitions,
                "ambiguousTerms": self.ambiguous_terms,
                "dimensionDefinitions": self.dimension_definitions,
            },
            ensure_ascii=False,
            indent=2,
        )

    def metric(self, name: str | None) -> Dict[str, Any] | None:
        return self.metrics.get(name) if name else None


CATALOG = Catalog.load()
