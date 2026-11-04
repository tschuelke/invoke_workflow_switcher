"""Standalone Workflow Switcher & Collection Flyout extension for InvokeAI 7 (`webv2`).

Inspired by InvokeAI 7's native Video-tab mode selector: adds a configurable
workflow-switching flyout at the top of the Workflow Form panel that scans
installed node-pack workflows (`nodes/*/workflows/*.json`), user library workflows,
and custom user-specified folders, with per-workflow checkbox filtering.
"""

from .switcher_extension import (
    SWITCHER_SCRIPT_PATH,
    install_workflow_switcher_extension,
)

__version__ = "0.2.0"
__all__ = [
    "__version__",
    "SWITCHER_SCRIPT_PATH",
    "install_workflow_switcher_extension",
]

# Auto-register with InvokeAI 7's FastAPI application when loaded by `load_custom_nodes`.
install_workflow_switcher_extension()
