# Split out of tool_manager.py: CodeEngineToolManager eagerly imports every
# tool class at module-import time (to build its singleton registry), and
# every tool class needs CodeEngineTool — importing it FROM tool_manager.py
# would make that a circular import the moment any tool module is loaded
# directly (e.g. `from core.code_engine.tools.file_system import FileSystem`
# on its own, before tool_manager.py has finished initializing). This file
# has no other imports, so it can never be part of a cycle.
class CodeEngineTool:
    """Base interface every tool in core/code_engine/tools/ implements."""
    name: str = ""
    description: str = ""
    version: str = "1.0"

    def ping(self) -> bool:
        return True
