"""In-memory ASGI Workflow Switcher Form-Element extension for InvokeAI 7 (`webv2`).

How it integrates natively into InvokeAI 7's Form Builder (`Edit` tab) and Form View (`View` tab):
1. **`+ Add form element` Menu Integration:**
   When the user clicks `+ Add form element` in the `Edit` tab, our script injects a 6th menu item:
   **`⇄ Workflow Switcher`** right below `Container (row)`.
2. **In the `Edit` Tab (Form Builder):**
   Clicking `⇄ Workflow Switcher` adds a standard form element carrying a `[workflow-switcher]` marker.
   Inside that movable, deletable Form Builder card, the user can:
   - Add custom local workflow folders (`+ Add Folder`),
   - Check (`[x]`) or uncheck which workflows (from Node Packs, the InvokeAI Library, or custom folders)
     should be included in this switcher dropdown,
   - Drag the `⇄ Workflow Switcher` card anywhere in the form (e.g. to the very top) or delete it with `×`.
3. **In the `View` Tab (Daily Use):**
   Where the `[workflow-switcher]` element sits in the form, the user sees ONLY the clean
   **Workflow Switcher Dropdown** showing the checked workflows for instant 1-click switching!
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

SWITCHER_SCRIPT_PATH: str = "/workflow-switcher.js"
SWITCHER_SCAN_PATH: str = "/workflow-switcher/scan"
SWITCHER_FILE_PATH: str = "/workflow-switcher/file"
SWITCHER_SCRIPT_TAG: bytes = b'<script src="/workflow-switcher.js" defer></script></body>'


_IGNORED_SCAN_DIR_NAMES: frozenset[str] = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", "venv", "tests", "test", "dist", "build", ".mypy_cache", ".pytest_cache"}
)


def get_default_workflow_dirs() -> list[Path]:
    """Locate `<INVOKE_ROOT>/nodes/*` and `<INVOKE_ROOT>/workflows` automatically."""
    dirs: list[Path] = []
    try:
        nodes_dir = Path(__file__).resolve().parents[1]
        if nodes_dir.is_dir():
            for child in sorted(nodes_dir.iterdir()):
                if child.is_dir() and not child.name.startswith(".") and child.name not in _IGNORED_SCAN_DIR_NAMES:
                    if child not in dirs:
                        dirs.append(child)
        invoke_root = Path(__file__).resolve().parents[2]
        root_wf = invoke_root / "workflows"
        if root_wf.is_dir() and root_wf not in dirs:
            dirs.append(root_wf)
    except IndexError:
        pass
    return dirs


def _iter_workflow_json_files(root_folder: Path) -> list[Path]:
    """Recursively find `.json` files under `root_folder`, skipping hidden/build/test directories."""
    found: list[Path] = []
    try:
        for json_file in root_folder.rglob("*.json"):
            try:
                rel_parts = json_file.relative_to(root_folder).parts[:-1]
            except ValueError:
                rel_parts = ()
            if any(p.startswith(".") or p.lower() in _IGNORED_SCAN_DIR_NAMES for p in rel_parts):
                continue
            if json_file.is_file() and not json_file.is_symlink():
                found.append(json_file)
    except OSError:
        pass
    return sorted(found)


def _is_valid_invoke_workflow_dict(data: Any) -> tuple[bool, dict[str, Any]]:
    """Return `(True, inner_workflow_dict)` only for genuine InvokeAI workflow JSON objects."""
    if not isinstance(data, dict):
        return False, {}
    inner = data.get("workflow", data) if isinstance(data.get("workflow"), dict) else data
    if not isinstance(inner, dict):
        return False, {}
    nodes = inner.get("nodes")
    edges = inner.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return False, {}
    return True, inner


def scan_workflow_directories(extra_folders: list[str] | None = None) -> list[dict[str, str]]:
    """Recursively scan node-pack directories plus any user-supplied folder paths for workflow JSONs."""
    search_dirs: list[tuple[str, Path]] = []
    for d in get_default_workflow_dirs():
        label = "Node Pack" if d.name != "workflows" else "InvokeAI Workflows"
        search_dirs.append((label, d))

    for raw_folder in extra_folders or []:
        cleaned = raw_folder.strip().strip('"').strip("'")
        if not cleaned:
            continue
        p = Path(cleaned)
        if p.is_dir():
            search_dirs.append(("Custom Folder", p))

    results: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for source_label, folder in search_dirs:
        for json_file in _iter_workflow_json_files(folder):
            resolved_str = str(json_file.resolve())
            if resolved_str in seen_paths:
                continue
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                is_wf, inner = _is_valid_invoke_workflow_dict(data)
                if not is_wf:
                    continue
                name = str(inner.get("name") or json_file.stem).strip()
                desc = str(inner.get("description") or "").strip()
                raw_tags = str(inner.get("tags") or "").strip()
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
                headings: list[str] = []
                form_obj = inner.get("form")
                if isinstance(form_obj, dict) and isinstance(form_obj.get("elements"), dict):
                    for el in form_obj["elements"].values():
                        if isinstance(el, dict) and el.get("type") == "heading":
                            el_data = el.get("data")
                            if isinstance(el_data, dict):
                                h_text = str(el_data.get("content") or "").strip()
                                if h_text:
                                    headings.append(h_text.lower())
                seen_paths.add(resolved_str)
                results.append(
                    {
                        "id": f"file:{resolved_str}",
                        "name": name,
                        "description": desc,
                        "tags": tags,
                        "headings": headings,
                        "source": source_label,
                        "path": resolved_str,
                        "filename": json_file.name,
                    }
                )
            except Exception:
                continue

    results.sort(key=lambda item: (item["source"].lower(), item["name"].lower()))
    return results


def read_workflow_file_bytes(file_path_str: str, extra_folders: list[str] | None = None) -> tuple[bytes, str] | None:
    """Safely read a discovered `.json` workflow file from disk and return `(bytes, filename)`."""
    cleaned = unquote(file_path_str).strip()
    if not cleaned:
        return None
    candidate = Path(cleaned)
    if candidate.is_symlink() or candidate.suffix.lower() != ".json" or not candidate.is_file():
        return None
    try:
        resolved = candidate.resolve()
        allowed_roots = [d.resolve() for d in get_default_workflow_dirs()]
        for raw_folder in extra_folders or []:
            folder_clean = raw_folder.strip().strip('"').strip("'")
            if folder_clean and Path(folder_clean).is_dir():
                allowed_roots.append(Path(folder_clean).resolve())
        if allowed_roots and not any(resolved.is_relative_to(root) for root in allowed_roots):
            return None
        raw = resolved.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        if not _is_valid_invoke_workflow_dict(parsed)[0]:
            return None
        return raw, resolved.name
    except Exception:
        return None


def install_workflow_switcher_extension(target_app: Any | None = None, logger: logging.Logger | None = None) -> bool:
    """Attach (or hot-update on Reload Nodes) the standalone Workflow Switcher extension."""
    app = target_app if target_app is not None else _resolve_invoke_fastapi_app()
    if app is None:
        return False

    ui_route = _find_ui_mount_route(app)
    if ui_route is None or getattr(ui_route, "app", None) is None:
        return False

    current_ui_app = ui_route.app
    while getattr(current_ui_app, "_is_workflow_switcher_wrapper", False):
        current_ui_app = current_ui_app.inner_app

    ui_route.app = _WorkflowSwitcherASGIWrapper(current_ui_app)
    setattr(app, "_invoke_workflow_switcher_installed", True)

    log = logger or logging.getLogger("InvokeAI")
    log.info("Loaded invoke_workflow_switcher v0.2.0 (+ Add form element -> Workflow Switcher with Collapsible toggle)")
    return True


class _WorkflowSwitcherASGIWrapper:
    """ASGI wrapper serving `/workflow-switcher.js`, `/workflow-switcher/scan`, and `/workflow-switcher/file`."""

    _is_workflow_switcher_wrapper: bool = True

    def __init__(self, inner_app: Any) -> None:
        self.inner_app = inner_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.inner_app(scope, receive, send)
            return

        path: str = scope.get("path", "/") or "/"
        query_string: str = (scope.get("query_string") or b"").decode("utf-8", errors="ignore")

        if path == SWITCHER_SCRIPT_PATH or path.endswith("/workflow-switcher.js"):
            await _send_response(send, _WORKFLOW_SWITCHER_JS_BYTES, b"application/javascript; charset=utf-8")
            return

        if path == SWITCHER_SCAN_PATH:
            params = parse_qs(query_string)
            folders = params.get("folder", [])
            items = scan_workflow_directories(extra_folders=folders)
            payload = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
            await _send_response(send, payload, b"application/json; charset=utf-8")
            return

        if path == SWITCHER_FILE_PATH:
            params = parse_qs(query_string)
            target = (params.get("path") or [""])[0]
            folders = params.get("folder", [])
            res = read_workflow_file_bytes(target, extra_folders=folders)
            if res is not None:
                raw_bytes, _fname = res
                await _send_response(send, raw_bytes, b"application/json; charset=utf-8")
            else:
                await _send_response(send, b'{"error":"Invalid or missing workflow JSON"}', b"application/json", status=404)
            return

        if path not in ("/", "/index.html"):
            await self.inner_app(scope, receive, send)
            return

        start_message: dict[str, Any] | None = None
        body_chunks: list[bytes] = []

        async def capture_send(message: dict[str, Any]) -> None:
            nonlocal start_message
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                start_message = message
                return
            if msg_type == "http.response.body":
                body_chunks.append(message.get("body", b""))
                if not message.get("more_body", False) and start_message is not None:
                    full_body = b"".join(body_chunks)
                    if b"</body>" in full_body and b"workflow-switcher.js" not in full_body:
                        full_body = full_body.replace(b"</body>", SWITCHER_SCRIPT_TAG, 1)
                    headers = [
                        (k, v)
                        for k, v in start_message.get("headers", [])
                        if k.lower() != b"content-length"
                    ]
                    headers.append((b"content-length", str(len(full_body)).encode("ascii")))
                    updated_start = {**start_message, "headers": headers}
                    await send(updated_start)
                    await send({"type": "http.response.body", "body": full_body, "more_body": False})
                return
            await send(message)

        await self.inner_app(scope, receive, capture_send)


def _resolve_invoke_fastapi_app() -> Any | None:
    api_app_mod = sys.modules.get("invokeai.app.api_app")
    if api_app_mod is not None and hasattr(api_app_mod, "app"):
        return getattr(api_app_mod, "app")
    return None


def _find_ui_mount_route(app: Any) -> Any | None:
    routes = getattr(getattr(app, "router", None), "routes", None) or getattr(app, "routes", None)
    if not routes:
        return None
    for route in routes:
        if getattr(route, "name", None) == "ui":
            return route
    for route in reversed(list(routes)):
        if getattr(route, "path", None) == "/" and hasattr(route, "app"):
            return route
    return None


async def _send_response(send: Any, body: bytes, content_type: bytes, *, status: int = 200) -> None:
    headers = [
        (b"content-type", content_type),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


_WORKFLOW_SWITCHER_JS: str = r"""
(function () {
  if (window.__invoke7WorkflowSwitcherLoaded) return;
  window.__invoke7WorkflowSwitcherLoaded = true;

  const STORAGE_KEY = "invoke7_workflow_switcher_config_v2";
  const MARKER_PREFIX = "[workflow-switcher";
  const MARKER_DEFAULT = "[workflow-switcher]";

  function isSwitcherMarker(text) {
    const t = (text || "").trim().toLowerCase();
    return t === "[workflow-switcher]" || t.startsWith("[workflow-switcher:") || t.startsWith("[workflow-switcher]");
  }

  function loadConfig() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        return {
          customFolders: Array.isArray(parsed.customFolders) ? parsed.customFolders : [],
          globalFavorites: Array.isArray(parsed.globalFavorites) ? parsed.globalFavorites : [],
          globalActiveTags: Array.isArray(parsed.globalActiveTags)
            ? parsed.globalActiveTags.map((t) => String(t).toLowerCase())
            : [],
          globalIncludeNodePacks: Boolean(parsed.globalIncludeNodePacks),
          globalIncludeBrowse: Boolean(parsed.globalIncludeBrowse),
          savedSectionsByWorkflow:
            parsed.savedSectionsByWorkflow && typeof parsed.savedSectionsByWorkflow === "object"
              ? parsed.savedSectionsByWorkflow
              : {},
          collapsedInView: Boolean(parsed.collapsedInView)
        };
      }
    } catch (_) {}
    return {
      customFolders: [],
      globalFavorites: [],
      globalActiveTags: [],
      globalIncludeNodePacks: false,
      globalIncludeBrowse: false,
      savedSectionsByWorkflow: {},
      collapsedInView: false
    };
  }

  function saveConfig(cfg) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg));
    } catch (_) {}
  }

  let switcherConfig = loadConfig();
  let allDiscoveredWorkflows = [];
  let scanInFlight = false;
  let pendingStampNewTextElement = false;
  let knownFormTextareas = new Set();

  function getItemFavoriteKey(item) {
    const kind =
      item && item.id && item.id.startsWith("file:")
        ? "pack:"
        : item && item.isDefaultLib
          ? "browse:"
          : "lib:";
    const name = ((item && item.name) || "").trim().toLowerCase();
    return kind + name;
  }

  function createInitialMarkerStateFromGlobal() {
    return {
      colorized: true,
      showInfo: false,
      includeNodePacks: Boolean(switcherConfig.globalIncludeNodePacks),
      includeBrowse: Boolean(switcherConfig.globalIncludeBrowse),
      activeTags: Array.isArray(switcherConfig.globalActiveTags) ? [...switcherConfig.globalActiveTags] : [],
      favorites: Array.isArray(switcherConfig.globalFavorites) ? [...switcherConfig.globalFavorites] : []
    };
  }

  function parseWorkflowMarkerState(text) {
    const raw = (text || "").trim();
    const braceIdx = raw.indexOf("{");
    if (braceIdx !== -1) {
      try {
        const parsed = JSON.parse(raw.slice(braceIdx));
        if (parsed && typeof parsed === "object") {
          return {
            colorized: parsed.colorized !== false,
            showInfo: Boolean(parsed.showInfo),
            includeNodePacks: Boolean(parsed.includeNodePacks),
            includeBrowse: Boolean(parsed.includeBrowse),
            activeTags: Array.isArray(parsed.activeTags)
              ? parsed.activeTags.map((t) => String(t).toLowerCase())
              : [],
            favorites: Array.isArray(parsed.favorites)
              ? parsed.favorites.map((f) => String(f).trim().toLowerCase()).filter(Boolean)
              : []
          };
        }
      } catch (_) {}
    }
    const lower = raw.toLowerCase();
    const hasPlain = lower.includes("plain") || raw.includes("nocolor");
    const hasInfo = lower.includes("info") || lower.includes("desc");
    const base = createInitialMarkerStateFromGlobal();
    return {
      ...base,
      colorized: !hasPlain,
      showInfo: hasInfo
    };
  }

  function formatWorkflowMarkerState(state) {
    const clean = {
      colorized: state.colorized !== false,
      showInfo: Boolean(state.showInfo),
      includeNodePacks: Boolean(state.includeNodePacks),
      includeBrowse: Boolean(state.includeBrowse),
      activeTags: Array.isArray(state.activeTags) ? state.activeTags : [],
      favorites: Array.isArray(state.favorites) ? state.favorites : []
    };
    return MARKER_DEFAULT + JSON.stringify(clean);
  }

  function isItemFavoritedInState(wfState, item) {
    if (!wfState || !Array.isArray(wfState.favorites) || wfState.favorites.length === 0) {
      return false;
    }
    const key = getItemFavoriteKey(item);
    const nameOnly = ((item && item.name) || "").trim().toLowerCase();
    return wfState.favorites.includes(key) || wfState.favorites.includes(nameOnly);
  }

  function injectSwitcherStyles() {
    if (document.getElementById("inv7-wf-switcher-styles")) return;
    const style = document.createElement("style");
    style.id = "inv7-wf-switcher-styles";
    style.textContent = `
      .inv7-wfs-view-box {
        display: flex;
        flex-direction: column;
        gap: 0;
        padding: 0;
        overflow: hidden;
        border-radius: 6px;
        background-color: var(--inv7-wfs-box-bg, var(--chakra-colors-bg\\.muted, var(--chakra-colors-neutral-800, #33353d)));
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-left: 3px solid var(--chakra-colors-accent-solid, #5ac8fa);
      }
      .inv7-wfs-view-box[data-colorized="false"] {
        background-color: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.10);
        border-left: 3px solid var(--chakra-colors-accent-solid, #5ac8fa);
      }
      .inv7-wfs-view-box[data-colorized="false"] .inv7-wfs-select {
        background-color: rgba(255, 255, 255, 0.08);
        border-color: rgba(255, 255, 255, 0.18);
      }
      .inv7-wfs-header-row {
        display: flex;
        align-items: center;
        width: 100%;
        height: 32px;
        padding: 0 8px 0 6px;
        gap: 6px;
      }
      .inv7-wfs-acc-trigger {
        display: flex;
        align-items: center;
        gap: 8px;
        flex: 1;
        min-width: 0;
        height: 100%;
        padding: 0;
        background: transparent;
        border: none;
        cursor: pointer;
        text-align: left;
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
      }
      .inv7-wfs-acc-trigger:hover {
        color: var(--chakra-colors-fg, #f4f4f5);
      }
      .inv7-wfs-acc-chevron {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 14px;
        height: 14px;
        flex-shrink: 0;
        transition: transform 0.15s ease;
      }
      .inv7-wfs-view-box[data-open="true"] .inv7-wfs-acc-chevron {
        transform: rotate(90deg);
      }
      .inv7-wfs-acc-badge {
        margin-left: auto;
        font-family: var(--chakra-fonts-mono, monospace);
        font-size: 10px;
        font-weight: 500;
        padding: 2px 6px;
        border-radius: 4px;
        background-color: rgba(90, 200, 250, 0.18);
        color: rgb(147, 226, 255);
        max-width: 58%;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex-shrink: 0;
      }
      .inv7-wfs-view-box[data-colorized="false"] .inv7-wfs-acc-badge {
        background-color: rgba(255, 255, 255, 0.08);
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
      }
      .inv7-wfs-info-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 22px;
        height: 22px;
        padding: 0;
        border-radius: 4px;
        border: 1px solid transparent;
        background: transparent;
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
        cursor: pointer;
        flex-shrink: 0;
        transition: color 0.12s ease, background-color 0.12s ease, border-color 0.12s ease;
      }
      .inv7-wfs-info-btn:hover {
        color: var(--chakra-colors-fg, #f4f4f5);
        background-color: rgba(255, 255, 255, 0.06);
      }
      .inv7-wfs-info-btn[aria-pressed="true"] {
        color: var(--chakra-colors-accent-solid, #5ac8fa);
        background-color: rgba(90, 200, 250, 0.14);
        border-color: rgba(90, 200, 250, 0.32);
      }
      .inv7-wfs-acc-body {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 8px 10px 10px 10px;
        border-top: 1px solid var(--chakra-colors-bg\\.subtle, rgba(0, 0, 0, 0.35));
      }
      .inv7-wfs-view-box[data-open="false"] .inv7-wfs-acc-body {
        display: none;
      }
      .inv7-wfs-header-checks {
        display: inline-flex;
        align-items: center;
        gap: 10px;
        margin-right: 6px;
      }
      .inv7-wfs-header-check {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 10px;
        font-weight: 500;
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
        cursor: pointer;
        user-select: none;
      }
      .inv7-wfs-header-check input {
        cursor: pointer;
        accent-color: var(--chakra-colors-accent-solid, #5ac8fa);
      }
      .inv7-wfs-edit-box {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 8px 4px 4px 4px;
      }
      .inv7-wfs-toprow {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .inv7-wfs-label {
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.09em;
        text-transform: uppercase;
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
      }
      .inv7-wfs-btn {
        font-size: 11px;
        font-weight: 600;
        padding: 3px 9px;
        border-radius: 4px;
        cursor: pointer;
        background-color: var(--wb-theme-input-bg, var(--chakra-colors-bg\\.subtle, rgba(255, 255, 255, 0.10)));
        color: var(--chakra-colors-fg, #f4f4f5);
        border: 1px solid var(--wb-theme-input-border, var(--chakra-colors-border, rgba(255, 255, 255, 0.20)));
      }
      .inv7-wfs-btn:hover {
        border-color: var(--chakra-colors-accent-solid, #5ac8fa);
      }
      .inv7-wfs-select {
        width: 100%;
        height: 32px;
        padding: 0 10px;
        border-radius: 5px;
        font-size: 12px;
        font-weight: 600;
        color: var(--chakra-colors-fg, #f4f4f5);
        background-color: var(--wb-theme-input-bg, var(--chakra-colors-bg\\.subtle, rgba(255, 255, 255, 0.10)));
        border: 1px solid var(--wb-theme-input-border, var(--chakra-colors-border, rgba(255, 255, 255, 0.22)));
        cursor: pointer;
        outline: none;
      }
      .inv7-wfs-select:hover {
        border-color: var(--chakra-colors-accent-solid, #5ac8fa);
      }
      .inv7-wfs-select option, .inv7-wfs-select optgroup {
        background-color: #18181b;
        color: #f4f4f5;
      }
      .inv7-wfs-select option[data-source-kind="pack"] {
        color: #fbbf24;
      }
      .inv7-wfs-select option[data-source-kind="browse"] {
        color: #c4b5fd;
      }
      .inv7-wfs-select hr {
        border: none;
        border-top: 1px solid rgba(255, 255, 255, 0.18);
        margin: 3px 0;
      }
      .inv7-wfs-checkitem[data-family-start="true"] {
        border-top: 1px solid rgba(255, 255, 255, 0.12);
        padding-top: 6px;
        margin-top: 3px;
      }
      .inv7-wfs-checkitem[data-source-kind="pack"] {
        color: #fde68a;
      }
      .inv7-wfs-checkitem[data-source-kind="pack"] input {
        accent-color: #f59e0b;
      }
      .inv7-wfs-checkitem[data-source-kind="browse"] {
        color: #ddd6fe;
      }
      .inv7-wfs-checkitem[data-source-kind="browse"] input {
        accent-color: #8b5cf6;
      }
      .inv7-wfs-pack-toggle {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-size: 10px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 4px;
        background-color: rgba(245, 158, 11, 0.16);
        border: 1px solid rgba(245, 158, 11, 0.38);
        color: #fbbf24;
        cursor: pointer;
        user-select: none;
      }
      .inv7-wfs-pack-toggle input {
        cursor: pointer;
        accent-color: #f59e0b;
      }
      .inv7-wfs-browse-toggle {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-size: 10px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 4px;
        background-color: rgba(139, 92, 246, 0.18);
        border: 1px solid rgba(139, 92, 246, 0.42);
        color: #c4b5fd;
        cursor: pointer;
        user-select: none;
      }
      .inv7-wfs-browse-toggle input {
        cursor: pointer;
        accent-color: #8b5cf6;
      }
      .inv7-wfs-desc {
        font-size: 11px;
        line-height: 1.35;
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
      }
      .inv7-wfs-folder-row {
        display: flex;
        gap: 6px;
      }
      .inv7-wfs-folder-input {
        flex: 1;
        height: 28px;
        padding: 0 8px;
        border-radius: 4px;
        font-size: 11px;
        color: var(--chakra-colors-fg, #f4f4f5);
        background-color: var(--wb-theme-input-bg, var(--chakra-colors-bg\\.subtle, rgba(255, 255, 255, 0.10)));
        border: 1px solid var(--wb-theme-input-border, var(--chakra-colors-border, rgba(255, 255, 255, 0.22)));
      }
      .inv7-wfs-checklist {
        max-height: 220px;
        overflow-y: auto;
        overflow-x: hidden;
        display: flex;
        flex-direction: column;
        gap: 5px;
        padding: 6px 8px;
        border-radius: 4px;
        background-color: rgba(0, 0, 0, 0.25);
        border: 1px solid rgba(255, 255, 255, 0.07);
      }
      .inv7-wfs-checkitem {
        display: flex;
        align-items: center;
        gap: 7px;
        font-size: 11px;
        color: var(--chakra-colors-fg, #f4f4f5);
        cursor: pointer;
        min-width: 0;
      }
      .inv7-wfs-checkitem-name {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .inv7-wfs-source-tag {
        font-size: 9px;
        padding: 1px 5px;
        border-radius: 3px;
        background-color: rgba(90, 200, 250, 0.14);
        color: rgb(147, 226, 255);
        margin-left: auto;
        flex-shrink: 0;
      }
      .inv7-wfs-source-tag[data-source-kind="pack"] {
        background-color: rgba(245, 158, 11, 0.20);
        color: #fbbf24;
      }
      .inv7-wfs-source-tag[data-source-kind="browse"] {
        background-color: rgba(139, 92, 246, 0.22);
        color: #c4b5fd;
      }
      .inv7-wfs-folder-tag {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 10px;
        padding: 2px 6px;
        border-radius: 4px;
        background-color: rgba(90, 200, 250, 0.15);
        color: #f4f4f5;
      }
      .inv7-wfs-folder-remove {
        cursor: pointer;
        font-weight: bold;
        color: #f87171;
        border: none;
        background: none;
        padding: 0 2px;
      }
      .inv7-wfs-chip-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        align-items: center;
      }
      .inv7-wfs-chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 10px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 999px;
        cursor: pointer;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background-color: rgba(255, 255, 255, 0.04);
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
        transition: background-color 0.12s ease, color 0.12s ease, border-color 0.12s ease;
      }
      .inv7-wfs-chip:hover {
        color: var(--chakra-colors-fg, #f4f4f5);
        border-color: rgba(255, 255, 255, 0.24);
      }
      .inv7-wfs-chip[data-active="true"] {
        background-color: rgba(90, 200, 250, 0.18);
        color: rgb(147, 226, 255);
        border-color: rgba(90, 200, 250, 0.45);
      }
      .inv7-wfs-chip-count {
        font-size: 9px;
        opacity: 0.75;
      }

      /* Media Dropzones ('Choose image' / 'Choose video'): bind background & dashed border to Input Brightness & Border sliders */
      .group > button[data-scope="popover"][data-part="trigger"] {
        background-color: var(--wb-theme-input-bg, var(--chakra-colors-bg\\.subtle, rgba(0, 0, 0, 0.24))) !important;
      }
      .group > button[data-scope="popover"][data-part="trigger"]:not([data-invalid]) {
        border-color: var(--wb-theme-input-border, var(--chakra-colors-border\\.emphasized, rgba(255, 255, 255, 0.28))) !important;
      }
      .group > button[data-scope="popover"][data-part="trigger"]:hover:not(:disabled) {
        border-color: var(--chakra-colors-accent-solid, #5ac8fa) !important;
      }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  function setReactTextareaValue(textarea, newValue) {
    const previousValue = textarea.value;
    const proto = window.HTMLTextAreaElement && window.HTMLTextAreaElement.prototype;
    const descriptor = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
    if (descriptor && descriptor.set) {
      descriptor.set.call(textarea, newValue);
    } else {
      textarea.value = newValue;
    }
    const tracker = textarea._valueTracker;
    if (tracker && typeof tracker.setValue === "function") {
      tracker.setValue(previousValue);
    }
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
  }

  let activeTagFilter = null;
  let searchFilterText = "";

  function parseTagsList(rawTags) {
    if (Array.isArray(rawTags)) return rawTags;
    if (!rawTags || typeof rawTags !== "string") return [];
    return rawTags
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
  }

  async function refreshAllWorkflows() {
    if (scanInFlight) return;
    scanInFlight = true;
    try {
      const q = switcherConfig.customFolders
        .map((f) => "folder=" + encodeURIComponent(f))
        .join("&");
      const scanUrl = "/workflow-switcher/scan" + (q ? "?" + q : "");
      const [libResp, browseResp, diskResp] = await Promise.all([
        fetch("/api/v1/workflows/?page=0&per_page=100&categories=user&order_by=name&direction=ASC", { cache: "no-store" }).catch(() => null),
        fetch("/api/v1/workflows/?page=0&per_page=100&categories=default&order_by=name&direction=ASC", { cache: "no-store" }).catch(() => null),
        fetch(scanUrl, { cache: "no-store" }).catch(() => null)
      ]);

      const combined = [];
      const seenLibraryNames = new Set();

      if (libResp && libResp.ok) {
        const libData = await libResp.json();
        const libItems = Array.isArray(libData.items) ? libData.items : [];
        const fullRecords = await Promise.all(
          libItems.map((wf) =>
            wf && wf.workflow_id
              ? fetch("/api/v1/workflows/i/" + encodeURIComponent(wf.workflow_id), { cache: "no-store" })
                  .then((r) => (r.ok ? r.json() : null))
                  .catch(() => null)
              : Promise.resolve(null)
          )
        );
        for (let i = 0; i < libItems.length; i++) {
          const wf = libItems[i];
          if (!wf || !wf.workflow_id || !wf.name) continue;
          const record = fullRecords[i];
          const inner = record && record.workflow ? record.workflow : null;
          const headings = [];
          if (inner && inner.form && inner.form.elements && typeof inner.form.elements === "object") {
            for (const el of Object.values(inner.form.elements)) {
              if (el && el.type === "heading" && el.data && el.data.content) {
                const h = String(el.data.content).trim().toLowerCase();
                if (h) headings.push(h);
              }
            }
          }
          const tags = parseTagsList(wf.tags || (inner && inner.tags));
          seenLibraryNames.add(wf.name.trim().toLowerCase());
          combined.push({
            id: "lib:" + wf.workflow_id,
            workflow_id: wf.workflow_id,
            name: wf.name,
            description: wf.description || (inner && inner.description) || "",
            tags: tags,
            headings: headings,
            isDefaultLib: false,
            isPack: false,
            source: "Your Library"
          });
        }
      }

      if (browseResp && browseResp.ok) {
        const browseData = await browseResp.json();
        for (const wf of browseData.items || []) {
          if (!wf || !wf.workflow_id || !wf.name) continue;
          if (seenLibraryNames.has(wf.name.trim().toLowerCase())) continue;
          combined.push({
            id: "lib:" + wf.workflow_id,
            workflow_id: wf.workflow_id,
            name: wf.name,
            description: wf.description || "",
            tags: parseTagsList(wf.tags),
            headings: [],
            isDefaultLib: true,
            isPack: false,
            source: "Built-in (Browse)"
          });
        }
      }

      if (diskResp && diskResp.ok) {
        const diskData = await diskResp.json();
        for (const item of diskData.items || []) {
          // If the same workflow name already exists in Your Library, Your Library takes priority!
          if (seenLibraryNames.has((item.name || "").trim().toLowerCase())) continue;
          combined.push({
            ...item,
            isPack: item.source && item.source.startsWith("Node Pack"),
            tags: parseTagsList(item.tags)
          });
        }
      }

      allDiscoveredWorkflows = combined;
    } finally {
      scanInFlight = false;
      rehydrateAllSwitcherWidgets();
    }
  }

  function findWorkflowUiAdapter(startEl) {
    const candidates = [
      startEl,
      document.querySelector(".inv7-wfs-edit-box"),
      document.querySelector(".inv7-wfs-view-box"),
      document.querySelector("input[type='file'][accept*='.json']"),
      document.querySelector("[role='region']")
    ];
    for (const el of candidates) {
      if (!el) continue;
      const fiberKey = Object.keys(el).find((k) => k.startsWith("__reactFiber$"));
      if (!fiberKey) continue;
      let cur = el[fiberKey];
      while (cur) {
        const props = cur.memoizedProps;
        if (props && typeof props === "object") {
          const adp = props.adapter || props.value;
          if (adp && adp.commands && typeof adp.commands.setWorkflowSource === "function" && adp.project) {
            return adp;
          }
        }
        cur = cur.return;
      }
    }
    return null;
  }

  let hydratedWorkflowIds = new Set();

  function captureActiveWorkflowSectionStates() {
    const adapter = findWorkflowUiAdapter();
    if (!adapter || !adapter.project) return null;
    const snap = adapter.project.getSnapshot();
    if (!snap || !snap.activeWorkflowId || !snap.projectGraph) return null;
    const doc = snap.projectGraph;
    const wfNameKey = (doc.name || "").trim().toLowerCase();
    const allOpen =
      snap.workflowValues &&
      snap.workflowValues.formSectionsOpen &&
      typeof snap.workflowValues.formSectionsOpen === "object"
        ? snap.workflowValues.formSectionsOpen
        : {};
    const currentMap =
      allOpen[snap.activeWorkflowId] && typeof allOpen[snap.activeWorkflowId] === "object"
        ? allOpen[snap.activeWorkflowId]
        : {};

    const byTitleAndId = {};
    const elements = (doc.form && doc.form.elements) || {};
    for (const [elId, el] of Object.entries(elements)) {
      if (el && el.type === "heading" && typeof currentMap[elId] === "boolean") {
        byTitleAndId[elId] = currentMap[elId];
        const titleKey = "title:" + String((el.data && el.data.content) || "").trim().toLowerCase();
        byTitleAndId[titleKey] = currentMap[elId];
      }
    }

    if (wfNameKey && Object.keys(byTitleAndId).length > 0) {
      if (!switcherConfig.savedSectionsByWorkflow) switcherConfig.savedSectionsByWorkflow = {};
      switcherConfig.savedSectionsByWorkflow[wfNameKey] = byTitleAndId;
      saveConfig(switcherConfig);
    }
    return { currentMap, byTitleAndId, wfNameKey };
  }

  function syncSavedOpenSectionsForActiveWorkflow() {
    const adapter = findWorkflowUiAdapter();
    if (!adapter || !adapter.project || !adapter.widgets) return;
    const snap = adapter.project.getSnapshot();
    if (!snap || !snap.activeWorkflowId || !snap.projectGraph) return;
    if (hydratedWorkflowIds.has(snap.activeWorkflowId)) return;

    const doc = snap.projectGraph;
    const wfNameKey = (doc.name || "").trim().toLowerCase();
    const allOpen =
      snap.workflowValues &&
      snap.workflowValues.formSectionsOpen &&
      typeof snap.workflowValues.formSectionsOpen === "object"
        ? snap.workflowValues.formSectionsOpen
        : {};
    const existingForId = allOpen[snap.activeWorkflowId];
    if (existingForId && typeof existingForId === "object" && Object.keys(existingForId).length > 0) {
      hydratedWorkflowIds.add(snap.activeWorkflowId);
      return;
    }

    const savedByWf =
      wfNameKey && switcherConfig.savedSectionsByWorkflow ? switcherConfig.savedSectionsByWorkflow[wfNameKey] : null;
    const elements = (doc.form && doc.form.elements) || {};
    const restored = {};
    let foundAny = false;

    for (const [elId, el] of Object.entries(elements)) {
      if (!el || el.type !== "heading") continue;
      if (el.data && typeof el.data.defaultOpen === "boolean") {
        restored[elId] = el.data.defaultOpen;
        foundAny = true;
        continue;
      }
      if (savedByWf && typeof savedByWf === "object") {
        const titleKey = "title:" + String((el.data && el.data.content) || "").trim().toLowerCase();
        if (typeof savedByWf[elId] === "boolean") {
          restored[elId] = savedByWf[elId];
          foundAny = true;
        } else if (typeof savedByWf[titleKey] === "boolean") {
          restored[elId] = savedByWf[titleKey];
          foundAny = true;
        }
      }
    }

    hydratedWorkflowIds.add(snap.activeWorkflowId);
    if (foundAny) {
      adapter.widgets.patchValues("workflow", {
        formSectionsOpen: {
          ...allOpen,
          [snap.activeWorkflowId]: restored
        }
      });
    }
  }

  function serializeProjectGraphToWorkflowJson(document, libraryWorkflowId, sectionStatesMap) {
    const serializeInvNode = (node) => {
      const clonedData = JSON.parse(JSON.stringify(node.data || {}));
      delete clonedData.callSavedWorkflowFieldsFrom;
      delete clonedData.callSavedWorkflowStatus;
      return {
        data: { ...clonedData, id: node.id },
        id: node.id,
        position: { ...(node.position || { x: 0, y: 0 }) },
        type: node.type
      };
    };
    const clonedElements = JSON.parse(JSON.stringify((document.form && document.form.elements) || {}));
    if (sectionStatesMap && typeof sectionStatesMap === "object") {
      for (const [elId, el] of Object.entries(clonedElements)) {
        if (el && el.type === "heading" && el.data && typeof sectionStatesMap[elId] === "boolean") {
          el.data.defaultOpen = sectionStatesMap[elId];
        }
      }
    }
    return {
      author: document.author || "",
      contact: document.contact || "",
      description: document.description || "",
      edges: (document.edges || []).map((edge) => ({ ...edge })),
      exposedFields: [],
      form: {
        elements: clonedElements,
        rootElementId: (document.form && document.form.rootElementId) || ""
      },
      ...(libraryWorkflowId ? { id: libraryWorkflowId } : {}),
      meta: { category: "user", version: "3.0.0" },
      name: document.name || "",
      nodes: (document.nodes || []).map((node) =>
        node.type === "connector"
          ? {
              data: { ...node.data, id: node.id, isOpen: true, type: "connector" },
              id: node.id,
              position: { ...(node.position || { x: 0, y: 0 }) },
              type: node.type
            }
          : node.type === "notes" || node.type === "current_image"
            ? {
                data: { ...node.data, id: node.id, isOpen: true },
                id: node.id,
                position: { ...(node.position || { x: 0, y: 0 }) },
                type: node.type
              }
            : serializeInvNode(node)
      ),
      notes: document.notes || "",
      tags: document.tags || "",
      version: document.workflowVersion || "1.0.0"
    };
  }

  async function saveOrUpdateCurrentWorkflowInYours(hostDiv, statusBtn) {
    const adapter = findWorkflowUiAdapter(hostDiv);
    if (!adapter || typeof adapter.getProjectGraph !== "function") {
      if (statusBtn) statusBtn.textContent = "⚠ Open via menu";
      return;
    }
    const snap = adapter.project.getSnapshot();
    const doc = adapter.getProjectGraph();
    if (!doc) return;

    const currentSource = snap.activeWorkflow && snap.activeWorkflow.source ? snap.activeWorkflow.source : null;
    const currentNameLower = (doc.name || "").trim().toLowerCase();
    const matchingLibItem = allDiscoveredWorkflows.find(
      (w) =>
        !w.isPack &&
        !w.isDefaultLib &&
        w.workflow_id &&
        ((currentSource && currentSource.libraryWorkflowId === w.workflow_id) ||
          (currentNameLower && (w.name || "").trim().toLowerCase() === currentNameLower))
    );

    const targetWorkflowId =
      (currentSource && !String(currentSource.libraryWorkflowId || "").startsWith("default_")
        ? currentSource.libraryWorkflowId
        : null) || (matchingLibItem ? matchingLibItem.workflow_id : null);

    try {
      if (statusBtn) statusBtn.textContent = "Saving...";
      const captured = captureActiveWorkflowSectionStates();
      const sectionStatesMap = captured ? captured.currentMap : null;
      if (targetWorkflowId) {
        const payload = serializeProjectGraphToWorkflowJson(doc, targetWorkflowId, sectionStatesMap);
        const resp = await fetch("/api/v1/workflows/i/" + encodeURIComponent(targetWorkflowId), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workflow: payload })
        });
        if (resp.ok) {
          const updatedDto = await resp.json();
          adapter.commands.setWorkflowSource(
            { projectId: snap.id, workflowId: snap.activeWorkflowId },
            {
              libraryWorkflowId: updatedDto.workflow_id,
              revision: updatedDto.revision,
              ownerUserId: updatedDto.user_id
            }
          );
          if (statusBtn) {
            statusBtn.textContent = "✓ Updated in Yours!";
            window.setTimeout(() => {
              statusBtn.textContent = "💾 Save / Update in Yours";
            }, 1600);
          }
          await refreshAllWorkflows();
          return;
        }
      }

      // Create new in Your Library if not found or if PATCH returned 404:
      const newPayload = serializeProjectGraphToWorkflowJson(doc, undefined, sectionStatesMap);
      delete newPayload.id;
      const createResp = await fetch("/api/v1/workflows/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow: newPayload })
      });
      if (createResp.ok) {
        const createdDto = await createResp.json();
        adapter.commands.setWorkflowSource(
          { projectId: snap.id, workflowId: snap.activeWorkflowId },
          {
            libraryWorkflowId: createdDto.workflow_id,
            revision: createdDto.revision,
            ownerUserId: createdDto.user_id
          }
        );
        if (statusBtn) {
          statusBtn.textContent = "✓ Saved to Yours!";
          window.setTimeout(() => {
            statusBtn.textContent = "💾 Save / Update in Yours";
          }, 1600);
        }
        await refreshAllWorkflows();
      } else if (statusBtn) {
        statusBtn.textContent = "⚠ Save failed";
      }
    } catch (_) {
      if (statusBtn) statusBtn.textContent = "⚠ Save error";
    }
  }

  async function activateWorkflowItem(item) {
    if (!item) return;
    const fileInput = document.querySelector("input[type='file'][accept*='.json']");
    if (!fileInput) return;

    let jsonText = "";
    let filename = (item.name || "workflow") + ".json";
    let loadedLibraryDto = null;

    if (item.id.startsWith("file:")) {
      const folderQuery = switcherConfig.customFolders
        .map((f) => "&folder=" + encodeURIComponent(f))
        .join("");
      const resp = await fetch(
        "/workflow-switcher/file?path=" + encodeURIComponent(item.path) + folderQuery,
        { cache: "no-store" }
      );
      if (!resp.ok) return;
      jsonText = await resp.text();
      filename = item.filename || filename;
    } else if (item.id.startsWith("lib:")) {
      const resp = await fetch("/api/v1/workflows/i/" + encodeURIComponent(item.workflow_id), { cache: "no-store" });
      if (!resp.ok) return;
      const dto = await resp.json();
      loadedLibraryDto = dto;
      const wfObj = dto.workflow || dto;
      jsonText = JSON.stringify(wfObj, null, 2);
    }

    if (!jsonText) return;
    const file = new File([jsonText], filename, { type: "application/json" });
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));

    // Restore the library link (`source`) on the imported workflow so "Update library template..." stays active!
    if (loadedLibraryDto && loadedLibraryDto.workflow_id) {
      const linkSource = () => {
        const adapter = findWorkflowUiAdapter(fileInput);
        if (!adapter || !adapter.project || !adapter.commands) return;
        const snap = adapter.project.getSnapshot();
        if (!snap || !snap.id || !snap.activeWorkflowId) return;
        adapter.commands.setWorkflowSource(
          { projectId: snap.id, workflowId: snap.activeWorkflowId },
          {
            libraryWorkflowId: loadedLibraryDto.workflow_id,
            revision: typeof loadedLibraryDto.revision === "number" ? loadedLibraryDto.revision : null,
            ownerUserId: loadedLibraryDto.user_id
          }
        );
        hydratedWorkflowIds.delete(snap.activeWorkflowId);
        syncSavedOpenSectionsForActiveWorkflow();
      };
      window.setTimeout(linkSource, 80);
      window.setTimeout(linkSource, 250);
    } else {
      window.setTimeout(syncSavedOpenSectionsForActiveWorkflow, 100);
    }
  }

  function syncSwitcherSurfaceColor() {
    const root = document.documentElement;
    const inlineMuted = root.style.getPropertyValue("--chakra-colors-bg.muted").trim();
    if (inlineMuted) {
      root.style.setProperty("--inv7-wfs-box-bg", inlineMuted);
      return;
    }
    const openSection = document.querySelector("[data-scope='collapsible'][data-part='root'][data-state='open']");
    if (openSection) {
      const bg = window.getComputedStyle(openSection).backgroundColor;
      if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") {
        root.style.setProperty("--inv7-wfs-box-bg", bg);
        return;
      }
    }
    root.style.removeProperty("--inv7-wfs-box-bg");
  }

  function detectActiveCenterWorkflowName(enabledItems) {
    const linkIcon = document.querySelector("button [data-template-link]");
    const linkBtn = linkIcon ? linkIcon.closest("button") : null;
    if (linkBtn) {
      const aria = linkBtn.getAttribute("aria-label") || "";
      const sepIdx = aria.indexOf("—");
      if (sepIdx !== -1) {
        return aria.slice(sepIdx + 1).trim();
      }
      const txt = (linkBtn.textContent || "").trim();
      if (txt) return txt;
    }
    const centerBtns = document.querySelectorAll("button[aria-label]");
    for (const btn of centerBtns) {
      const aria = btn.getAttribute("aria-label") || "";
      if (aria.toLowerCase().includes("workflow")) {
        const sepIdx = aria.indexOf("—");
        if (sepIdx !== -1) {
          return aria.slice(sepIdx + 1).trim();
        }
        const txt = (btn.textContent || "").trim();
        if (txt && txt.toLowerCase() !== "workflow") return txt;
      }
    }
    // Fallback when Preview is in the center panel: match current form headings against discovered workflows.
    if (Array.isArray(enabledItems) && enabledItems.length > 0) {
      const norm = (s) => (s || "").trim().toLowerCase().replace(/[\u2012\u2013\u2014\u2015]/g, "-").replace(/\s+/g, " ");
      const domHeadings = new Set();
      for (const el of document.querySelectorAll("[data-scope='collapsible'] [data-part='trigger'] span, [role='region'] p")) {
        const t = norm(el.textContent);
        if (t && t.length > 3 && t !== "needs attention" && t !== "● active") {
          domHeadings.add(t);
        }
      }
      let bestItem = null;
      let bestScore = 0;
      for (const item of enabledItems) {
        if (!Array.isArray(item.headings) || item.headings.length === 0) continue;
        let matches = 0;
        for (const h of item.headings) {
          if (domHeadings.has(norm(h))) matches++;
        }
        const score = matches / item.headings.length;
        if (matches > 0 && score > bestScore) {
          bestScore = score;
          bestItem = item;
        }
      }
      if (bestItem) return bestItem.name;
    }
    return "";
  }

  /* ==========================================================================
   * 1. INJECT "⇄ Workflow Switcher" INTO "+ Add form element" MENU
   * ========================================================================== */
  function augmentAddFormElementMenus() {
    const menus = document.querySelectorAll("[role='menu']");
    for (const menu of menus) {
      if (menu.querySelector("[data-inv7-wfs-menuitem='true']")) continue;
      const textItem = menu.querySelector("[role='menuitem'][data-value='text']");
      const rowItem = menu.querySelector("[role='menuitem'][data-value='container-row']");
      if (!textItem || !rowItem) continue;

      const wfsItem = rowItem.cloneNode(true);
      wfsItem.setAttribute("data-value", "workflow-switcher");
      wfsItem.setAttribute("data-inv7-wfs-menuitem", "true");
      wfsItem.id = (rowItem.id || "menu-item") + "-workflow-switcher";
      wfsItem.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;">
          <path d="m16 3 4 4-4 4"/><path d="M20 7H4"/><path d="m8 21-4-4 4-4"/><path d="M4 17h16"/>
        </svg>
        Favorite Workflows
      `;

      wfsItem.addEventListener("click", (evt) => {
        evt.preventDefault();
        evt.stopPropagation();
        knownFormTextareas = new Set(document.querySelectorAll("textarea[aria-label='Form text'], textarea[placeholder='Text']"));
        pendingStampNewTextElement = true;
        textItem.click();
        window.setTimeout(scheduleCheck, 30);
        window.setTimeout(scheduleCheck, 120);
      });

      menu.appendChild(wfsItem);
    }
  }

  /* ==========================================================================
   * 2. ENHANCE BUILDER CARDS IN THE "EDIT" TAB
   * ========================================================================== */
  function getWorkflowFamilyLabel(item) {
    const name = (item && item.name ? String(item.name) : "").trim();
    if (item && item.isDefaultLib) {
      const parts = name.split(/\s+[-–—]\s+/);
      if (parts.length > 1) {
        return parts[parts.length - 1].trim().replace(/\s+/g, " ");
      }
    }
    const dashMatch = name.match(/^([^-–—]+?)\s*[-–—]/);
    if (dashMatch && dashMatch[1]) {
      return dashMatch[1].trim().replace(/\s+/g, " ");
    }
    const words = name.split(/\s+/).filter(Boolean);
    return words.slice(0, 2).join(" ") || name || "Other";
  }

  function getWorkflowFamilyKey(item) {
    return getWorkflowFamilyLabel(item).toLowerCase();
  }

  function getPoolForWorkflowState(wfState) {
    const includePacks = Boolean(wfState && wfState.includeNodePacks);
    const includeBrowse = Boolean(wfState && wfState.includeBrowse);
    return allDiscoveredWorkflows.filter((w) => {
      if (isItemFavoritedInState(wfState, w)) return true;
      if (w.isPack) return includePacks;
      if (w.isDefaultLib) return includeBrowse;
      return true;
    });
  }

  function buildDropdownSelectElement(wfState) {
    const pool = getPoolForWorkflowState(wfState);
    const enabledItems = pool.filter((w) => isItemFavoritedInState(wfState, w));
    const activeName = detectActiveCenterWorkflowName(pool);
    const matchedItem = activeName
      ? pool.find((w) => w.name && w.name.toLowerCase() === activeName.toLowerCase()) || null
      : null;

    const select = document.createElement("select");
    select.className = "inv7-wfs-select";

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.selected = true;
    placeholder.disabled = true;
    placeholder.hidden = true;
    placeholder.textContent =
      enabledItems.length === 0
        ? activeName
          ? activeName + " (0 favorites checked in Edit)"
          : "— No favorite workflows selected (configure in Edit tab) —"
        : activeName
          ? activeName
          : "— Select a favorite workflow (" + enabledItems.length + " available) —";
    select.appendChild(placeholder);

    let prevFamily = null;
    for (const item of enabledItems) {
      // Omit the currently active workflow from the open dropdown choices since it is already shown in the closed box above.
      if (activeName && item.name && item.name.toLowerCase() === activeName.toLowerCase()) {
        continue;
      }
      const family = getWorkflowFamilyKey(item);
      if (prevFamily !== null && family !== prevFamily) {
        const sep = document.createElement("hr");
        select.appendChild(sep);
      }
      prevFamily = family;

      const isPack = Boolean(item.isPack || (item.id && item.id.startsWith("file:")));
      const isBrowse = Boolean(item.isDefaultLib);
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = item.name;
      if (isPack) {
        opt.setAttribute("data-source-kind", "pack");
      } else if (isBrowse) {
        opt.setAttribute("data-source-kind", "browse");
      }
      select.appendChild(opt);
    }

    select.addEventListener("change", (e) => {
      const chosenId = e.target.value;
      const chosen = allDiscoveredWorkflows.find((w) => w.id === chosenId);
      if (chosen) {
        activateWorkflowItem(chosen);
      }
    });

    return { select, matchedItem, activeName, enabledCount: enabledItems.length };
  }

  function computeFamilyCounts(workflows) {
    const map = new Map();
    for (const wf of workflows) {
      const label = getWorkflowFamilyLabel(wf);
      const key = label.toLowerCase();
      const existing = map.get(key);
      if (!existing) {
        map.set(key, { tag: label, key: key, count: 1 });
      } else {
        existing.count += 1;
      }
    }
    return Array.from(map.values()).sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag));
  }

  function renderEditCardControls(hostDiv, ta) {
    const oldList = hostDiv.querySelector(".inv7-wfs-checklist");
    const savedListScroll = oldList ? oldList.scrollTop : 0;
    const outerViewport = hostDiv.closest("[data-scope='scroll-area'][data-part='viewport']");
    const savedOuterScroll = outerViewport ? outerViewport.scrollTop : null;

    let wfState = parseWorkflowMarkerState(ta ? ta.value : "");
    const commitWfState = (nextState, shouldRebuildCard) => {
      wfState = nextState;
      if (ta) {
        setReactTextareaValue(ta, formatWorkflowMarkerState(wfState));
      }
      if (shouldRebuildCard) {
        renderEditCardControls(hostDiv, ta);
      } else {
        updatePreviewSelectInPlace();
      }
      for (const viewBox of document.querySelectorAll(".inv7-wfs-view-box")) {
        renderViewWidget(viewBox);
      }
    };

    hostDiv.innerHTML = "";

    const previewWrap = document.createElement("div");
    previewWrap.style.display = "flex";
    previewWrap.style.flexDirection = "column";
    previewWrap.style.gap = "6px";

    function updatePreviewSelectInPlace() {
      previewWrap.innerHTML = "";
      const { select, enabledCount } = buildDropdownSelectElement(wfState);
      const previewLbl = document.createElement("div");
      previewLbl.className = "inv7-wfs-label";
      previewLbl.textContent = "Preview Dropdown (" + enabledCount + " favorite workflows enabled in this workflow):";
      previewWrap.appendChild(previewLbl);
      previewWrap.appendChild(select);
    }

    updatePreviewSelectInPlace();
    hostDiv.appendChild(previewWrap);

    // Source toggle row: Your Library (cyan) + Built-in Browse (purple) + Node Pack Templates (amber/gold)
    const sourceRow = document.createElement("div");
    sourceRow.className = "inv7-wfs-toprow";
    sourceRow.style.flexWrap = "wrap";
    const sourceLbl = document.createElement("span");
    sourceLbl.className = "inv7-wfs-label";
    sourceLbl.textContent = "Source: Your Library (cyan)";

    const togglesGroup = document.createElement("div");
    togglesGroup.style.display = "flex";
    togglesGroup.style.flexWrap = "wrap";
    togglesGroup.style.gap = "6px";

    const browseToggleLbl = document.createElement("label");
    browseToggleLbl.className = "inv7-wfs-browse-toggle";
    browseToggleLbl.title = "Also list InvokeAI's built-in Browse workflows (highlighted in lavender/purple)";
    const browseCb = document.createElement("input");
    browseCb.type = "checkbox";
    browseCb.checked = Boolean(wfState.includeBrowse);
    browseCb.addEventListener("change", () => {
      commitWfState({ ...wfState, includeBrowse: Boolean(browseCb.checked) }, true);
    });
    const browseSpan = document.createElement("span");
    browseSpan.textContent = "+ Built-in (Browse)";
    browseToggleLbl.appendChild(browseCb);
    browseToggleLbl.appendChild(browseSpan);

    const packToggleLbl = document.createElement("label");
    packToggleLbl.className = "inv7-wfs-pack-toggle";
    packToggleLbl.title = "Also list unmodified factory templates from Node Pack folders (highlighted in amber/gold)";
    const packCb = document.createElement("input");
    packCb.type = "checkbox";
    packCb.checked = Boolean(wfState.includeNodePacks);
    packCb.addEventListener("change", () => {
      commitWfState({ ...wfState, includeNodePacks: Boolean(packCb.checked) }, true);
    });
    const packSpan = document.createElement("span");
    packSpan.textContent = "+ Node Pack Templates";
    packToggleLbl.appendChild(packCb);
    packToggleLbl.appendChild(packSpan);

    togglesGroup.appendChild(browseToggleLbl);
    togglesGroup.appendChild(packToggleLbl);
    sourceRow.appendChild(sourceLbl);
    sourceRow.appendChild(togglesGroup);
    hostDiv.appendChild(sourceRow);

    // Family Filter Chips (multi-select, persisted per workflow in wfState.activeTags)
    const pool = getPoolForWorkflowState(wfState);
    const familyCounts = computeFamilyCounts(pool);
    const validFamilyKeys = new Set(familyCounts.map((entry) => entry.key));
    const activeTags = (Array.isArray(wfState.activeTags) ? wfState.activeTags : []).filter((k) =>
      validFamilyKeys.has(k)
    );
    if (familyCounts.length > 0) {
      const chipStrip = document.createElement("div");
      chipStrip.className = "inv7-wfs-chip-strip";

      const makeChip = (label, tagKey, count) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "inv7-wfs-chip";
        const isActive =
          tagKey === null ? activeTags.length === 0 : activeTags.includes(tagKey);
        btn.setAttribute("data-active", isActive ? "true" : "false");
        btn.textContent = label;
        if (count !== null) {
          const cSpan = document.createElement("span");
          cSpan.className = "inv7-wfs-chip-count";
          cSpan.textContent = String(count);
          btn.appendChild(cSpan);
        }
        btn.addEventListener("click", () => {
          let nextTags;
          if (tagKey === null) {
            nextTags = [];
          } else if (activeTags.includes(tagKey)) {
            nextTags = activeTags.filter((t) => t !== tagKey);
          } else {
            nextTags = [...activeTags, tagKey];
          }
          commitWfState({ ...wfState, activeTags: nextTags }, true);
        });
        return btn;
      };

      chipStrip.appendChild(makeChip("All", null, pool.length));
      for (const entry of familyCounts) {
        chipStrip.appendChild(makeChip(entry.tag, entry.key, entry.count));
      }
      hostDiv.appendChild(chipStrip);
    }

    // Search filter input
    const searchRow = document.createElement("div");
    searchRow.className = "inv7-wfs-folder-row";
    const searchInput = document.createElement("input");
    searchInput.type = "text";
    searchInput.className = "inv7-wfs-folder-input";
    searchInput.placeholder = "Filter workflows by name or tag (e.g. LTX, Nano, Upscale)...";
    searchInput.value = searchFilterText;
    searchInput.addEventListener("input", () => {
      searchFilterText = searchInput.value || "";
      renderChecklistItems();
    });
    searchRow.appendChild(searchInput);
    hostDiv.appendChild(searchRow);

    const getVisibleWorkflows = () => {
      const q = (searchFilterText || "").trim().toLowerCase();
      return pool.filter((item) => {
        if (activeTags.length > 0) {
          const familyKey = getWorkflowFamilyKey(item);
          if (!activeTags.includes(familyKey)) return false;
        }
        if (q) {
          const inName = (item.name || "").toLowerCase().includes(q);
          const inTags = (item.tags || []).some((t) => t.toLowerCase().includes(q));
          if (!inName && !inTags) return false;
        }
        return true;
      });
    };

    const bulkRow = document.createElement("div");
    bulkRow.className = "inv7-wfs-toprow";
    bulkRow.style.flexWrap = "wrap";
    const bulkLbl = document.createElement("span");
    bulkLbl.className = "inv7-wfs-label";
    bulkLbl.textContent = "Check favorite workflows for this workflow:";
    const btnGroup = document.createElement("div");
    btnGroup.style.display = "flex";
    btnGroup.style.flexWrap = "wrap";
    btnGroup.style.gap = "4px";

    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "inv7-wfs-btn";
    allBtn.textContent = "All visible";
    allBtn.addEventListener("click", () => {
      const nextFavs = new Set(wfState.favorites);
      for (const w of getVisibleWorkflows()) {
        nextFavs.add(getItemFavoriteKey(w));
      }
      commitWfState({ ...wfState, favorites: Array.from(nextFavs) }, true);
    });

    const noneBtn = document.createElement("button");
    noneBtn.type = "button";
    noneBtn.className = "inv7-wfs-btn";
    noneBtn.textContent = "None visible";
    noneBtn.addEventListener("click", () => {
      const removeKeys = new Set();
      for (const w of getVisibleWorkflows()) {
        removeKeys.add(getItemFavoriteKey(w));
        removeKeys.add(((w && w.name) || "").trim().toLowerCase());
      }
      const nextFavs = (wfState.favorites || []).filter((k) => !removeKeys.has(k));
      commitWfState({ ...wfState, favorites: nextFavs }, true);
    });

    const saveGlobalBtn = document.createElement("button");
    saveGlobalBtn.type = "button";
    saveGlobalBtn.className = "inv7-wfs-btn";
    saveGlobalBtn.title = "Save this workflow's favorite selection as the Global Favorites preset for new/other workflows";
    saveGlobalBtn.textContent = "★ Save to Global";
    saveGlobalBtn.addEventListener("click", () => {
      switcherConfig.globalFavorites = [...(wfState.favorites || [])];
      switcherConfig.globalActiveTags = [...(wfState.activeTags || [])];
      switcherConfig.globalIncludeNodePacks = Boolean(wfState.includeNodePacks);
      switcherConfig.globalIncludeBrowse = Boolean(wfState.includeBrowse);
      saveConfig(switcherConfig);
      saveGlobalBtn.textContent = "✓ Saved Global!";
      window.setTimeout(() => {
        saveGlobalBtn.textContent = "★ Save to Global";
      }, 1400);
    });

    const loadGlobalBtn = document.createElement("button");
    loadGlobalBtn.type = "button";
    loadGlobalBtn.className = "inv7-wfs-btn";
    loadGlobalBtn.title = "Load your saved Global Favorites selection into this workflow";
    loadGlobalBtn.textContent = "↓ Load Global";
    loadGlobalBtn.addEventListener("click", () => {
      commitWfState(
        {
          ...wfState,
          favorites: Array.isArray(switcherConfig.globalFavorites) ? [...switcherConfig.globalFavorites] : [],
          activeTags: Array.isArray(switcherConfig.globalActiveTags) ? [...switcherConfig.globalActiveTags] : [],
          includeNodePacks: Boolean(switcherConfig.globalIncludeNodePacks),
          includeBrowse: Boolean(switcherConfig.globalIncludeBrowse)
        },
        true
      );
    });

    const updateYoursBtn = document.createElement("button");
    updateYoursBtn.type = "button";
    updateYoursBtn.className = "inv7-wfs-btn";
    updateYoursBtn.style.borderColor = "rgba(90, 200, 250, 0.45)";
    updateYoursBtn.style.color = "rgb(147, 226, 255)";
    updateYoursBtn.title = "Directly update this workflow in 'Your Library' in place (without creating a duplicate copy)";
    updateYoursBtn.textContent = "💾 Save / Update in Yours";
    updateYoursBtn.addEventListener("click", () => {
      saveOrUpdateCurrentWorkflowInYours(hostDiv, updateYoursBtn);
    });

    const rescanBtn = document.createElement("button");
    rescanBtn.type = "button";
    rescanBtn.className = "inv7-wfs-btn";
    rescanBtn.textContent = "↻ Refresh";
    rescanBtn.addEventListener("click", () => refreshAllWorkflows());

    btnGroup.appendChild(allBtn);
    btnGroup.appendChild(noneBtn);
    btnGroup.appendChild(saveGlobalBtn);
    btnGroup.appendChild(loadGlobalBtn);
    btnGroup.appendChild(updateYoursBtn);
    btnGroup.appendChild(rescanBtn);
    bulkRow.appendChild(bulkLbl);
    bulkRow.appendChild(btnGroup);
    hostDiv.appendChild(bulkRow);

    const listEl = document.createElement("div");
    listEl.className = "inv7-wfs-checklist";

    function renderChecklistItems() {
      listEl.innerHTML = "";
      const visible = getVisibleWorkflows();
      if (visible.length === 0) {
        const emptyMsg = document.createElement("div");
        emptyMsg.className = "inv7-wfs-desc";
        emptyMsg.textContent =
          pool.length === 0
            ? "No workflows found in Your Library yet. Save workflows to Your Library or enable '+ Node Pack Templates' above."
            : "No workflows match the current tag/search filter.";
        listEl.appendChild(emptyMsg);
        return;
      }
      let prevFamily = null;
      for (const item of visible) {
        const family = getWorkflowFamilyKey(item);
        const isPack = Boolean(item.isPack || (item.id && item.id.startsWith("file:")));
        const isBrowse = Boolean(item.isDefaultLib);
        const row = document.createElement("label");
        row.className = "inv7-wfs-checkitem";
        if (isPack) {
          row.setAttribute("data-source-kind", "pack");
        } else if (isBrowse) {
          row.setAttribute("data-source-kind", "browse");
        }
        if (prevFamily !== null && family !== prevFamily) {
          row.setAttribute("data-family-start", "true");
        }
        prevFamily = family;
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = isItemFavoritedInState(wfState, item);
        cb.addEventListener("change", () => {
          const favKey = getItemFavoriteKey(item);
          const nameKey = ((item && item.name) || "").trim().toLowerCase();
          const currentFavs = (wfState.favorites || []).filter((k) => k !== favKey && k !== nameKey);
          const nextFavs = cb.checked ? [...currentFavs, favKey] : currentFavs;
          commitWfState({ ...wfState, favorites: nextFavs }, false);
        });
        const nameSpan = document.createElement("span");
        nameSpan.className = "inv7-wfs-checkitem-name";
        nameSpan.textContent = item.name;
        nameSpan.title = item.name;
        const srcSpan = document.createElement("span");
        srcSpan.className = "inv7-wfs-source-tag";
        if (isPack) {
          srcSpan.setAttribute("data-source-kind", "pack");
        } else if (isBrowse) {
          srcSpan.setAttribute("data-source-kind", "browse");
        }
        srcSpan.textContent = isPack ? "Node Pack" : isBrowse ? "Built-in (Browse)" : "Your Library";
        row.appendChild(cb);
        row.appendChild(nameSpan);
        row.appendChild(srcSpan);
        listEl.appendChild(row);
      }
    }

    renderChecklistItems();
    hostDiv.appendChild(listEl);
    if (savedListScroll > 0) {
      listEl.scrollTop = savedListScroll;
    }
    if (outerViewport && savedOuterScroll !== null) {
      outerViewport.scrollTop = savedOuterScroll;
    }
  }

  let tempInfoOpen = false;

  function scanEditTabSwitcherCards() {
    // 1. Clean up any .inv7-wfs-edit-box whose card was reused by React for a non-switcher text element:
    for (const editBox of document.querySelectorAll(".inv7-wfs-edit-box")) {
      const card = editBox.parentElement;
      const ta = card ? card.querySelector("textarea") : null;
      if (!ta || !isSwitcherMarker(ta.value)) {
        if (ta) ta.style.display = "";
        const headerBar = card ? card.previousElementSibling : null;
        const oldChecks = headerBar ? headerBar.querySelector(".inv7-wfs-header-checks") : null;
        if (oldChecks) oldChecks.remove();
        editBox.remove();
      }
    }

    const formTextareas = Array.from(
      document.querySelectorAll("textarea[aria-label='Form text'], textarea[placeholder='Text']")
    );
    if (pendingStampNewTextElement && formTextareas.length > 0) {
      let targetTa = formTextareas.find((el) => !knownFormTextareas.has(el) && (el.value || "").trim() === "");
      if (!targetTa) {
        const lastTa = formTextareas[formTextareas.length - 1];
        if ((lastTa.value || "").trim() === "") {
          targetTa = lastTa;
        }
      }
      if (targetTa) {
        pendingStampNewTextElement = false;
        knownFormTextareas.clear();
        setReactTextareaValue(targetTa, formatWorkflowMarkerState(createInitialMarkerStateFromGlobal()));
      }
    }

    const textareas = formTextareas.length > 0 ? formTextareas : document.querySelectorAll("textarea");

    for (const ta of textareas) {
      const val = (ta.value || "").trim();
      if (!isSwitcherMarker(val)) continue;

      ta.style.display = "none";
      const card = ta.parentElement;
      if (!card) continue;

      const headerBar = card.previousElementSibling;
      if (headerBar) {
        const titleEl = headerBar.querySelector("p");
        if (titleEl && titleEl.textContent !== "⇄ Favorite Workflows") {
          titleEl.textContent = "⇄ Favorite Workflows";
        }
        const actionsBar = headerBar.lastElementChild;
        if (actionsBar) {
          const wfState = parseWorkflowMarkerState(ta.value);
          let checksWrap = actionsBar.querySelector(".inv7-wfs-header-checks");
          if (!checksWrap) {
            checksWrap = document.createElement("div");
            checksWrap.className = "inv7-wfs-header-checks";
            checksWrap.addEventListener("pointerdown", (e) => e.stopPropagation());
            checksWrap.addEventListener("keydown", (e) => e.stopPropagation());

            // 1. Colorized checkbox
            const colorLbl = document.createElement("label");
            colorLbl.className = "inv7-wfs-header-check";
            colorLbl.title = "Adopt custom accordion & input brightness settings (when off, keep original dark look)";
            const colorCb = document.createElement("input");
            colorCb.type = "checkbox";
            colorCb.setAttribute("data-flag", "colorized");
            colorCb.checked = wfState.colorized;
            const colorSpan = document.createElement("span");
            colorSpan.textContent = "Colorized";
            colorLbl.appendChild(colorCb);
            colorLbl.appendChild(colorSpan);

            // 2. Extra Info checkbox
            const infoLbl = document.createElement("label");
            infoLbl.className = "inv7-wfs-header-check";
            infoLbl.title = "Always show workflow description below dropdown (when off, use the ⓘ icon in View)";
            const infoCb = document.createElement("input");
            infoCb.type = "checkbox";
            infoCb.setAttribute("data-flag", "showInfo");
            infoCb.checked = wfState.showInfo;
            const infoSpan = document.createElement("span");
            infoSpan.textContent = "Extra Info";
            infoLbl.appendChild(infoCb);
            infoLbl.appendChild(infoSpan);

            const updateHeaderFlags = () => {
              const latest = parseWorkflowMarkerState(ta.value);
              const nextState = {
                ...latest,
                colorized: Boolean(colorCb.checked),
                showInfo: Boolean(infoCb.checked)
              };
              setReactTextareaValue(ta, formatWorkflowMarkerState(nextState));
              for (const viewBox of document.querySelectorAll(".inv7-wfs-view-box")) {
                renderViewWidget(viewBox);
              }
            };
            colorCb.addEventListener("change", updateHeaderFlags);
            infoCb.addEventListener("change", updateHeaderFlags);

            checksWrap.appendChild(colorLbl);
            checksWrap.appendChild(infoLbl);
            actionsBar.insertBefore(checksWrap, actionsBar.firstChild);
          } else {
            const colorCb = checksWrap.querySelector("input[data-flag='colorized']");
            const infoCb = checksWrap.querySelector("input[data-flag='showInfo']");
            if (colorCb && colorCb.checked !== wfState.colorized) colorCb.checked = wfState.colorized;
            if (infoCb && infoCb.checked !== wfState.showInfo) infoCb.checked = wfState.showInfo;
          }
        }
      }

      let hostDiv = card.querySelector(".inv7-wfs-edit-box");
      if (!hostDiv) {
        hostDiv = document.createElement("div");
        hostDiv.className = "inv7-wfs-edit-box";
        card.appendChild(hostDiv);
        renderEditCardControls(hostDiv, ta);
      } else if (hostDiv.getAttribute("data-bound-marker") !== val) {
        hostDiv.setAttribute("data-bound-marker", val);
        renderEditCardControls(hostDiv, ta);
      }
    }
  }

  /* ==========================================================================
   * 3. RENDER DROPDOWN IN THE "VIEW" TAB WHERE "[workflow-switcher]" IS PLACED
   * ========================================================================== */
  function renderViewWidget(hostDiv) {
    hostDiv.innerHTML = "";
    const prevP = hostDiv.previousElementSibling;
    const markerText = prevP && prevP.tagName === "P" ? prevP.textContent || "" : "";
    const wfState = parseWorkflowMarkerState(markerText);
    hostDiv.setAttribute("data-colorized", wfState.colorized ? "true" : "false");
    hostDiv.setAttribute("data-showinfo", wfState.showInfo ? "true" : "false");
    const alwaysShowInfo = wfState.showInfo;
    const { select, matchedItem, activeName } = buildDropdownSelectElement(wfState);
    const activeLabel = matchedItem ? matchedItem.name : activeName;
    const hasDescription = Boolean(matchedItem && matchedItem.description);
    const isDescVisible = hasDescription && (alwaysShowInfo || tempInfoOpen);

    const isOpen = !switcherConfig.collapsedInView;
    hostDiv.setAttribute("data-open", isOpen ? "true" : "false");

    const headerRow = document.createElement("div");
    headerRow.className = "inv7-wfs-header-row";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "inv7-wfs-acc-trigger";
    trigger.setAttribute("aria-expanded", isOpen ? "true" : "false");

    const chevron = document.createElement("span");
    chevron.className = "inv7-wfs-acc-chevron";
    chevron.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>`;

    const title = document.createElement("span");
    title.className = "inv7-wfs-label";
    title.textContent = "⇄ Favorite Workflows";

    trigger.appendChild(chevron);
    trigger.appendChild(title);

    if (!isOpen && activeLabel) {
      const badge = document.createElement("span");
      badge.className = "inv7-wfs-acc-badge";
      badge.textContent = activeLabel;
      trigger.appendChild(badge);
    }

    trigger.addEventListener("click", () => {
      switcherConfig.collapsedInView = !switcherConfig.collapsedInView;
      saveConfig(switcherConfig);
      renderViewWidget(hostDiv);
    });

    headerRow.appendChild(trigger);

    if (isOpen && hasDescription && !alwaysShowInfo) {
      const infoBtn = document.createElement("button");
      infoBtn.type = "button";
      infoBtn.className = "inv7-wfs-info-btn";
      infoBtn.setAttribute("aria-label", "Toggle workflow description");
      infoBtn.setAttribute("aria-pressed", isDescVisible ? "true" : "false");
      infoBtn.title = isDescVisible ? "Hide workflow description" : "Show workflow description";
      infoBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>`;
      infoBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        tempInfoOpen = !tempInfoOpen;
        renderViewWidget(hostDiv);
      });
      headerRow.appendChild(infoBtn);
    }

    const body = document.createElement("div");
    body.className = "inv7-wfs-acc-body";
    body.appendChild(select);
    if (isDescVisible) {
      const descEl = document.createElement("div");
      descEl.className = "inv7-wfs-desc";
      descEl.textContent = matchedItem.description;
      body.appendChild(descEl);
    }

    hostDiv.appendChild(headerRow);
    hostDiv.appendChild(body);
  }

  function scanViewTabSwitcherElements() {
    // 1. Remove any stale .inv7-wfs-view-box left behind when switching to a workflow without a switcher:
    for (const box of document.querySelectorAll(".inv7-wfs-view-box")) {
      const prev = box.previousElementSibling;
      if (!prev || prev.tagName !== "P" || !isSwitcherMarker(prev.textContent)) {
        box.remove();
      }
    }

    // 2. Restore any paragraph that was previously hidden if its text is no longer a switcher marker:
    for (const p of document.querySelectorAll("[role='region'] p[data-inv7-wfs-hidden='true']")) {
      if (!isSwitcherMarker(p.textContent)) {
        p.style.display = "";
        p.removeAttribute("data-inv7-wfs-hidden");
      }
    }

    const paragraphs = document.querySelectorAll("[role='region'] p");
    for (const p of paragraphs) {
      const txt = (p.textContent || "").trim();
      if (!isSwitcherMarker(txt)) continue;

      p.style.display = "none";
      p.setAttribute("data-inv7-wfs-hidden", "true");
      let viewBox = p.nextElementSibling;
      if (!viewBox || !viewBox.classList.contains("inv7-wfs-view-box")) {
        viewBox = document.createElement("div");
        viewBox.className = "inv7-wfs-view-box";
        viewBox.setAttribute("data-bound-marker", txt);
        p.insertAdjacentElement("afterend", viewBox);
        renderViewWidget(viewBox);
      } else if (viewBox.getAttribute("data-bound-marker") !== txt) {
        viewBox.setAttribute("data-bound-marker", txt);
        renderViewWidget(viewBox);
      }
    }
  }

  function rehydrateAllSwitcherWidgets() {
    for (const editBox of document.querySelectorAll(".inv7-wfs-edit-box")) {
      const card = editBox.parentElement;
      const ta = card ? card.querySelector("textarea") : null;
      renderEditCardControls(editBox, ta);
    }
    for (const viewBox of document.querySelectorAll(".inv7-wfs-view-box")) {
      renderViewWidget(viewBox);
    }
  }

  let initialScanTriggered = false;
  let rafPending = false;

  document.addEventListener(
    "click",
    (e) => {
      const target = e.target instanceof Element ? e.target : null;
      if (target && target.closest("[data-scope='collapsible'] [data-part='trigger']")) {
        window.setTimeout(captureActiveWorkflowSectionStates, 40);
      }
    },
    true
  );

  function scheduleCheck() {
    if (rafPending) return;
    rafPending = true;
    window.requestAnimationFrame(() => {
      rafPending = false;
      injectSwitcherStyles();
      syncSwitcherSurfaceColor();
      augmentAddFormElementMenus();
      scanEditTabSwitcherCards();
      scanViewTabSwitcherElements();
      syncSavedOpenSectionsForActiveWorkflow();
      if (!initialScanTriggered) {
        initialScanTriggered = true;
        refreshAllWorkflows();
      }
    });
  }

  scheduleCheck();
  const observer = new MutationObserver(scheduleCheck);
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
"""

_WORKFLOW_SWITCHER_JS_BYTES: bytes = _WORKFLOW_SWITCHER_JS.strip().encode("utf-8")
