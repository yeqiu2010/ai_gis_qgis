"""Searchable catalog for QGIS Processing toolbox documentation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProcessingToolSpec:
    tool_id: str
    name: str
    description: str
    parameters: str
    code_example: str
    domain: str
    provider: str

    def summary(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "domain": self.domain,
            "provider": self.provider,
            "description": self.description[:600],
        }

    def detail(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "domain": self.domain,
            "provider": self.provider,
            "description": self.description,
            "parameters": self.parameters,
            "code_example": self.code_example,
        }


DOMAIN_DEFINITIONS: dict[str, dict[str, Any]] = {
    "qgis-vector-selection": {
        "description": "属性筛选、表达式筛选、按位置选择或提取矢量要素。",
        "keywords": ["extract", "select", "filter", "expression", "attribute", "location", "筛选", "选择", "提取", "属性", "表达式", "位置"],
    },
    "qgis-vector-geometry": {
        "description": "缓冲、质心、凸包、简化、平滑、几何转换、线面点几何处理。",
        "keywords": ["buffer", "centroid", "convex", "geometry", "simplify", "smooth", "polygon", "line", "point", "缓冲", "质心", "几何", "简化", "平滑"],
    },
    "qgis-vector-overlay": {
        "description": "裁剪、相交、联合、差集、擦除等矢量叠加分析。",
        "keywords": ["clip", "intersection", "intersect", "union", "difference", "dissolve", "overlay", "erase", "裁剪", "相交", "叠加", "联合", "差集", "融合"],
    },
    "qgis-vector-table": {
        "description": "字段计算、字段管理、属性连接、统计汇总、聚合。",
        "keywords": ["field", "attribute", "join", "statistics", "aggregate", "table", "字段", "属性", "连接", "统计", "汇总", "聚合"],
    },
    "qgis-raster-terrain": {
        "description": "坡度、坡向、山体阴影、粗糙度、TPI、TRI、视域等地形分析。",
        "keywords": ["slope", "aspect", "hillshade", "roughness", "terrain", "tpi", "tri", "viewshed", "坡度", "坡向", "山体阴影", "地形", "视域"],
    },
    "qgis-raster-analysis": {
        "description": "栅格计算、邻域分析、重分类、NoData 处理、栅格统计。",
        "keywords": ["raster calculator", "mapcalc", "reclass", "nodata", "neighbors", "栅格计算", "重分类", "邻域", "空值"],
    },
    "qgis-raster-conversion": {
        "description": "栅格裁剪、重投影、格式转换、矢量化、栅格化、拼接。",
        "keywords": ["rasterize", "polygonize", "warp", "translate", "merge", "clip raster", "栅格化", "矢量化", "重投影", "格式转换", "拼接"],
    },
    "qgis-hydrology": {
        "description": "流域、流向、汇流、河网提取、水文填洼等水文分析。",
        "keywords": ["watershed", "stream", "drain", "basin", "hydrology", "flow", "流域", "河网", "水文", "汇流", "流向"],
    },
    "qgis-interpolation": {
        "description": "IDW、最近邻、线性插值、网格化等插值分析。",
        "keywords": ["grid", "interpolation", "inverse distance", "nearest", "linear", "idw", "插值", "网格"],
    },
    "qgis-data-management": {
        "description": "数据转换、修复、合并、索引、投影定义、信息读取等数据管理。",
        "keywords": ["convert", "fix", "merge", "info", "projection", "index", "import", "export", "转换", "修复", "合并", "投影", "导入", "导出"],
    },
    "qgis-cartography": {
        "description": "制图、瓦片、颜色表、等值线、专题表达相关工具。",
        "keywords": ["tiles", "color", "contour", "relief", "style", "map", "瓦片", "颜色", "等值线", "制图", "专题图"],
    },
    "qgis-3d": {
        "description": "三维、Z 值、三角剖分和 3D 几何处理。",
        "keywords": ["3d", "z", "tessellate", "tin", "三维", "剖分"],
    },
    "qgis-provider-specialized": {
        "description": "GRASS/GDAL/SAGA 等 provider 中较专门、难以归入其他 domain 的算法。",
        "keywords": [],
    },
}

QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "筛选": ("attribute filter", "extract by expression"),
    "查找": ("select", "extract", "filter"),
    "选出": ("select", "extract by expression"),
    "属性过滤": ("attribute filter", "extract by expression"),
    "表达式": ("expression", "extract by expression", "field calculator"),
    "周边": ("buffer", "distance"),
    "服务半径": ("buffer", "distance", "dissolve", "native:buffer"),
    "影响范围": ("buffer", "distance", "dissolve"),
    "缓冲": ("buffer", "distance", "dissolve"),
    "范围内": ("within", "contains", "extract by location"),
    "位于": ("within", "contains", "extract by location"),
    "落在": ("within", "contains", "spatial join", "extract by location"),
    "重叠": ("intersects", "intersection", "overlay"),
    "相交": ("intersects", "intersection", "overlay"),
    "交叉": ("intersects", "intersection", "overlay"),
    "裁剪": ("clip", "overlay"),
    "截取": ("clip", "extract"),
    "融合": ("dissolve", "aggregate"),
    "消除内部边界": ("dissolve",),
    "空间连接": ("spatial join", "join attributes by location", "joinbylocationsummary"),
    "空间挂接": ("spatial join", "join attributes by location", "joinbylocationsummary"),
    "归属街道": ("spatial join", "join attributes by location", "joinbylocationsummary"),
    "归属地块": ("spatial join", "join attributes by location", "joinbylocationsummary"),
    "面积": ("area", "field calculator", "geometry"),
    "覆盖率": ("intersection area", "area ratio", "field calculator", "fieldcalculator", "aggregate"),
    "密度": ("area ratio", "field calculator", "fieldcalculator", "aggregate"),
    "字段": ("field", "attribute", "field calculator"),
    "添加字段": ("field calculator", "fieldcalculator", "add field", "expression"),
    "更新字段": ("field calculator", "fieldcalculator", "expression"),
    "计算": ("calculate", "calculator", "expression"),
    "平方千米": ("square kilometer", "area"),
    "平方公里": ("square kilometer", "area"),
    "长度": ("length", "geometry", "field calculator"),
    "周长": ("perimeter", "geometry", "field calculator"),
    "连接": ("join", "join attributes"),
    "按位置连接": ("spatial join", "join attributes by location"),
    "统计": ("statistics", "aggregate", "group by"),
    "分类统计": ("statistics by categories", "statisticsbycategories", "aggregate", "group by"),
    "按街道统计": ("statistics by categories", "statisticsbycategories", "aggregate", "group by"),
    "汇总": ("statistics", "aggregate", "group by"),
    "坐标系": ("reproject layer", "projection", "CRS"),
    "统一坐标系": ("reproject layer", "reprojectlayer", "projected CRS"),
    "重投影": ("reproject layer", "reprojectlayer", "warp"),
    "修复几何": ("fix geometries", "make valid"),
    "无效几何": ("fix geometries", "make valid"),
    "合并图层": ("merge vector layers", "merge"),
    "拼接": ("merge", "mosaic"),
    "栅格转矢量": ("polygonize", "raster to vector"),
    "矢量转栅格": ("rasterize", "vector to raster"),
    "坡度": ("slope", "terrain"),
    "坡向": ("aspect", "terrain"),
    "山体阴影": ("hillshade", "terrain"),
    "插值": ("interpolation", "IDW", "TIN"),
    "流域": ("watershed", "basin", "hydrology"),
    "流向": ("flow direction", "hydrology"),
}


class QGISToolboxCatalog:
    def __init__(self, catalog_path: Path | str | None = None):
        root = Path(__file__).resolve().parents[2]
        self.catalog_path = Path(catalog_path) if catalog_path else root / "resources" / "qgis_toolbox" / "catalog.json"
        self._tools = self._load_tools()

    @property
    def tools(self) -> list[ProcessingToolSpec]:
        return list(self._tools)

    def domains(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for tool in self._tools:
            counts[tool.domain] = counts.get(tool.domain, 0) + 1
        return [
            {
                "domain": name,
                "description": definition["description"],
                "tool_count": counts.get(name, 0),
                "keywords": definition["keywords"][:12],
            }
            for name, definition in DOMAIN_DEFINITIONS.items()
            if counts.get(name, 0) or name != "qgis-provider-specialized"
        ]

    def get(self, tool_id: str) -> ProcessingToolSpec | None:
        normalized = str(tool_id or "").strip().lower()
        for tool in self._tools:
            if tool.tool_id.lower() == normalized:
                return tool
        return None

    def search_domains(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        scored = []
        for domain in self.domains():
            haystack = f"{domain['domain']} {domain['description']} {' '.join(domain['keywords'])}"
            score = _score(query_tokens, haystack)
            if score > 0:
                scored.append({**domain, "score": score})
        return sorted(scored, key=lambda item: item["score"], reverse=True)[: max(1, limit)]

    def search_tools(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        query_tokens = _tokens(_expand_query(query))
        candidates = [tool for tool in self._tools if not domain or tool.domain == domain]
        scored = []
        for tool in candidates:
            haystack = f"{tool.tool_id} {tool.name} {tool.description} {tool.parameters}"
            score = _score(query_tokens, haystack)
            normalized_query = re.sub(r"[^0-9a-z]+", "", query.lower())
            normalized_name = re.sub(r"[^0-9a-z]+", "", tool.name.lower())
            if normalized_query and normalized_query == normalized_name:
                score += 20
            if domain and tool.domain == domain:
                score += 1
            if score > 0:
                scored.append({**tool.summary(), "score": score})
        return sorted(scored, key=lambda item: item["score"], reverse=True)[: max(1, limit)]

    def search_tools_many(
        self,
        queries: list[str],
        *,
        domain: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for query in queries:
            for result in self.search_tools(query, domain=domain, limit=limit):
                tool_id = result["tool_id"]
                existing = merged.get(tool_id)
                if existing is None:
                    merged[tool_id] = {**result, "matched_queries": [query]}
                    continue
                if query not in existing["matched_queries"]:
                    existing["matched_queries"].append(query)
                existing["score"] += result["score"]
        ranked = sorted(
            merged.values(),
            key=lambda item: (item["score"], len(item["matched_queries"])),
            reverse=True,
        )
        return ranked[: max(1, limit)]

    def _load_tools(self) -> list[ProcessingToolSpec]:
        if not self.catalog_path.exists():
            return []
        raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        tools = []
        for item in raw if isinstance(raw, list) else []:
            tool_id = str(item.get("tool_id") or "").strip()
            if not tool_id:
                continue
            description = str(item.get("tool_description") or "")
            parameters = str(item.get("parameters") or "")
            name = str(item.get("toolname") or tool_id)
            tools.append(
                ProcessingToolSpec(
                    tool_id=tool_id,
                    name=name,
                    description=description,
                    parameters=parameters,
                    code_example=str(item.get("code_example") or ""),
                    domain=_classify_tool(tool_id, name, description, parameters),
                    provider=tool_id.split(":", 1)[0] if ":" in tool_id else "",
                )
            )
        return tools


@lru_cache(maxsize=1)
def default_catalog() -> QGISToolboxCatalog:
    return QGISToolboxCatalog()


def _classify_tool(tool_id: str, name: str, description: str, parameters: str) -> str:
    text = f"{tool_id} {name} {description} {parameters}".lower()
    if tool_id.startswith("3d:") or "tessellate" in text:
        return "qgis-3d"
    terrain_ids = {
        "gdal:slope",
        "gdal:aspect",
        "gdal:hillshade",
        "gdal:roughness",
        "gdal:tpitopographicpositionindex",
        "gdal:triterrainruggednessindex",
        "gdal:viewshed",
    }
    if tool_id in terrain_ids or tool_id.endswith(":r.slope.aspect"):
        return "qgis-raster-terrain"
    best_domain = "qgis-provider-specialized"
    best_score = 0
    for domain, definition in DOMAIN_DEFINITIONS.items():
        if domain == "qgis-provider-specialized":
            continue
        score = sum(1 for keyword in definition["keywords"] if keyword.lower() in text)
        if score > best_score:
            best_domain = domain
            best_score = score
    return best_domain


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.split(r"[^0-9A-Za-z_\u4e00-\u9fff]+", str(text).lower())
        if len(token) >= 2
    ]


def _expand_query(query: str) -> str:
    aliases = [
        alias
        for phrase, expansions in QUERY_ALIASES.items()
        if phrase in query
        for alias in expansions
    ]
    return " ".join([query, *aliases])


def _score(tokens: list[str], haystack: str) -> int:
    haystack = haystack.lower()
    score = 0
    for token in tokens:
        if token in haystack:
            score += 3 if len(token) > 3 else 1
    return score
