"""Hermes-style Plugin discovery and registration.

Plugins own trusted executable capabilities. Skills remain readable workflow
knowledge and are progressively loaded through SkillManager.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .skills.skill_manager import SkillManager
from .tools.registry import ToolEntry, ToolRegistry


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str
    path: Path
    module: str
    entrypoint: str = "register"
    enabled_by_default: bool = False
    trusted: bool = False


@dataclass
class PluginDiagnostic:
    plugin_id: str
    status: str
    message: str = ""
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


class PluginContext:
    """Narrow dependency-injection surface exposed to a trusted Plugin."""

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        registry: ToolRegistry,
        skill_manager: SkillManager,
        runtime: dict[str, Any],
    ):
        self.manifest = manifest
        self.registry = registry
        self.skill_manager = skill_manager
        self.runtime = runtime
        self.registered_tools: list[str] = []
        self.registered_skills: list[str] = []
        self.hooks: dict[str, list[Callable[..., Any]]] = {}

    @property
    def config(self) -> dict[str, Any]:
        value = self.runtime.get("plugin_config", {}).get(self.manifest.plugin_id, {})
        return dict(value) if isinstance(value, dict) else {}

    def register_tool(self, entry: ToolEntry) -> None:
        self.registry.register(replace(entry, source=f"plugin:{self.manifest.plugin_id}"))
        self.registered_tools.append(entry.name)

    def register_tools(self, entries: Iterable[ToolEntry]) -> None:
        for entry in entries:
            self.register_tool(entry)

    def register_skill(
        self,
        name: str,
        relative_path: str,
        *,
        aliases: Iterable[str] = (),
    ) -> None:
        canonical = f"{self.manifest.plugin_id}:{name}"
        skill_path = (self.manifest.path / relative_path).resolve()
        if self.manifest.path.resolve() not in skill_path.parents:
            raise ValueError("Plugin Skill path escapes the Plugin directory")
        self.skill_manager.register_skill(
            canonical,
            skill_path,
            source=f"plugin:{self.manifest.plugin_id}",
            aliases=aliases,
            read_only=True,
        )
        self.registered_skills.append(canonical)

    def register_hook(self, event: str, callback: Callable[..., Any]) -> None:
        self.hooks.setdefault(str(event), []).append(callback)


class PluginManager:
    def __init__(
        self,
        *,
        skill_manager: SkillManager,
        runtime: dict[str, Any],
        builtin_dir: Path | str | None = None,
        user_dir: Path | str | None = None,
        project_dir: Path | str | None = None,
        enabled: Iterable[str] = (),
        disabled: Iterable[str] = (),
    ):
        self.skill_manager = skill_manager
        self.runtime = runtime
        self.builtin_dir = Path(builtin_dir or Path(__file__).resolve().parents[1] / "plugins")
        self.user_dir = Path(user_dir).expanduser() if user_dir else None
        self.project_dir = Path(project_dir).expanduser() if project_dir else None
        self.enabled = {str(value) for value in enabled}
        self.disabled = {str(value) for value in disabled}
        self.diagnostics: list[PluginDiagnostic] = []
        self._discovery_diagnostics: list[PluginDiagnostic] = []
        self._entrypoints: dict[str, Any] = {}
        self.hooks: dict[str, list[Callable[..., Any]]] = {}

    def discover(self) -> list[PluginManifest]:
        manifests: dict[str, PluginManifest] = {}
        self._discovery_diagnostics = []
        for root, trusted in (
            (self.builtin_dir, True),
            (self.user_dir, False),
            (self.project_dir, False),
        ):
            if root is None or not root.exists():
                continue
            for path in sorted(root.glob("*/plugin.yaml")):
                try:
                    manifest = self._read_manifest(path, trusted=trusted)
                except Exception as exc:
                    self._discovery_diagnostics.append(
                        PluginDiagnostic(path.parent.name, "failed", str(exc))
                    )
                    continue
                if manifest.plugin_id in manifests:
                    self._discovery_diagnostics.append(
                        PluginDiagnostic(
                            manifest.plugin_id,
                            "failed",
                            f"Duplicate Plugin id: {manifest.plugin_id}",
                        )
                    )
                    continue
                manifests[manifest.plugin_id] = manifest
        try:
            entrypoints = importlib.metadata.entry_points(
                group="qgis_hermes_agent.plugins"
            )
        except TypeError:  # Python 3.10 compatibility
            entrypoints = importlib.metadata.entry_points().select(
                group="qgis_hermes_agent.plugins"
            )
        for entrypoint in entrypoints:
            plugin_id = str(entrypoint.name)
            if plugin_id in manifests:
                self._discovery_diagnostics.append(
                    PluginDiagnostic(plugin_id, "failed", f"Duplicate Plugin id: {plugin_id}")
                )
                continue
            distribution_root = Path.cwd()
            if entrypoint.dist is not None:
                distribution_root = Path(str(entrypoint.dist.locate_file("")))
            manifests[plugin_id] = PluginManifest(
                plugin_id=plugin_id,
                version=str(entrypoint.dist.version if entrypoint.dist else "0.0.0"),
                path=distribution_root,
                module=f"@entrypoint:{plugin_id}",
                enabled_by_default=False,
                trusted=False,
            )
            self._entrypoints[plugin_id] = entrypoint
        return list(manifests.values())

    def register_all(self, registry: ToolRegistry) -> list[PluginDiagnostic]:
        manifests = self.discover()
        self.diagnostics = list(self._discovery_diagnostics)
        for manifest in manifests:
            if manifest.plugin_id in self.disabled or (
                not manifest.enabled_by_default and manifest.plugin_id not in self.enabled
            ):
                self.diagnostics.append(PluginDiagnostic(manifest.plugin_id, "disabled"))
                continue
            context = PluginContext(
                manifest=manifest,
                registry=registry,
                skill_manager=self.skill_manager,
                runtime=self.runtime,
            )
            try:
                if manifest.module.startswith("@entrypoint:"):
                    loaded = self._entrypoints[manifest.plugin_id].load()
                    register = loaded if callable(loaded) else getattr(
                        loaded, manifest.entrypoint
                    )
                elif manifest.module.startswith("@file:"):
                    relative_file = manifest.module.split(":", 1)[1]
                    module_path = (manifest.path / relative_file).resolve()
                    if manifest.path.resolve() not in module_path.parents:
                        raise ValueError("Plugin entrypoint escapes its directory")
                    module_name = f"_qgis_hermes_plugin_{manifest.plugin_id.replace('-', '_')}"
                    spec = importlib.util.spec_from_file_location(module_name, module_path)
                    if spec is None or spec.loader is None:
                        raise ImportError(f"Cannot load Plugin module: {module_path}")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    register = getattr(module, manifest.entrypoint)
                else:
                    module = importlib.import_module(self._qualified_module(manifest.module))
                    register = getattr(module, manifest.entrypoint)
                register(context)
            except Exception as exc:
                self.diagnostics.append(
                    PluginDiagnostic(manifest.plugin_id, "failed", str(exc))
                )
                continue
            for event, callbacks in context.hooks.items():
                self.hooks.setdefault(event, []).extend(callbacks)
            self.diagnostics.append(
                PluginDiagnostic(
                    manifest.plugin_id,
                    "loaded",
                    tools=context.registered_tools,
                    skills=context.registered_skills,
                )
            )
        return list(self.diagnostics)

    def emit_hook(self, event: str, **payload: Any) -> list[str]:
        errors = []
        for callback in self.hooks.get(event, []):
            try:
                callback(**payload)
            except Exception as exc:
                errors.append(str(exc))
        return errors

    def build_diagnostics_tool(self) -> ToolEntry:
        def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            del arguments
            plugins = [
                {
                    "plugin_id": item.plugin_id,
                    "status": item.status,
                    "message": item.message,
                    "tools": item.tools,
                    "skills": item.skills,
                }
                for item in self.diagnostics
            ]
            return {"success": True, "plugins": plugins, "count": len(plugins)}

        return ToolEntry(
            name="list_plugins",
            description="列出已发现 Plugin 的启用、加载、失败状态及其注册能力。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler,
            category="skill",
            source="core:plugin-manager",
        )

    def attach_hooks(self, registry: ToolRegistry) -> None:
        def before(name: str, arguments: dict[str, Any]) -> None:
            self.emit_hook("before_tool", name=name, arguments=arguments)

        def after(
            name: str,
            arguments: dict[str, Any],
            result: dict[str, Any],
        ) -> None:
            self.emit_hook(
                "after_tool", name=name, arguments=arguments, result=result
            )

        registry.set_execution_hooks(
            before=before,
            after=after,
        )

    @staticmethod
    def _read_manifest(path: Path, *, trusted: bool) -> PluginManifest:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid Plugin manifest: {path}")
        plugin_id = str(raw.get("id") or path.parent.name).strip()
        raw_module = str(raw.get("module") or "").strip()
        entrypoint_file = str(raw.get("entrypoint_file") or "plugin.py").strip()
        module = raw_module or (
            f"@file:{entrypoint_file}"
            if (path.parent / entrypoint_file).is_file()
            else f"plugins.{plugin_id}.plugin"
        )
        if not plugin_id or not module:
            raise ValueError(f"Plugin manifest misses id/module: {path}")
        return PluginManifest(
            plugin_id=plugin_id,
            version=str(raw.get("version") or "0.0.0"),
            path=path.parent,
            module=module,
            entrypoint=str(raw.get("entrypoint") or "register"),
            enabled_by_default=trusted and bool(raw.get("enabled_by_default", False)),
            trusted=trusted and bool(raw.get("trusted", True)),
        )

    @staticmethod
    def _qualified_module(module: str) -> str:
        package = __package__ or "backend"
        root = package.rsplit(".backend", 1)[0] if ".backend" in package else ""
        if module.startswith("."):
            return f"{root}{module}" if root else module.lstrip(".")
        if module.startswith("plugins.") and root:
            return f"{root}.{module}"
        return module
