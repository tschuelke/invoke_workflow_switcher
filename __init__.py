"""Standalone Workflow Switcher & Collection Flyout extension for InvokeAI 7 (`webv2`).

Inspired by InvokeAI 7's native Video-tab mode selector: adds a configurable
workflow-switching flyout at the top of the Workflow Form panel that lists
user library workflows, built-in default workflows, and installed node-pack
workflow templates (`nodes/*/workflows/*.json`) with per-workflow checkbox filtering.
"""

from .switcher_extension import (
    SWITCHER_SCRIPT_PATH,
    SWITCHER_VERSION,
    install_workflow_switcher_extension,
)

__version__ = SWITCHER_VERSION
__all__ = [
    "__version__",
    "SWITCHER_SCRIPT_PATH",
    "install_workflow_switcher_extension",
]

# Auto-register with InvokeAI 7's FastAPI application when loaded by `load_custom_nodes`.
install_workflow_switcher_extension()
