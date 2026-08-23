# CODE ENGINE TOOL MANAGER — registry for Code Engine's OWN development
# tools (project_analyzer/file_system/code_search/editor/git). Completely
# independent from core.module_manager.ModuleManager, which manages LIRA's
# own runnable skills/ capability modules — different registry, different
# file (data/code_engine_permissions.json vs data/modules.json), different
# purpose. Neither imports the other.
#
# CodeEngineTool itself lives in tool_base.py, not here — see that file's
# own comment for why (this module eagerly imports every tool class below,
# and every tool class needs CodeEngineTool; importing it from HERE would
# be circular the moment a tool module is loaded on its own).
import logging

from core.code_engine.tool_base import CodeEngineTool   # re-exported for convenience

logger = logging.getLogger("code_engine")


class CodeEngineToolManager:
    def __init__(self) -> None:
        self._tools: dict = {}
        self._register_all()

    def _register_all(self) -> None:
        from core.code_engine.tools.project_analyzer import ProjectAnalyzer
        from core.code_engine.tools.file_system import FileSystem
        from core.code_engine.tools.code_search import CodeSearch
        from core.code_engine.tools.editor import Editor
        from core.code_engine.tools.git import Git
        from core.code_engine.tools.shell import Shell
        from core.code_engine.tools.dependency_manager import DependencyManager
        from core.code_engine.tools.testing import Testing
        from core.code_engine.tools.debugger import Debugger
        from core.code_engine.tools.checkpoint_manager import CheckpointManager
        from core.code_engine.tools.planner import Planner
        from core.code_engine.tools.orchestrator import ToolOrchestrator
        from core.code_engine.tools.code_reviewer import CodeReviewer
        from core.code_engine.tools.docs_browser import DocsBrowser
        from core.code_engine.tools.code_memory import CodeMemory
        from core.code_engine.tools.deployer import Deployer

        for cls in (ProjectAnalyzer, FileSystem, CodeSearch, Editor, Git, Shell, DependencyManager, Testing,
                    Debugger, CheckpointManager, Planner, ToolOrchestrator,
                    CodeReviewer, DocsBrowser, CodeMemory, Deployer):
            try:
                instance = cls()
                self._tools[instance.name] = instance
            except Exception:
                logger.error("CodeEngineToolManager: failed to register %s", cls.__name__, exc_info=True)

    def get_tool(self, name: str):
        # 'code_engine_enabled' Ajustes toggle (data/feature_flags.json) —
        # a single choke point that disables every Code Engine capability
        # at once: every tool in this package is only ever reached via
        # get_tool() (see each tool's own auto-trigger call sites, which
        # all go through core.code_engine.tool_manager.tool_manager), so
        # returning None here makes the whole system degrade exactly like
        # an already-handled "X tool unavailable" case everywhere else in
        # this package — no special-casing needed at any call site.
        try:
            from core import memory
            if not memory.is_feature_enabled("code_engine_enabled"):
                return None
        except Exception:
            pass   # flag lookup failing should never itself block tool access
        return self._tools.get(name)

    def is_available(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            return False
        try:
            return bool(tool.ping())
        except Exception:
            return False

    def list_tools(self) -> list:
        out = []
        for name, tool in self._tools.items():
            try:
                ok = bool(tool.ping())
            except Exception:
                ok = False
            out.append({
                "name": name,
                "description": tool.description,
                "version": tool.version,
                "status": "ok" if ok else "error",
            })
        return out


tool_manager = CodeEngineToolManager()
