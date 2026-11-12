"""In-memory ASGI Workflow Switcher Form-Element extension for InvokeAI 7 (`webv2`).

How it integrates natively into InvokeAI 7's Form Builder (`Edit` tab) and Form View (`View` tab):
1. **`+ Add form element` Menu Integration:**
   When the user clicks `+ Add form element` in the `Edit` tab, our script injects a 6th menu item:
   **`⇄ Workflow Switcher`** right below `Container (row)`.
2. **In the `Edit` Tab (Form Builder):**
   Clicking `⇄ Workflow Switcher` adds a standard form element carrying a `[workflow-switcher]` marker.
   Inside that movable, deletable Form Builder card, the user can:
   - Check (`[x]`) or uncheck which workflows (from Your Library, Built-in Browse, or Node Pack Templates)
     should be included in this switcher dropdown,
   - Drag the `⇄ Workflow Switcher` card anywhere in the form (e.g. to the very top) or delete it with `×`.
3. **In the `View` Tab (Daily Use):**
   Where the `[workflow-switcher]` element sits in the form, the user sees ONLY the clean
   **Workflow Switcher Dropdown** showing the checked workflows for instant 1-click switching!

Single- and multi-user mode & Security architecture (v0.4.0):
- Every API call from the browser carries the signed-in user's Bearer token, so in multi-user mode the
  library listing and "Save / Update in Yours" act as that user, exactly like InvokeAI's own UI.
- The listing shows the user's own workflows plus workflows other users shared (`is_public`).
  Saving only ever updates a workflow the user owns; anything else becomes a new own copy.
- `/workflow-switcher/scan` and `/workflow-switcher/file` authenticate through InvokeAI's own
  `get_current_user_or_default` and allow `GET` requests only.
- Zero client control over filesystem paths: only server-configured workflow directories
  (`<INVOKE_ROOT>/nodes/<pack>` and `<INVOKE_ROOT>/workflows`) are scanned. Client-supplied `folder` or
  `path` parameters are never accepted.
- Zero server path disclosure: `/workflow-switcher/scan` never exposes filesystem paths to the client;
  discovered templates are referenced exclusively by opaque 16-character SHA-256 IDs (`file:<id>`)
  resolved against a server-maintained allowlist.
- Strict traversal & resource bounds: directory scanning uses bounded depth (`MAX_SCAN_DEPTH = 3`),
  skips symlinks, enforces a pre-read file size cap (`MAX_WORKFLOW_FILE_BYTES = 2 MiB`) and file count
  limits, caches results in memory (`SCAN_CACHE_TTL_SECONDS = 5.0`), and runs off the ASGI event loop
  via `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

SWITCHER_VERSION: str = "0.5.0"
SWITCHER_SCRIPT_PATH: str = "/workflow-switcher.js"
SWITCHER_SCAN_PATH: str = "/workflow-switcher/scan"
SWITCHER_FILE_PATH: str = "/workflow-switcher/file"
SWITCHER_SCRIPT_TAG: bytes = b'<script src="/workflow-switcher.js" defer></script></body>'


_IGNORED_SCAN_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "tests",
        "test",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        "models",
        "assets",
        "overlays",
        "tools",
    }
)
# Hard resource bounds for server-side node-pack workflow discovery:
MAX_SCAN_DEPTH: int = 3
MAX_WORKFLOW_FILE_BYTES: int = 2 * 1024 * 1024  # 2 MiB cap checked via stat() before reading
MAX_JSON_FILES_PER_FOLDER: int = 200
MAX_TOTAL_SCANNED_FILES: int = 500
MAX_VISITED_ENTRIES: int = 5000
SCAN_CACHE_TTL_SECONDS: float = 5.0
_OPAQUE_FILE_ID_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{16}$")

_SCAN_LOCK = threading.RLock()
_DISCOVERED_WORKFLOW_ALLOWLIST: dict[str, Path] = {}
_CACHED_SCAN_ITEMS: list[dict[str, Any]] | None = None
_CACHED_SCAN_TIMESTAMP: float = 0.0
_CACHED_SCAN_ROOTS_KEY: tuple[str, ...] = ()


@dataclass(frozen=True)
class SwitcherRequestUser:
    """Who is asking, as InvokeAI's own authentication sees it."""

    user_id: str
    is_admin: bool


def _is_symlink_or_junction(path: Path) -> bool:
    """Return True if `path` is a symlink or a Windows NTFS junction point."""
    try:
        if path.is_symlink():
            return True
        is_junc = getattr(path, "is_junction", None)
        if callable(is_junc) and bool(is_junc()):
            return True
    except OSError:
        return True
    return False


def _is_within_allowed_roots(resolved_path: Path, allowed_roots: list[Path]) -> bool:
    """Return True only when `resolved_path` resides inside at least one of `allowed_roots`."""
    return any(resolved_path.is_relative_to(root) for root in allowed_roots)


def get_default_workflow_dirs() -> list[Path]:
    """Locate server-configured `<INVOKE_ROOT>/nodes/*` and `<INVOKE_ROOT>/workflows` directories."""
    dirs: list[Path] = []
    try:
        nodes_dir = Path(__file__).resolve().parents[1]
        if nodes_dir.is_dir() and not _is_symlink_or_junction(nodes_dir):
            for child in sorted(nodes_dir.iterdir()):
                if (
                    child.is_dir()
                    and not _is_symlink_or_junction(child)
                    and not child.name.startswith(".")
                    and child.name.lower() not in _IGNORED_SCAN_DIR_NAMES
                ):
                    if child not in dirs:
                        dirs.append(child)
        invoke_root = Path(__file__).resolve().parents[2]
        root_wf = invoke_root / "workflows"
        if root_wf.is_dir() and not _is_symlink_or_junction(root_wf) and root_wf not in dirs:
            dirs.append(root_wf)
    except IndexError:
        pass
    return dirs


def _compute_workflow_file_id(resolved_path: Path) -> str:
    """Return a deterministic, opaque 16-hex-char identifier for a resolved server workflow path."""
    return hashlib.sha256(str(resolved_path).encode("utf-8")).hexdigest()[:16]


def _iter_workflow_json_files(root_folder: Path, *, max_depth: int = MAX_SCAN_DEPTH) -> list[Path]:
    """Traverse `root_folder` up to `max_depth` levels for `.json` files within size/count limits."""
    found: list[Path] = []
    if not root_folder.is_dir() or _is_symlink_or_junction(root_folder):
        return found

    queue: deque[tuple[Path, int]] = deque([(root_folder, 0)])
    visited_entries = 0
    while queue and len(found) < MAX_JSON_FILES_PER_FOLDER and visited_entries < MAX_VISITED_ENTRIES:
        current_dir, depth = queue.popleft()
        try:
            entries = sorted(current_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue

        for entry in entries:
            visited_entries += 1
            if visited_entries > MAX_VISITED_ENTRIES:
                break
            if entry.name.startswith("."):
                continue
            try:
                if _is_symlink_or_junction(entry):
                    continue
                if entry.is_dir():
                    if depth < max_depth and entry.name.lower() not in _IGNORED_SCAN_DIR_NAMES:
                        queue.append((entry, depth + 1))
                elif entry.is_file() and entry.suffix.lower() == ".json":
                    st = entry.stat()
                    if 0 < st.st_size <= MAX_WORKFLOW_FILE_BYTES:
                        found.append(entry)
                        if len(found) >= MAX_JSON_FILES_PER_FOLDER:
                            break
            except OSError:
                continue

    return found


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


def invalidate_workflow_scan_cache() -> None:
    """Clear the in-memory workflow scan cache and allowlist."""
    global _CACHED_SCAN_ITEMS, _CACHED_SCAN_TIMESTAMP, _CACHED_SCAN_ROOTS_KEY
    with _SCAN_LOCK:
        _CACHED_SCAN_ITEMS = None
        _CACHED_SCAN_TIMESTAMP = 0.0
        _CACHED_SCAN_ROOTS_KEY = ()
        _DISCOVERED_WORKFLOW_ALLOWLIST.clear()


def scan_workflow_directories(*, use_cache: bool = False) -> list[dict[str, Any]]:
    """Scan server-configured workflow directories and populate the server-side file allowlist."""
    global _CACHED_SCAN_ITEMS, _CACHED_SCAN_TIMESTAMP, _CACHED_SCAN_ROOTS_KEY

    with _SCAN_LOCK:
        default_dirs = get_default_workflow_dirs()
        roots_key = tuple(str(d) for d in default_dirs)
        now = time.monotonic()
        if (
            use_cache
            and _CACHED_SCAN_ITEMS is not None
            and _CACHED_SCAN_ROOTS_KEY == roots_key
            and (now - _CACHED_SCAN_TIMESTAMP) < SCAN_CACHE_TTL_SECONDS
        ):
            return [dict(item) for item in _CACHED_SCAN_ITEMS]

        allowed_roots = [d.resolve() for d in default_dirs]
        search_dirs: list[tuple[str, Path]] = []
        for d in default_dirs:
            label = "Node Pack" if d.name != "workflows" else "InvokeAI Workflows"
            search_dirs.append((label, d))

        results: list[dict[str, Any]] = []
        new_allowlist: dict[str, Path] = {}
        seen_paths: set[str] = set()

        for source_label, folder in search_dirs:
            if len(results) >= MAX_TOTAL_SCANNED_FILES:
                break
            for json_file in _iter_workflow_json_files(folder):
                if len(results) >= MAX_TOTAL_SCANNED_FILES:
                    break
                if json_file.name.endswith("(PoC).json"):
                    continue
                try:
                    resolved = json_file.resolve()
                    if not _is_within_allowed_roots(resolved, allowed_roots):
                        continue
                    resolved_str = str(resolved)
                    if resolved_str in seen_paths:
                        continue
                    st = resolved.stat()
                    if st.st_size <= 0 or st.st_size > MAX_WORKFLOW_FILE_BYTES:
                        continue
                    data = json.loads(resolved.read_text(encoding="utf-8"))
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
                            if isinstance(el, dict) and el.get("type") in ("heading", "collapsible"):
                                el_data = el.get("data")
                                if isinstance(el_data, dict):
                                    h_text = str(el_data.get("content") or "").strip()
                                    if h_text:
                                        headings.append(h_text.lower())
                    file_id = _compute_workflow_file_id(resolved)
                    seen_paths.add(resolved_str)
                    new_allowlist[file_id] = resolved
                    results.append(
                        {
                            "id": f"file:{file_id}",
                            "file_id": file_id,
                            "name": name,
                            "description": desc,
                            "tags": tags,
                            "headings": headings,
                            "source": source_label,
                            "filename": json_file.name,
                        }
                    )
                except Exception:
                    continue

        results.sort(key=lambda item: (str(item["source"]).lower(), str(item["name"]).lower()))
        _DISCOVERED_WORKFLOW_ALLOWLIST.clear()
        _DISCOVERED_WORKFLOW_ALLOWLIST.update(new_allowlist)
        _CACHED_SCAN_ITEMS = [dict(item) for item in results]
        _CACHED_SCAN_TIMESTAMP = time.monotonic()
        _CACHED_SCAN_ROOTS_KEY = roots_key
        return results


def read_workflow_file_bytes(file_id: str) -> tuple[bytes, str] | None:
    """Safely read a server-allowlisted workflow JSON file by its opaque `file_id`."""
    cleaned_id = unquote(file_id or "").strip().lower()
    if cleaned_id.startswith("file:"):
        cleaned_id = cleaned_id[5:]
    if not _OPAQUE_FILE_ID_RE.fullmatch(cleaned_id):
        return None

    with _SCAN_LOCK:
        candidate = _DISCOVERED_WORKFLOW_ALLOWLIST.get(cleaned_id)
        if candidate is None:
            scan_workflow_directories(use_cache=True)
            candidate = _DISCOVERED_WORKFLOW_ALLOWLIST.get(cleaned_id)
    if candidate is None:
        return None

    try:
        if _is_symlink_or_junction(candidate) or candidate.suffix.lower() != ".json" or not candidate.is_file():
            return None
        resolved = candidate.resolve()
        allowed_roots = [d.resolve() for d in get_default_workflow_dirs()]
        # No allowed root means nothing may be read (fail closed).
        if not _is_within_allowed_roots(resolved, allowed_roots):
            return None
        st = resolved.stat()
        if st.st_size <= 0 or st.st_size > MAX_WORKFLOW_FILE_BYTES:
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
    log.info(
        f"Loaded invoke_workflow_switcher v{SWITCHER_VERSION} (+ Add form element -> Workflow Switcher with Collapsible toggle)"
    )
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
        method: str = str(scope.get("method", "GET") or "GET").upper()
        query_string: str = (scope.get("query_string") or b"").decode("utf-8", errors="ignore")

        if path == SWITCHER_SCRIPT_PATH or path.endswith("/workflow-switcher.js"):
            if method != "GET":
                await _send_response(send, b'{"error":"Method not allowed"}', b"application/json", status=405)
                return
            await _send_response(send, _WORKFLOW_SWITCHER_JS_BYTES, b"application/javascript; charset=utf-8")
            return

        if path in (SWITCHER_SCAN_PATH, SWITCHER_FILE_PATH):
            if method != "GET":
                await _send_response(send, b'{"error":"Method not allowed"}', b"application/json", status=405)
                return
            user = resolve_request_user(scope)
            if user is None:
                await _send_response(send, b'{"error":"Authentication required"}', b"application/json", status=401)
                return
            if path == SWITCHER_SCAN_PATH:
                items = await asyncio.to_thread(scan_workflow_directories, use_cache=True)
                payload = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
                await _send_response(send, payload, b"application/json; charset=utf-8")
                return
            params = parse_qs(query_string)
            target_id = (params.get("id") or [""])[0]
            res = await asyncio.to_thread(read_workflow_file_bytes, target_id)
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


def resolve_request_user(scope: dict[str, Any]) -> SwitcherRequestUser | None:
    """Authenticate a raw ASGI request exactly like InvokeAI's REST routes do.

    Delegates to InvokeAI's `get_current_user_or_default`: in single-user mode that is always the admin
    `system`; in multi-user mode the Bearer token must belong to an existing, active, non-revoked account.
    Returns None when the request is not authorized. Fails closed in multi-user mode if InvokeAI's
    authentication cannot be imported.
    """
    token = _bearer_token_from_scope(scope)
    try:
        from fastapi import HTTPException
        from fastapi.security import HTTPAuthorizationCredentials

        from invokeai.app.api.auth_dependencies import get_current_user_or_default
    except ImportError:
        return _fallback_user_without_auth_module()

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None
    try:
        token_data = get_current_user_or_default(credentials)
    except HTTPException:
        return None
    except Exception:
        return None
    return SwitcherRequestUser(user_id=str(token_data.user_id), is_admin=bool(token_data.is_admin))


def _bearer_token_from_scope(scope: dict[str, Any]) -> str | None:
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
    return None


def _fallback_user_without_auth_module() -> SwitcherRequestUser | None:
    """Without InvokeAI's auth module only single-user mode can be trusted."""
    try:
        from invokeai.app.services.config.config_default import get_config

        multiuser = bool(getattr(get_config(), "multiuser", False))
    except Exception:
        return None
    return None if multiuser else SwitcherRequestUser(user_id="system", is_admin=True)


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

  // Presets are kept per user id; the pre-0.3.0 shared key only seeds a user's first load.
  const STORAGE_KEY_BASE = "invoke7_workflow_switcher_config_v2";
  // Where InvokeAI 7's webv2 keeps the signed-in user's JWT (features/identity/core/tokenStorage.ts).
  const AUTH_TOKEN_STORAGE_KEY = "auth_token";
  const SINGLE_USER_ID = "system";
  const MARKER_PREFIX = "[workflow-switcher";
  const MARKER_DEFAULT = "[workflow-switcher]";

  function isSwitcherMarker(text) {
    const t = (text || "").trim().toLowerCase();
    return t === "[workflow-switcher]" || t.startsWith("[workflow-switcher:") || t.startsWith("[workflow-switcher]");
  }

  function readAuthToken() {
    try {
      return window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
    } catch (_) {
      return null;
    }
  }

  // Same credential InvokeAI's own UI sends; in single-user mode there is no token and none is needed.
  function apiFetch(url, init) {
    const options = { cache: "no-store", ...(init || {}) };
    const headers = new Headers(options.headers || {});
    const token = readAuthToken();
    if (token && !headers.has("Authorization")) headers.set("Authorization", "Bearer " + token);
    return fetch(url, { ...options, headers });
  }

  // Display/ownership hint only; the server enforces every permission itself.
  function currentUserId() {
    const token = readAuthToken();
    if (!token) return SINGLE_USER_ID;
    try {
      const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const binary = atob(payload.padEnd(payload.length + ((4 - (payload.length % 4)) % 4), "="));
      const claims = JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0))));
      return String(claims.user_id || SINGLE_USER_ID);
    } catch (_) {
      return SINGLE_USER_ID;
    }
  }

  function storageKeyForCurrentUser() {
    return STORAGE_KEY_BASE + ":" + currentUserId();
  }

  function createWorkflowGroup(name, overrides) {
    const cleanName = String(name || "Default Group").trim() || "Default Group";
    const id =
      (overrides && overrides.id) ||
      "grp_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 7);
    const normalizedLabels = {};
    if (overrides && overrides.favoriteLabels && typeof overrides.favoriteLabels === "object") {
      for (const [k, v] of Object.entries(overrides.favoriteLabels)) {
        const cleanKey = String(k || "").trim().toLowerCase();
        if (cleanKey && v) {
          normalizedLabels[cleanKey] = String(v);
        }
      }
    }
    return {
      id: String(id),
      name: cleanName,
      favorites:
        overrides && Array.isArray(overrides.favorites)
          ? Array.from(new Set(overrides.favorites.map((f) => String(f).trim().toLowerCase()).filter(Boolean)))
          : [],
      favoriteLabels: normalizedLabels,
      activeTags:
        overrides && Array.isArray(overrides.activeTags)
          ? overrides.activeTags.map((t) => String(t).toLowerCase())
          : [],
      includeNodePacks: Boolean(overrides && overrides.includeNodePacks),
      includeBrowse: Boolean(overrides && overrides.includeBrowse)
    };
  }

  function normalizeGroupsConfig(parsed) {
    const src = parsed && typeof parsed === "object" ? parsed : {};
    let groups = [];
    const seenIds = new Set();
    if (Array.isArray(src.groups) && src.groups.length > 0) {
      for (let idx = 0; idx < src.groups.length; idx++) {
        const g = src.groups[idx];
        if (!g || typeof g !== "object") continue;
        const candidate = createWorkflowGroup(g.name || "Group " + (idx + 1), g);
        if (seenIds.has(candidate.id)) continue;
        seenIds.add(candidate.id);
        groups.push(candidate);
      }
    }
    if (groups.length === 0) {
      const hasLegacyV4 =
        Array.isArray(src.globalFavorites) ||
        Array.isArray(src.globalActiveTags) ||
        typeof src.globalIncludeNodePacks === "boolean" ||
        typeof src.globalIncludeBrowse === "boolean";
      if (hasLegacyV4) {
        groups = [
          createWorkflowGroup("Default Group", {
            id: "grp_default",
            favorites: Array.isArray(src.globalFavorites) ? src.globalFavorites : [],
            favoriteLabels: src.favoriteLabels || {},
            activeTags: Array.isArray(src.globalActiveTags) ? src.globalActiveTags : [],
            includeNodePacks: Boolean(src.globalIncludeNodePacks),
            includeBrowse: Boolean(src.globalIncludeBrowse)
          })
        ];
      } else {
        // Fresh start: empty Default Group (no pre-populated foreign workflows).
        groups = [createWorkflowGroup("Default Group", { id: "grp_default" })];
      }
    }
    const requestedActiveId = typeof src.activeGroupId === "string" ? src.activeGroupId : "";
    const activeGroup = groups.find((g) => g.id === requestedActiveId) || groups[0];
    return {
      groups: groups,
      activeGroupId: activeGroup.id,
      savedSectionsByWorkflow:
        src.savedSectionsByWorkflow && typeof src.savedSectionsByWorkflow === "object"
          ? src.savedSectionsByWorkflow
          : {},
      collapsedInView: Boolean(src.collapsedInView)
    };
  }

  function loadConfig() {
    try {
      const raw =
        window.localStorage.getItem(storageKeyForCurrentUser()) || window.localStorage.getItem(STORAGE_KEY_BASE);
      if (raw) {
        return normalizeGroupsConfig(JSON.parse(raw));
      }
    } catch (_) {}
    return normalizeGroupsConfig(null);
  }

  function saveConfig(cfg) {
    try {
      window.localStorage.setItem(storageKeyForCurrentUser(), JSON.stringify(cfg));
    } catch (_) {}
  }

  let switcherConfig = loadConfig();
  let switcherConfigUserId = currentUserId();

  // Another account signed in within this tab: switch to that user's presets.
  function reloadConfigIfUserChanged() {
    const userId = currentUserId();
    if (userId === switcherConfigUserId) return false;
    switcherConfig = loadConfig();
    switcherConfigUserId = userId;
    return true;
  }

  function getActiveGroup() {
    if (!Array.isArray(switcherConfig.groups) || switcherConfig.groups.length === 0) {
      switcherConfig = normalizeGroupsConfig(switcherConfig);
    }
    return (
      switcherConfig.groups.find((g) => g.id === switcherConfig.activeGroupId) || switcherConfig.groups[0]
    );
  }

  function updateGroupById(groupId, patch) {
    if (reloadConfigIfUserChanged()) return null;
    const target = switcherConfig.groups.find((g) => g.id === groupId);
    if (!target) return null;
    const updated = createWorkflowGroup(
      patch && typeof patch.name === "string" ? patch.name : target.name,
      { ...target, ...(patch || {}), id: target.id }
    );
    switcherConfig.groups = switcherConfig.groups.map((g) => (g.id === target.id ? updated : g));
    saveConfig(switcherConfig);
    return updated;
  }

  let allDiscoveredWorkflows = [];
  let hasCompletedInitialScan = false;
  let lastScanFinishedAt = 0;
  let lastScanStatus = { lib: false, browse: false, pack: false };
  let activeScanPromise = null;
  let pendingRehydrateAfterSelect = false;
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

  function humanizeFavoriteKey(favKey, favoriteLabels) {
    const cleanKey = String(favKey || "").trim().toLowerCase();
    if (favoriteLabels && typeof favoriteLabels === "object") {
      if (favoriteLabels[cleanKey]) return String(favoriteLabels[cleanKey]);
      if (favoriteLabels[favKey]) return String(favoriteLabels[favKey]);
    }
    const raw = String(favKey || "").trim();
    const colonIdx = raw.indexOf(":");
    const stripped = colonIdx !== -1 ? raw.slice(colonIdx + 1).trim() : raw;
    return stripped || raw;
  }

  function findMissingFavoritesForGroup(group, discoveredWorkflows, scanStatus) {
    if (!group || !Array.isArray(group.favorites) || group.favorites.length === 0) {
      return [];
    }
    const status =
      scanStatus && typeof scanStatus === "object"
        ? scanStatus
        : { lib: true, browse: true, pack: true };
    const list = Array.isArray(discoveredWorkflows) ? discoveredWorkflows : [];
    const presentKeys = new Set();
    const presentLibNames = new Set();
    const presentAllNames = new Set();
    for (const item of list) {
      const itemKey = getItemFavoriteKey(item);
      presentKeys.add(itemKey);
      const nm = ((item && item.name) || "").trim().toLowerCase();
      if (nm) {
        presentAllNames.add(nm);
        if (itemKey.startsWith("lib:")) {
          presentLibNames.add(nm);
        }
      }
    }
    const missing = [];
    const seen = new Set();
    for (const favKey of group.favorites) {
      const k = String(favKey || "").trim().toLowerCase();
      if (!k || seen.has(k)) continue;
      seen.add(k);
      const colonIdx = k.indexOf(":");
      const prefix = colonIdx !== -1 ? k.slice(0, colonIdx) : "";
      const namePart = colonIdx !== -1 ? k.slice(colonIdx + 1).trim() : k;
      const sourceKind = prefix === "pack" ? "pack" : prefix === "browse" ? "browse" : "lib";

      // Only report a workflow as missing when its source endpoint completed without error:
      if (sourceKind === "lib" && !status.lib) continue;
      if (sourceKind === "browse" && !status.browse) continue;
      if (sourceKind === "pack" && !status.pack) continue;

      if (prefix === "lib") {
        if (presentKeys.has(k)) continue;
        // If the user deleted their Library copy of a Node Pack workflow while Node Pack Templates are enabled
        // in this group, the workflow seamlessly falls back to the yellow Node Pack template instead of missing:
        if (group.includeNodePacks && namePart && presentKeys.has("pack:" + namePart) && status.pack) continue;
        if (group.includeBrowse && namePart && presentKeys.has("browse:" + namePart) && status.browse) continue;
      } else if (prefix === "browse" || prefix === "pack") {
        // When a user saves a Browse or Node Pack workflow to Your Library, refreshAllWorkflows deduplicates it
        // in favour of the `lib:` entry; treat it as present if either the original key or `lib:<name>` exists.
        if (presentKeys.has(k) || (namePart && presentLibNames.has(namePart) && status.lib)) continue;
      } else {
        // Legacy unprefixed entry:
        if (namePart && presentAllNames.has(namePart)) continue;
      }

      missing.push({
        key: k,
        label: humanizeFavoriteKey(k, group.favoriteLabels),
        sourceKind: sourceKind
      });
    }
    return missing;
  }

  function removeMissingFavoritesFromGroup(group, discoveredWorkflows, scanStatus) {
    const missing = findMissingFavoritesForGroup(group, discoveredWorkflows, scanStatus);
    if (missing.length === 0) return group;
    const missingKeySet = new Set(missing.map((m) => m.key));
    const nextFavs = (group.favorites || []).filter((k) => !missingKeySet.has(String(k).trim().toLowerCase()));
    const nextLabels = { ...(group.favoriteLabels || {}) };
    for (const mKey of missingKeySet) {
      delete nextLabels[mKey];
    }
    return createWorkflowGroup(group.name, {
      ...group,
      favorites: nextFavs,
      favoriteLabels: nextLabels
    });
  }

  function createInitialMarkerStateFromGlobal() {
    return {
      colorized: true,
      showInfo: false
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
            showInfo: Boolean(parsed.showInfo)
          };
        }
      } catch (_) {}
    }
    const lower = raw.toLowerCase();
    const hasPlain = lower.includes("plain") || raw.includes("nocolor");
    const hasInfo = lower.includes("info") || lower.includes("desc");
    return {
      colorized: !hasPlain,
      showInfo: hasInfo
    };
  }

  function formatWorkflowMarkerState(state) {
    const clean = {
      colorized: state.colorized !== false,
      showInfo: Boolean(state.showInfo)
    };
    return MARKER_DEFAULT + JSON.stringify(clean);
  }

  function isItemFavoritedInGroup(group, item) {
    if (!group || !Array.isArray(group.favorites) || group.favorites.length === 0) {
      return false;
    }
    const key = getItemFavoriteKey(item);
    const nameOnly = ((item && item.name) || "").trim().toLowerCase();
    if (group.favorites.includes(key) || (nameOnly && group.favorites.includes(nameOnly))) {
      return true;
    }
    if (!nameOnly) return false;
    // Seamless transition when a yellow Node Pack (`pack:`) or purple Browse (`browse:`) workflow is copied/saved
    // into Your Library (`lib:`): it stays checked in the group and automatically switches to the white Library entry!
    if (key.startsWith("lib:")) {
      return group.favorites.includes("pack:" + nameOnly) || group.favorites.includes("browse:" + nameOnly);
    }
    // Conversely, if the user later deletes their Library copy while Node Pack / Browse templates are enabled,
    // fall back to showing the template as checked:
    if (key.startsWith("pack:") && group.includeNodePacks) {
      return group.favorites.includes("lib:" + nameOnly);
    }
    if (key.startsWith("browse:") && group.includeBrowse) {
      return group.favorites.includes("lib:" + nameOnly);
    }
    return false;
  }

  function promoteGroupFavoritesToLibrary(group, libraryNamesSet) {
    if (!group || !Array.isArray(group.favorites) || !(libraryNamesSet instanceof Set) || libraryNamesSet.size === 0) {
      return group;
    }
    let changed = false;
    const nextFavs = [];
    const nextLabels = { ...(group.favoriteLabels || {}) };
    for (const rawKey of group.favorites) {
      const k = String(rawKey || "").trim().toLowerCase();
      if (!k) continue;
      const colonIdx = k.indexOf(":");
      const prefix = colonIdx !== -1 ? k.slice(0, colonIdx) : "";
      const namePart = colonIdx !== -1 ? k.slice(colonIdx + 1).trim() : k;
      if ((prefix === "pack" || prefix === "browse") && namePart && libraryNamesSet.has(namePart)) {
        const libKey = "lib:" + namePart;
        if (!nextFavs.includes(libKey)) {
          nextFavs.push(libKey);
        }
        if (nextLabels[k] && !nextLabels[libKey]) {
          nextLabels[libKey] = nextLabels[k];
        }
        delete nextLabels[k];
        changed = true;
      } else if (!nextFavs.includes(k)) {
        nextFavs.push(k);
      }
    }
    if (!changed) return group;
    return createWorkflowGroup(group.name, {
      ...group,
      favorites: nextFavs,
      favoriteLabels: nextLabels
    });
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
      .inv7-wfs-group-mini-wrap {
        position: relative;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        height: 22px;
        padding: 0 6px;
        border-radius: 4px;
        border: 1px solid rgba(255, 255, 255, 0.10);
        background-color: rgba(255, 255, 255, 0.04);
        color: var(--chakra-colors-fg\\.muted, #a1a1aa);
        font-size: 10px;
        font-weight: 600;
        cursor: pointer;
        flex-shrink: 0;
        max-width: 145px;
        transition: color 0.12s ease, background-color 0.12s ease, border-color 0.12s ease;
      }
      .inv7-wfs-group-mini-wrap:hover {
        color: var(--chakra-colors-fg, #f4f4f5);
        background-color: rgba(90, 200, 250, 0.12);
        border-color: rgba(90, 200, 250, 0.35);
      }
      .inv7-wfs-group-mini-label {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 96px;
      }
      .inv7-wfs-group-mini-select {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        opacity: 0;
        cursor: pointer;
        font-size: 11px;
      }
      .inv7-wfs-group-mini-select option {
        background-color: #18181b;
        color: #f4f4f5;
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
      ::selection,
      input::selection,
      textarea::selection {
        background-color: var(--chakra-colors-accent-solid, #38bdf8) !important;
        color: var(--chakra-colors-accent-contrast, #16181d) !important;
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
      .inv7-wfs-source-tag[data-source-kind="shared"] {
        background-color: rgba(34, 197, 94, 0.18);
        color: #86efac;
      }
      .inv7-wfs-source-tag[data-source-kind="other"] {
        background-color: rgba(148, 163, 184, 0.20);
        color: #cbd5e1;
      }
      .inv7-wfs-source-tag[data-source-kind="missing"] {
        background-color: rgba(239, 68, 68, 0.22);
        color: #fca5a5;
        border: 1px solid rgba(239, 68, 68, 0.42);
      }
      .inv7-wfs-checkitem[data-missing="true"] {
        color: #fca5a5;
      }
      .inv7-wfs-checkitem[data-missing="true"] .inv7-wfs-checkitem-name {
        text-decoration: line-through;
        color: #f87171;
      }
      .inv7-wfs-checkitem[data-missing="true"] input {
        accent-color: #ef4444;
      }
      .inv7-wfs-missing-banner {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 5px 8px;
        border-radius: 4px;
        font-size: 11px;
        background-color: rgba(239, 68, 68, 0.14);
        border: 1px solid rgba(239, 68, 68, 0.38);
        color: #fecaca;
      }
      .inv7-wfs-group-box {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 7px 8px;
        border-radius: 5px;
        background-color: rgba(90, 200, 250, 0.07);
        border: 1px solid rgba(90, 200, 250, 0.24);
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

  const LIBRARY_SOURCE_LABELS = { own: "Your Library", shared: "Shared", other: "Other User" };

  // Single-user: everything belongs to "system" and is shared, so all of it is "own".
  // Multi-user: the user listing holds the caller's own workflows (an admin's holds everyone's), the
  // is_public listing what others shared. Own entries win name clashes, so favorites keyed by name
  // resolve to the user's own copy first.
  function mergeLibraryListings(userItems, sharedItems, me) {
    const rank = { own: 0, shared: 1, other: 2 };
    const byId = new Map();
    for (const wf of [...(userItems || []), ...(sharedItems || [])]) {
      if (!wf || !wf.workflow_id || !wf.name || byId.has(wf.workflow_id)) continue;
      const ownership = (wf.user_id || SINGLE_USER_ID) === me ? "own" : wf.is_public ? "shared" : "other";
      byId.set(wf.workflow_id, { wf, ownership });
    }
    const ordered = [...byId.values()].sort((a, b) => rank[a.ownership] - rank[b.ownership]);
    const seenNames = new Set();
    return ordered.filter(({ wf }) => {
      const key = wf.name.trim().toLowerCase();
      if (seenNames.has(key)) return false;
      seenNames.add(key);
      return true;
    });
  }

  function isWorkflowPageComplete(data, perPage) {
    if (!data || typeof data !== "object") return false;
    if (typeof data.pages === "number") return data.pages <= 1;
    const items = Array.isArray(data.items) ? data.items : [];
    return items.length < (perPage || 100);
  }

  async function fetchWorkflowListItemsWithStatus(query) {
    try {
      const resp = await apiFetch("/api/v1/workflows/?page=0&per_page=100&order_by=name&direction=ASC&" + query);
      if (!resp || !resp.ok) return { ok: false, complete: false, items: [] };
      const data = await resp.json();
      const items = Array.isArray(data.items) ? data.items : [];
      return { ok: true, complete: isWorkflowPageComplete(data, 100), items: items };
    } catch (_) {
      return { ok: false, complete: false, items: [] };
    }
  }

  let lastDiscoveredSignature = "";
  let lastScanPackNames = new Set();
  let lastScanBrowseNames = new Set();

  function computeDiscoveredSignature(items) {
    if (!Array.isArray(items)) return "";
    return items.map((w) => String(w.id || "") + ":" + String(w.name || "")).join("|");
  }

  function refreshAllWorkflows(options) {
    const forceRender = Boolean(options && options.forceRender);
    if (activeScanPromise) {
      if (forceRender) {
        return activeScanPromise.then(() => rehydrateAllSwitcherWidgets()).catch(() => {});
      }
      return activeScanPromise;
    }
    reloadConfigIfUserChanged();
    let changed = false;
    activeScanPromise = (async () => {
      try {
        const [userRes, sharedRes, browseResp, diskResp] = await Promise.all([
          fetchWorkflowListItemsWithStatus("categories=user"),
          fetchWorkflowListItemsWithStatus("categories=user&is_public=true"),
          apiFetch("/api/v1/workflows/?page=0&per_page=100&categories=default&order_by=name&direction=ASC").catch(() => null),
          apiFetch("/workflow-switcher/scan").catch(() => null)
        ]);

        const combined = [];
        const seenLibraryNames = new Set();
        const libOk = Boolean(userRes.ok && userRes.complete && sharedRes.ok && sharedRes.complete);
        let browseOk = false;
        let packOk = false;

        const libEntries = mergeLibraryListings(userRes.items, sharedRes.items, currentUserId());
        if (libEntries.length > 0) {
          const libItems = libEntries.map((entry) => entry.wf);
          const fullRecords = await Promise.all(
            libItems.map((wf) =>
              apiFetch("/api/v1/workflows/i/" + encodeURIComponent(wf.workflow_id))
                .then((r) => (r.ok ? r.json() : null))
                .catch(() => null)
            )
          );
          for (let i = 0; i < libItems.length; i++) {
            const wf = libItems[i];
            const ownership = libEntries[i].ownership;
            const record = fullRecords[i];
            const inner = record && record.workflow ? record.workflow : null;
            const headings = [];
            if (inner && inner.form && inner.form.elements && typeof inner.form.elements === "object") {
              for (const el of Object.values(inner.form.elements)) {
                if (el && (el.type === "heading" || el.type === "collapsible") && el.data && el.data.content) {
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
              isOwn: ownership === "own",
              ownership: ownership,
              source: LIBRARY_SOURCE_LABELS[ownership]
            });
          }
        }

        const seenPackNames = new Set();
        const seenBrowseNames = new Set();
        if (diskResp && diskResp.ok) {
          try {
            const diskData = await diskResp.json();
            const diskItems = Array.isArray(diskData.items) ? diskData.items : [];
            packOk = diskItems.length < 500;
            for (const item of diskItems) {
              const nmLower = (item.name || "").trim().toLowerCase();
              if (nmLower) seenPackNames.add(nmLower);
              // If the same workflow name already exists in Your Library, Your Library takes priority!
              if (seenLibraryNames.has(nmLower)) continue;
              combined.push({
                ...item,
                // Every disk-scanned JSON file (`file:...`) is a disk/pack template until saved into Your Library:
                isPack: true,
                tags: parseTagsList(item.tags)
              });
            }
          } catch (_) {}
        }

        if (browseResp && browseResp.ok) {
          try {
            const browseData = await browseResp.json();
            browseOk = isWorkflowPageComplete(browseData, 100);
            for (const wf of browseData.items || []) {
              if (!wf || !wf.workflow_id || !wf.name) continue;
              const nmLower = wf.name.trim().toLowerCase();
              if (seenPackNames.has(nmLower)) continue;
              if (nmLower) seenBrowseNames.add(nmLower);
              if (seenLibraryNames.has(nmLower)) continue;
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
          } catch (_) {}
        }

        lastScanPackNames = seenPackNames;
        lastScanBrowseNames = seenBrowseNames;

        const prevStatusSig = JSON.stringify(lastScanStatus);
        lastScanStatus = { lib: libOk, browse: browseOk, pack: packOk };
        if (libOk && seenLibraryNames.size > 0 && Array.isArray(switcherConfig.groups)) {
          let groupsPromoted = false;
          switcherConfig.groups = switcherConfig.groups.map((grp) => {
            const promoted = promoteGroupFavoritesToLibrary(grp, seenLibraryNames);
            if (promoted !== grp) groupsPromoted = true;
            return promoted;
          });
          if (groupsPromoted) {
            saveConfig(switcherConfig);
          }
        }
        const nextSig = computeDiscoveredSignature(combined);
        changed =
          !hasCompletedInitialScan ||
          nextSig !== lastDiscoveredSignature ||
          JSON.stringify(lastScanStatus) !== prevStatusSig;
        lastDiscoveredSignature = nextSig;
        allDiscoveredWorkflows = combined;
        hasCompletedInitialScan = true;
      } finally {
        lastScanFinishedAt = Date.now();
        activeScanPromise = null;
        if (changed || forceRender) {
          rehydrateAllSwitcherWidgets();
        }
      }
    })();
    return activeScanPromise;
  }

  function maybeAutoRefreshWorkflows() {
    if (activeScanPromise) return;
    if (Date.now() - lastScanFinishedAt < 2500) return;
    refreshAllWorkflows().catch(() => {});
  }

  function attachSelectInteractionGuard(selectEl) {
    if (!selectEl) return;
    const flushPending = () => {
      if (pendingRehydrateAfterSelect) {
        pendingRehydrateAfterSelect = false;
        window.setTimeout(() => rehydrateAllSwitcherWidgets(), 0);
      }
    };
    selectEl.addEventListener("focusout", flushPending);
    selectEl.addEventListener("change", flushPending);
  }

  function confirmAndCleanMissingForGroup(groupId) {
    if (reloadConfigIfUserChanged()) {
      rehydrateAllSwitcherWidgets();
      return;
    }
    const targetGrp = switcherConfig.groups.find((g) => g.id === groupId);
    if (!targetGrp) return;
    const missing = findMissingFavoritesForGroup(targetGrp, allDiscoveredWorkflows, lastScanStatus);
    if (missing.length === 0) return;
    const promptMsg =
      "Remove " +
      missing.length +
      (missing.length === 1 ? " missing workflow" : " missing workflows") +
      " from group \"" +
      targetGrp.name +
      "\"?\n\n" +
      missing.map((m) => "• " + m.label).join("\n");
    if (typeof window.confirm === "function" && !window.confirm(promptMsg)) {
      return;
    }
    const cleaned = removeMissingFavoritesFromGroup(targetGrp, allDiscoveredWorkflows, lastScanStatus);
    updateGroupById(targetGrp.id, {
      favorites: cleaned.favorites,
      favoriteLabels: cleaned.favoriteLabels
    });
    rehydrateAllSwitcherWidgets();
  }

  function findWorkflowUiAdapter(startEl) {
    const candidates = [
      startEl,
      document.querySelector(".inv7-wfs-edit-box"),
      document.querySelector(".inv7-wfs-view-box"),
      document.querySelector("input[type='file'][accept*='.json']"),
      ...document.querySelectorAll("[role='region']")
    ];
    for (const el of candidates) {
      let domNode = el;
      while (domNode && domNode !== document.body) {
        const fiberKey = Object.keys(domNode).find((k) => k.startsWith("__reactFiber$"));
        if (fiberKey) {
          let cur = domNode[fiberKey];
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
          break;
        }
        domNode = domNode.parentElement;
      }
    }
    return null;
  }

  let hydratedWorkflowIds = new Set();

  function captureActiveWorkflowSectionStates() {
    reloadConfigIfUserChanged();
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
      if (el && el.type === "collapsible" && typeof currentMap[elId] === "boolean") {
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
      if (!el || el.type !== "collapsible") continue;
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
        if (el && el.type === "collapsible" && el.data && typeof sectionStatesMap[elId] === "boolean") {
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

  async function copyCheckedPackWorkflowsOfActiveGroupToYours(skipNameLower) {
    const activeGroup = getActiveGroup();
    if (!activeGroup || !Array.isArray(activeGroup.favorites) || activeGroup.favorites.length === 0) {
      return 0;
    }
    const existingOwnLibNames = new Set(
      allDiscoveredWorkflows
        .filter((w) => w.isOwn && w.workflow_id && w.name)
        .map((w) => w.name.trim().toLowerCase())
    );
    if (skipNameLower) {
      existingOwnLibNames.add(skipNameLower);
    }
    const packCandidates = allDiscoveredWorkflows.filter((w) => {
      if (!w || !w.id) return false;
      const isDiskPack = w.id.startsWith("file:");
      const isBrowseTemplate = Boolean(w.isDefaultLib && w.workflow_id);
      if (!isDiskPack && !isBrowseTemplate) return false;
      const nm = (w.name || "").trim().toLowerCase();
      if (!nm || existingOwnLibNames.has(nm)) return false;
      return isItemFavoritedInGroup(activeGroup, w);
    });
    let copiedCount = 0;
    for (const packItem of packCandidates) {
      try {
        let rawData = null;
        if (packItem.id.startsWith("file:")) {
          const fileId = packItem.file_id || packItem.id.slice(5);
          const fileResp = await apiFetch(
            "/workflow-switcher/file?id=" + encodeURIComponent(fileId),
            { cache: "no-store" }
          );
          if (!fileResp || !fileResp.ok) continue;
          rawData = await fileResp.json();
        } else if (packItem.isDefaultLib && packItem.workflow_id) {
          const dtoResp = await apiFetch(
            "/api/v1/workflows/i/" + encodeURIComponent(packItem.workflow_id),
            { cache: "no-store" }
          );
          if (!dtoResp || !dtoResp.ok) continue;
          rawData = await dtoResp.json();
        }
        if (!rawData) continue;
        const wfPayload =
          rawData && typeof rawData.workflow === "object" && rawData.workflow !== null
            ? { ...rawData.workflow }
            : { ...rawData };
        delete wfPayload.id;
        wfPayload.meta = {
          ...(wfPayload.meta && typeof wfPayload.meta === "object" ? wfPayload.meta : {}),
          category: "user",
          version: "3.0.0"
        };
        const createResp = await apiFetch("/api/v1/workflows/", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workflow: wfPayload })
        });
        if (createResp && createResp.ok) {
          copiedCount += 1;
          existingOwnLibNames.add((packItem.name || "").trim().toLowerCase());
        }
      } catch (_) {}
    }
    return copiedCount;
  }

  async function removeCheckedWorkflowsOfActiveGroupFromYours(statusBtn) {
    if (statusBtn && statusBtn.disabled) return;
    const defaultLabel = "🗑 Remove Checked from Yours";
    const setStatus = (txt) => {
      if (statusBtn) statusBtn.textContent = txt;
    };
    const restoreStatus = () => {
      if (statusBtn) statusBtn.textContent = defaultLabel;
    };
    const activeGroup = getActiveGroup();
    if (!activeGroup || !Array.isArray(activeGroup.favorites) || activeGroup.favorites.length === 0) {
      setStatus("No workflows checked");
      window.setTimeout(restoreStatus, 1600);
      return;
    }
    const ownTargets = allDiscoveredWorkflows.filter(
      (w) => w && w.isOwn && w.workflow_id && isItemFavoritedInGroup(activeGroup, w)
    );
    if (ownTargets.length === 0) {
      setStatus("None in Yours");
      window.setTimeout(restoreStatus, 1600);
      return;
    }
    const replaceable = [];
    const ownOnly = [];
    for (const w of ownTargets) {
      const nmLower = (w.name || "").trim().toLowerCase();
      if (lastScanPackNames.has(nmLower) || lastScanBrowseNames.has(nmLower)) {
        replaceable.push(w);
      } else {
        ownOnly.push(w);
      }
    }
    if (replaceable.length === 0) {
      setStatus("Custom workflows kept (no template)");
      window.setTimeout(restoreStatus, 2000);
      return;
    }
    const keptNote =
      ownOnly.length > 0
        ? "\n\n(Note: " +
          ownOnly.length +
          " custom workflow(s) without a Node Pack / Built-in template will be KEPT safe in Yours: " +
          ownOnly.map((w) => w.name).join(", ") +
          ".)"
        : "";
    const promptMsg =
      "Remove " +
      replaceable.length +
      (replaceable.length === 1 ? " checked workflow" : " checked workflows") +
      " of group \"" +
      activeGroup.name +
      "\" from Your Library (Yours)?\n\n" +
      replaceable.map((w) => "• " + w.name).join("\n") +
      "\n\n(Their Node Pack / Built-in templates on disk are NOT deleted — they will immediately reappear in the list so you can load or re-save the latest versions.)" +
      keptNote;
    if (typeof window.confirm === "function" && !window.confirm(promptMsg)) {
      return;
    }

    if (statusBtn) statusBtn.disabled = true;
    setStatus("Removing...");
    try {
      let removedCount = 0;
      const removedByName = new Map();
      let activatedPack = false;
      let activatedBrowse = false;

      for (const target of replaceable) {
        try {
          const delResp = await apiFetch("/api/v1/workflows/i/" + encodeURIComponent(target.workflow_id), {
            method: "DELETE"
          });
          if (delResp && (delResp.ok || delResp.status === 404)) {
            removedCount += 1;
            const nmLower = (target.name || "").trim().toLowerCase();
            if (nmLower) {
              const isPack = lastScanPackNames.has(nmLower);
              const newPrefix = isPack ? "pack:" : "browse:";
              if (isPack) activatedPack = true;
              else activatedBrowse = true;
              removedByName.set(nmLower, { newKey: newPrefix + nmLower, label: target.name });
            }
          }
        } catch (_) {}
      }

      if (removedCount > 0) {
        const nextLabels = { ...(activeGroup.favoriteLabels || {}) };
        const nextFavs = (activeGroup.favorites || []).map((k) => {
          const keyLower = String(k || "").trim().toLowerCase();
          if (keyLower.startsWith("lib:")) {
            const nm = keyLower.slice(4).trim();
            const rep = removedByName.get(nm);
            if (rep) {
              const existingLabel = nextLabels[keyLower] || rep.label;
              delete nextLabels[keyLower];
              if (existingLabel) nextLabels[rep.newKey] = existingLabel;
              return rep.newKey;
            }
          }
          return keyLower;
        });
        const patch = {
          favorites: nextFavs,
          favoriteLabels: nextLabels
        };
        if (activatedPack) patch.includeNodePacks = true;
        if (activatedBrowse) patch.includeBrowse = true;
        updateGroupById(activeGroup.id, patch);
        window.dispatchEvent(new CustomEvent("invoke7-refresh-workflow-library"));
        setStatus("✓ Removed " + removedCount + " from Yours!");
        window.setTimeout(restoreStatus, 1800);
        await refreshAllWorkflows({ forceRender: true });
      } else {
        setStatus("⚠ Remove failed");
        window.setTimeout(restoreStatus, 1800);
      }
    } finally {
      if (statusBtn) statusBtn.disabled = false;
    }
  }

  async function saveOrUpdateCurrentWorkflowInYours(hostDiv, statusBtn) {
    const isIconBtn = Boolean(statusBtn && statusBtn.getAttribute("data-icon-btn") === "true");
    const defaultBtnLabel = "💾 Copy / Save to Yours";
    const setBtnStatus = (text, stateKind) => {
      if (!statusBtn) return;
      if (isIconBtn) {
        if (stateKind === "ok") {
          statusBtn.style.color = "#4ade80";
          statusBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>`;
        } else if (stateKind === "err") {
          statusBtn.style.color = "#f87171";
        }
        statusBtn.title = text;
      } else {
        statusBtn.textContent = text;
      }
    };
    const restoreBtnStatus = () => {
      if (!statusBtn) return;
      if (isIconBtn) {
        statusBtn.style.color = "";
        statusBtn.title = "Copy / Save active workflow to Your Library (Yours)";
        statusBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>`;
      } else {
        statusBtn.textContent = defaultBtnLabel;
      }
    };

    const adapter = findWorkflowUiAdapter(hostDiv);
    const snap = adapter && adapter.project ? adapter.project.getSnapshot() : null;
    const doc =
      adapter && typeof adapter.getProjectGraph === "function"
        ? adapter.getProjectGraph()
        : snap && snap.projectGraph
          ? snap.projectGraph
          : null;
    if (!doc) {
      try {
        setBtnStatus("Saving...", "busy");
        const onlyCopied = await copyCheckedPackWorkflowsOfActiveGroupToYours("");
        if (onlyCopied > 0) {
          window.dispatchEvent(new CustomEvent("invoke7-refresh-workflow-library"));
          setBtnStatus("✓ Copied " + onlyCopied + " workflows to Yours!", "ok");
          window.setTimeout(restoreBtnStatus, 1800);
          await refreshAllWorkflows({ forceRender: true });
          return;
        }
      } catch (_) {}
      setBtnStatus("⚠ Open via menu", "err");
      window.setTimeout(restoreBtnStatus, 1800);
      return;
    }

    const currentSource = snap.activeWorkflow && snap.activeWorkflow.source ? snap.activeWorkflow.source : null;
    const currentNameLower = (doc.name || "").trim().toLowerCase();
    // Only the user's own records are candidates: a shared or (for admins) another user's workflow with the
    // same name must never be overwritten in place - saving it creates the user's own copy instead.
    const matchingLibItem = allDiscoveredWorkflows.find(
      (w) =>
        w.isOwn &&
        w.workflow_id &&
        ((currentSource && currentSource.libraryWorkflowId === w.workflow_id) ||
          (currentNameLower && (w.name || "").trim().toLowerCase() === currentNameLower))
    );

    // The workflow the project copy came from is only updated in place when the user owns it.
    const sourceId = currentSource && currentSource.libraryWorkflowId ? String(currentSource.libraryWorkflowId) : "";
    const sourceIsOwn =
      sourceId !== "" &&
      !sourceId.startsWith("default_") &&
      (currentSource.ownerUserId || SINGLE_USER_ID) === currentUserId();
    const targetWorkflowId = (sourceIsOwn ? sourceId : null) || (matchingLibItem ? matchingLibItem.workflow_id : null);

    try {
      setBtnStatus("Saving...", "busy");
      const captured = captureActiveWorkflowSectionStates();
      const sectionStatesMap = captured ? captured.currentMap : null;
      if (targetWorkflowId) {
        const payload = serializeProjectGraphToWorkflowJson(doc, targetWorkflowId, sectionStatesMap);
        const resp = await apiFetch("/api/v1/workflows/i/" + encodeURIComponent(targetWorkflowId), {
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
          const extraCopied = await copyCheckedPackWorkflowsOfActiveGroupToYours(currentNameLower);
          window.dispatchEvent(new CustomEvent("invoke7-refresh-workflow-library"));
          setBtnStatus(
            extraCopied > 0 ? "✓ Updated + " + extraCopied + " copied to Yours!" : "✓ Updated in Yours!",
            "ok"
          );
          window.setTimeout(restoreBtnStatus, 1800);
          await refreshAllWorkflows({ forceRender: true });
          return;
        }
      }

      // Create new in Your Library if not found or if PATCH returned 404:
      const newPayload = serializeProjectGraphToWorkflowJson(doc, undefined, sectionStatesMap);
      delete newPayload.id;
      const createResp = await apiFetch("/api/v1/workflows/", {
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
        const extraCopied = await copyCheckedPackWorkflowsOfActiveGroupToYours(currentNameLower);
        window.dispatchEvent(new CustomEvent("invoke7-refresh-workflow-library"));
        setBtnStatus(
          extraCopied > 0 ? "✓ Copied " + (extraCopied + 1) + " workflows to Yours!" : "✓ Copied to Yours!",
          "ok"
        );
        window.setTimeout(restoreBtnStatus, 1800);
        await refreshAllWorkflows({ forceRender: true });
      } else {
        setBtnStatus("⚠ Save failed", "err");
      }
    } catch (_) {
      setBtnStatus("⚠ Save error", "err");
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
      const fileId = item.file_id || item.id.slice(5);
      const resp = await apiFetch(
        "/workflow-switcher/file?id=" + encodeURIComponent(fileId),
        { cache: "no-store" }
      );
      if (!resp.ok) return;
      jsonText = await resp.text();
      filename = item.filename || filename;
    } else if (item.id.startsWith("lib:")) {
      const resp = await apiFetch("/api/v1/workflows/i/" + encodeURIComponent(item.workflow_id), { cache: "no-store" });
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

  function getPoolForGroup(group) {
    const activeGrp = group || getActiveGroup();
    const includePacks = Boolean(activeGrp && activeGrp.includeNodePacks);
    const includeBrowse = Boolean(activeGrp && activeGrp.includeBrowse);
    return allDiscoveredWorkflows.filter((w) => {
      if (isItemFavoritedInGroup(activeGrp, w)) return true;
      if (w.isPack) return includePacks;
      if (w.isDefaultLib) return includeBrowse;
      return true;
    });
  }

  function buildDropdownSelectElement() {
    const activeGroup = getActiveGroup();
    const allGroups = Array.isArray(switcherConfig.groups) ? switcherConfig.groups : [activeGroup];
    const pool = getPoolForGroup(activeGroup);
    const enabledItems = pool.filter((w) => isItemFavoritedInGroup(activeGroup, w));
    const missingItems = hasCompletedInitialScan
      ? findMissingFavoritesForGroup(activeGroup, allDiscoveredWorkflows, lastScanStatus)
      : [];
    const activeName = detectActiveCenterWorkflowName(pool);
    const matchedItem = activeName
      ? pool.find((w) => w.name && w.name.toLowerCase() === activeName.toLowerCase()) || null
      : null;

    const select = document.createElement("select");
    select.className = "inv7-wfs-select";
    attachSelectInteractionGuard(select);

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.selected = !matchedItem;
    placeholder.disabled = true;
    placeholder.hidden = true;
    placeholder.textContent =
      enabledItems.length === 0 && missingItems.length === 0
        ? "— No workflows in group \"" + activeGroup.name + "\" (check workflows in Edit tab) —"
        : activeName
          ? activeName
          : "— Select workflow in \"" + activeGroup.name + "\" (" + enabledItems.length + ") —";
    select.appendChild(placeholder);

    let prevFamily = null;
    for (const item of enabledItems) {
      const family = getWorkflowFamilyKey(item);
      if (prevFamily !== null && family !== prevFamily) {
        const sep = document.createElement("hr");
        select.appendChild(sep);
      }
      prevFamily = family;

      const isPack = Boolean(item.isPack || (item.id && item.id.startsWith("file:")));
      const isBrowse = Boolean(item.isDefaultLib);
      const isCurrentActive = Boolean(activeName && item.name && item.name.toLowerCase() === activeName.toLowerCase());
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.selected = isCurrentActive;
      opt.textContent = item.name;
      if (isPack) {
        opt.setAttribute("data-source-kind", "pack");
      } else if (isBrowse) {
        opt.setAttribute("data-source-kind", "browse");
      }
      select.appendChild(opt);
    }

    if (missingItems.length > 0) {
      const missingGroup = document.createElement("optgroup");
      missingGroup.label = "⚠ Deleted / Missing in this group";
      for (const m of missingItems) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.disabled = true;
        opt.textContent = "✖ " + m.label + " (deleted — click Clean missing)";
        missingGroup.appendChild(opt);
      }
      select.appendChild(missingGroup);
    }

    select.addEventListener("change", (e) => {
      if (reloadConfigIfUserChanged()) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      const chosenId = e.target.value;
      const chosen = allDiscoveredWorkflows.find((w) => w.id === chosenId);
      if (chosen) {
        activateWorkflowItem(chosen);
      }
    });

    return {
      select,
      matchedItem,
      activeName,
      activeGroup,
      missingItems,
      enabledCount: enabledItems.length
    };
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

    let activeGroup = getActiveGroup();
    const boundGroupId = activeGroup.id;
    const commitGroupPatch = (patch, shouldRebuildCard) => {
      const updated = updateGroupById(boundGroupId, patch);
      if (!updated) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      activeGroup = updated;
      if (shouldRebuildCard) {
        renderEditCardControls(hostDiv, ta);
      } else {
        const selectedOpt = Array.from(groupSelect.options).find((o) => o.value === boundGroupId);
        if (selectedOpt) {
          const favCount = Array.isArray(activeGroup.favorites) ? activeGroup.favorites.length : 0;
          selectedOpt.textContent = activeGroup.name + " (" + favCount + " workflows)";
        }
        updatePreviewSelectInPlace();
      }
      for (const viewBox of document.querySelectorAll(".inv7-wfs-view-box")) {
        renderViewWidget(viewBox);
      }
    };

    hostDiv.innerHTML = "";

    // 1. Central Workflow Group Bar (create / switch / rename / delete named groups)
    const groupBox = document.createElement("div");
    groupBox.className = "inv7-wfs-group-box";

    const groupHeaderRow = document.createElement("div");
    groupHeaderRow.className = "inv7-wfs-toprow";
    groupHeaderRow.style.flexWrap = "wrap";

    const groupLbl = document.createElement("span");
    groupLbl.className = "inv7-wfs-label";
    groupLbl.style.color = "rgb(147, 226, 255)";
    groupLbl.textContent = "📁 Active Workflow Group (shared across all workflows):";

    const groupSelect = document.createElement("select");
    groupSelect.className = "inv7-wfs-select";
    groupSelect.style.height = "28px";
    groupSelect.style.fontSize = "11px";
    attachSelectInteractionGuard(groupSelect);
    for (const grp of switcherConfig.groups) {
      const opt = document.createElement("option");
      opt.value = grp.id;
      opt.selected = grp.id === activeGroup.id;
      const favCount = Array.isArray(grp.favorites) ? grp.favorites.length : 0;
      opt.textContent = grp.name + " (" + favCount + " workflows)";
      groupSelect.appendChild(opt);
    }
    groupSelect.addEventListener("change", () => {
      if (reloadConfigIfUserChanged()) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      if (switcherConfig.groups.some((g) => g.id === groupSelect.value)) {
        switcherConfig.activeGroupId = groupSelect.value;
        saveConfig(switcherConfig);
        rehydrateAllSwitcherWidgets();
      }
    });

    const groupActionRow = document.createElement("div");
    groupActionRow.className = "inv7-wfs-folder-row";
    groupActionRow.style.flexWrap = "wrap";

    const groupNameInput = document.createElement("input");
    groupNameInput.type = "text";
    groupNameInput.className = "inv7-wfs-folder-input";
    groupNameInput.placeholder = "Rename this group (e.g. LTX Workflows, Comic Workflows)...";
    groupNameInput.value = activeGroup.name;

    const applyGroupRename = (showFeedback) => {
      if (reloadConfigIfUserChanged()) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      const nextName = (groupNameInput.value || "").trim();
      if (!nextName) return;
      const duplicate = switcherConfig.groups.some(
        (g) => g.id !== boundGroupId && g.name.toLowerCase() === nextName.toLowerCase()
      );
      if (duplicate) return;
      const updated = updateGroupById(boundGroupId, { name: nextName });
      if (!updated) return;
      activeGroup = updated;
      const selectedOpt = Array.from(groupSelect.options).find((o) => o.value === boundGroupId);
      if (selectedOpt) {
        const favCount = Array.isArray(activeGroup.favorites) ? activeGroup.favorites.length : 0;
        selectedOpt.textContent = activeGroup.name + " (" + favCount + " workflows)";
      }
      if (bulkLbl) {
        bulkLbl.textContent = "Workflows in group \"" + activeGroup.name + "\":";
      }
      updatePreviewSelectInPlace();
      for (const viewBox of document.querySelectorAll(".inv7-wfs-view-box")) {
        renderViewWidget(viewBox);
      }
      if (showFeedback && renameGroupBtn) {
        renameGroupBtn.textContent = "✓ Renamed!";
        window.setTimeout(() => {
          renameGroupBtn.textContent = "✎ Rename";
        }, 1200);
      }
    };

    // Live-rename as the user types so renaming "Default Group" (or any group) works effortlessly!
    groupNameInput.addEventListener("input", () => {
      applyGroupRename(false);
    });
    groupNameInput.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") {
        e.preventDefault();
        applyGroupRename(true);
      }
    });

    const newGroupBtn = document.createElement("button");
    newGroupBtn.type = "button";
    newGroupBtn.className = "inv7-wfs-btn";
    newGroupBtn.title = "Create a new empty workflow group, switch to it, and focus the name field";
    newGroupBtn.textContent = "+ New Group";
    newGroupBtn.addEventListener("click", () => {
      if (reloadConfigIfUserChanged()) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      let idx = switcherConfig.groups.length + 1;
      let nextName = "Group " + idx;
      while (switcherConfig.groups.some((g) => g.name.toLowerCase() === nextName.toLowerCase())) {
        idx += 1;
        nextName = "Group " + idx;
      }
      const created = createWorkflowGroup(nextName, {
        includeNodePacks: activeGroup.includeNodePacks,
        includeBrowse: activeGroup.includeBrowse
      });
      switcherConfig.groups.push(created);
      switcherConfig.activeGroupId = created.id;
      saveConfig(switcherConfig);
      rehydrateAllSwitcherWidgets();
      window.setTimeout(() => {
        const nameInp = hostDiv.querySelector(".inv7-wfs-group-box .inv7-wfs-folder-input");
        if (nameInp) {
          nameInp.focus();
          nameInp.select();
        }
      }, 20);
    });

    const renameGroupBtn = document.createElement("button");
    renameGroupBtn.type = "button";
    renameGroupBtn.className = "inv7-wfs-btn";
    renameGroupBtn.title = "Confirm group name";
    renameGroupBtn.textContent = "✎ Rename";
    renameGroupBtn.addEventListener("click", () => {
      applyGroupRename(true);
    });

    const deleteGroupBtn = document.createElement("button");
    deleteGroupBtn.type = "button";
    deleteGroupBtn.className = "inv7-wfs-btn";
    deleteGroupBtn.disabled = switcherConfig.groups.length <= 1;
    deleteGroupBtn.style.opacity = switcherConfig.groups.length <= 1 ? "0.45" : "1";
    deleteGroupBtn.title =
      switcherConfig.groups.length <= 1
        ? "At least one workflow group is kept"
        : "Delete the active workflow group";
    deleteGroupBtn.textContent = "🗑 Delete Group";
    deleteGroupBtn.addEventListener("click", () => {
      if (reloadConfigIfUserChanged()) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      if (switcherConfig.groups.length <= 1) return;
      const target = switcherConfig.groups.find((g) => g.id === boundGroupId);
      if (!target) return;
      if (
        Array.isArray(target.favorites) &&
        target.favorites.length > 0 &&
        typeof window.confirm === "function" &&
        !window.confirm("Delete workflow group \"" + target.name + "\" (" + target.favorites.length + " workflows)?")
      ) {
        return;
      }
      switcherConfig.groups = switcherConfig.groups.filter((g) => g.id !== boundGroupId);
      switcherConfig.activeGroupId = switcherConfig.groups[0].id;
      saveConfig(switcherConfig);
      rehydrateAllSwitcherWidgets();
    });

    groupHeaderRow.appendChild(groupLbl);
    groupBox.appendChild(groupHeaderRow);
    groupBox.appendChild(groupSelect);
    groupActionRow.appendChild(groupNameInput);
    groupActionRow.appendChild(newGroupBtn);
    groupActionRow.appendChild(renameGroupBtn);
    groupActionRow.appendChild(deleteGroupBtn);
    groupBox.appendChild(groupActionRow);
    hostDiv.appendChild(groupBox);

    // 2. Preview Dropdown
    const previewWrap = document.createElement("div");
    previewWrap.style.display = "flex";
    previewWrap.style.flexDirection = "column";
    previewWrap.style.gap = "6px";

    function updatePreviewSelectInPlace() {
      previewWrap.innerHTML = "";
      const { select, enabledCount } = buildDropdownSelectElement();
      const previewLbl = document.createElement("div");
      previewLbl.className = "inv7-wfs-label";
      previewLbl.textContent =
        "Preview Dropdown (" + enabledCount + " workflows active in group \"" + activeGroup.name + "\"):";
      previewWrap.appendChild(previewLbl);
      previewWrap.appendChild(select);
    }

    updatePreviewSelectInPlace();
    hostDiv.appendChild(previewWrap);

    // 3. Missing / Deleted workflows warning banner (if any workflow in this group was deleted)
    const missingItems = hasCompletedInitialScan
      ? findMissingFavoritesForGroup(activeGroup, allDiscoveredWorkflows, lastScanStatus)
      : [];
    if (missingItems.length > 0) {
      const warnBar = document.createElement("div");
      warnBar.className = "inv7-wfs-missing-banner";
      const warnTxt = document.createElement("span");
      warnTxt.textContent =
        "⚠ " +
        missingItems.length +
        (missingItems.length === 1
          ? " workflow in this group was deleted or renamed."
          : " workflows in this group were deleted or renamed.");
      const cleanBtn = document.createElement("button");
      cleanBtn.type = "button";
      cleanBtn.className = "inv7-wfs-btn";
      cleanBtn.style.borderColor = "rgba(248, 113, 113, 0.55)";
      cleanBtn.style.color = "#fecaca";
      cleanBtn.textContent = "🧹 Clean missing";
      cleanBtn.addEventListener("click", () => {
        confirmAndCleanMissingForGroup(boundGroupId);
      });
      warnBar.appendChild(warnTxt);
      warnBar.appendChild(cleanBtn);
      hostDiv.appendChild(warnBar);
    }

    // 4. Source toggle row: Your Library (cyan) + Built-in Browse (purple) + Node Pack Templates (amber/gold)
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
    browseCb.checked = Boolean(activeGroup.includeBrowse);
    browseCb.addEventListener("change", () => {
      commitGroupPatch({ includeBrowse: Boolean(browseCb.checked) }, true);
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
    packCb.checked = Boolean(activeGroup.includeNodePacks);
    packCb.addEventListener("change", () => {
      commitGroupPatch({ includeNodePacks: Boolean(packCb.checked) }, true);
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

    // 5. Family Filter Chips (multi-select, persisted per group in activeGroup.activeTags)
    const pool = getPoolForGroup(activeGroup);
    const familyCounts = computeFamilyCounts(pool);
    const validFamilyKeys = new Set(familyCounts.map((entry) => entry.key));
    const activeTags = (Array.isArray(activeGroup.activeTags) ? activeGroup.activeTags : []).filter((k) =>
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
          commitGroupPatch({ activeTags: nextTags }, true);
        });
        return btn;
      };

      chipStrip.appendChild(makeChip("All", null, pool.length));
      for (const entry of familyCounts) {
        chipStrip.appendChild(makeChip(entry.tag, entry.key, entry.count));
      }
      hostDiv.appendChild(chipStrip);
    }

    // 6. Search filter input
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
    bulkLbl.textContent = "Workflows in group \"" + activeGroup.name + "\":";
    const btnGroup = document.createElement("div");
    btnGroup.style.display = "flex";
    btnGroup.style.flexWrap = "wrap";
    btnGroup.style.gap = "4px";

    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "inv7-wfs-btn";
    allBtn.textContent = "All visible";
    allBtn.addEventListener("click", () => {
      const nextFavs = new Set(activeGroup.favorites);
      const nextLabels = { ...(activeGroup.favoriteLabels || {}) };
      for (const w of getVisibleWorkflows()) {
        const k = getItemFavoriteKey(w);
        nextFavs.add(k);
        if (w.name) nextLabels[k] = w.name;
      }
      commitGroupPatch({ favorites: Array.from(nextFavs), favoriteLabels: nextLabels }, true);
    });

    const noneBtn = document.createElement("button");
    noneBtn.type = "button";
    noneBtn.className = "inv7-wfs-btn";
    noneBtn.textContent = "None visible";
    noneBtn.addEventListener("click", () => {
      const removeKeys = new Set();
      const nextLabels = { ...(activeGroup.favoriteLabels || {}) };
      for (const w of getVisibleWorkflows()) {
        const k = getItemFavoriteKey(w);
        removeKeys.add(k);
        removeKeys.add(((w && w.name) || "").trim().toLowerCase());
        delete nextLabels[k];
      }
      const nextFavs = (activeGroup.favorites || []).filter((k) => !removeKeys.has(k));
      commitGroupPatch({ favorites: nextFavs, favoriteLabels: nextLabels }, true);
    });

    const updateYoursBtn = document.createElement("button");
    updateYoursBtn.type = "button";
    updateYoursBtn.className = "inv7-wfs-btn";
    updateYoursBtn.style.borderColor = "rgba(90, 200, 250, 0.45)";
    updateYoursBtn.style.color = "rgb(147, 226, 255)";
    updateYoursBtn.title = "Copy checked Node Pack / Browse workflows into 'Your Library' or update your existing copy in place";
    updateYoursBtn.textContent = "💾 Copy / Save to Yours";
    updateYoursBtn.addEventListener("click", () => {
      saveOrUpdateCurrentWorkflowInYours(hostDiv, updateYoursBtn);
    });

    const removeYoursBtn = document.createElement("button");
    removeYoursBtn.type = "button";
    removeYoursBtn.className = "inv7-wfs-btn";
    removeYoursBtn.style.borderColor = "rgba(248, 113, 113, 0.45)";
    removeYoursBtn.style.color = "rgb(252, 165, 165)";
    removeYoursBtn.title =
      "Remove checked workflows of this group from 'Your Library' (Yours) so their fresh Node Pack / Built-in templates reappear";
    removeYoursBtn.textContent = "🗑 Remove Checked from Yours";
    removeYoursBtn.addEventListener("click", () => {
      removeCheckedWorkflowsOfActiveGroupFromYours(removeYoursBtn);
    });

    const rescanBtn = document.createElement("button");
    rescanBtn.type = "button";
    rescanBtn.className = "inv7-wfs-btn";
    rescanBtn.textContent = "↻ Refresh";
    rescanBtn.addEventListener("click", () => refreshAllWorkflows({ forceRender: true }));

    btnGroup.appendChild(allBtn);
    btnGroup.appendChild(noneBtn);
    btnGroup.appendChild(updateYoursBtn);
    btnGroup.appendChild(removeYoursBtn);
    btnGroup.appendChild(rescanBtn);
    bulkRow.appendChild(bulkLbl);
    bulkRow.appendChild(btnGroup);
    hostDiv.appendChild(bulkRow);

    const listEl = document.createElement("div");
    listEl.className = "inv7-wfs-checklist";

    function renderChecklistItems() {
      listEl.innerHTML = "";
      // Show deleted/missing workflows at the top of the checklist struck through so the user can uncheck them:
      for (const m of missingItems) {
        const mRow = document.createElement("label");
        mRow.className = "inv7-wfs-checkitem";
        mRow.setAttribute("data-missing", "true");
        const mCb = document.createElement("input");
        mCb.type = "checkbox";
        mCb.checked = true;
        mCb.addEventListener("change", () => {
          const nextFavs = (activeGroup.favorites || []).filter(
            (k) => String(k).trim().toLowerCase() !== m.key
          );
          const nextLabels = { ...(activeGroup.favoriteLabels || {}) };
          delete nextLabels[m.key];
          commitGroupPatch({ favorites: nextFavs, favoriteLabels: nextLabels }, true);
        });
        const mName = document.createElement("span");
        mName.className = "inv7-wfs-checkitem-name";
        mName.textContent = m.label;
        mName.title = m.label + " (deleted or renamed — uncheck to remove from group)";
        const mTag = document.createElement("span");
        mTag.className = "inv7-wfs-source-tag";
        mTag.setAttribute("data-source-kind", "missing");
        mTag.textContent = "Deleted / Missing";
        mRow.appendChild(mCb);
        mRow.appendChild(mName);
        mRow.appendChild(mTag);
        listEl.appendChild(mRow);
      }

      const visible = getVisibleWorkflows();
      if (visible.length === 0 && missingItems.length === 0) {
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
        cb.checked = isItemFavoritedInGroup(activeGroup, item);
        cb.addEventListener("change", () => {
          const favKey = getItemFavoriteKey(item);
          const nameKey = ((item && item.name) || "").trim().toLowerCase();
          const currentFavs = (activeGroup.favorites || []).filter((k) => k !== favKey && k !== nameKey);
          const nextFavs = cb.checked ? [...currentFavs, favKey] : currentFavs;
          const nextLabels = { ...(activeGroup.favoriteLabels || {}) };
          if (cb.checked && item.name) {
            nextLabels[favKey] = item.name;
          } else {
            delete nextLabels[favKey];
          }
          commitGroupPatch({ favorites: nextFavs, favoriteLabels: nextLabels }, false);
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
        } else if (item.ownership && item.ownership !== "own") {
          srcSpan.setAttribute("data-source-kind", item.ownership);
        }
        srcSpan.textContent = isPack ? "Node Pack" : isBrowse ? "Built-in (Browse)" : item.source || LIBRARY_SOURCE_LABELS.own;
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
        hostDiv.setAttribute("data-bound-marker", val);
        // Prevent InvokeAI's Form Builder drag-and-drop (@dnd-kit) from stealing pointer/keyboard events inside inputs:
        hostDiv.addEventListener("pointerdown", (e) => e.stopPropagation());
        hostDiv.addEventListener("mousedown", (e) => e.stopPropagation());
        hostDiv.addEventListener("keydown", (e) => e.stopPropagation());
        card.appendChild(hostDiv);
        renderEditCardControls(hostDiv, ta);
        maybeAutoRefreshWorkflows();
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
    if (!hostDiv.hasAttribute("data-inv7-hover-bound")) {
      hostDiv.setAttribute("data-inv7-hover-bound", "true");
      hostDiv.addEventListener("pointerenter", () => {
        maybeAutoRefreshWorkflows();
      });
    }
    const prevP = hostDiv.previousElementSibling;
    const markerText = prevP && prevP.tagName === "P" ? prevP.textContent || "" : "";
    const wfState = parseWorkflowMarkerState(markerText);
    hostDiv.setAttribute("data-colorized", wfState.colorized ? "true" : "false");
    hostDiv.setAttribute("data-showinfo", wfState.showInfo ? "true" : "false");
    const alwaysShowInfo = wfState.showInfo;
    const { select, matchedItem, activeName, activeGroup, missingItems } = buildDropdownSelectElement();
    const boundViewGroupId = activeGroup.id;
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
      if (reloadConfigIfUserChanged()) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      switcherConfig.collapsedInView = !switcherConfig.collapsedInView;
      saveConfig(switcherConfig);
      renderViewWidget(hostDiv);
    });

    headerRow.appendChild(trigger);

    // Subtle mini group-switcher button + native mini dropdown in the right header bar:
    const allGroups = Array.isArray(switcherConfig.groups) ? switcherConfig.groups : [activeGroup];
    const miniWrap = document.createElement("div");
    miniWrap.className = "inv7-wfs-group-mini-wrap";
    miniWrap.title =
      allGroups.length > 1
        ? "Switch workflow group (active: " + activeGroup.name + ")"
        : "Active workflow group: " + activeGroup.name + " (add or rename groups in Edit tab)";
    miniWrap.addEventListener("click", (e) => e.stopPropagation());
    miniWrap.addEventListener("pointerdown", (e) => e.stopPropagation());

    const miniIcon = document.createElement("span");
    miniIcon.style.display = "inline-flex";
    miniIcon.style.alignItems = "center";
    miniIcon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;

    const miniLbl = document.createElement("span");
    miniLbl.className = "inv7-wfs-group-mini-label";
    miniLbl.textContent = activeGroup.name;

    const miniCaret = document.createElement("span");
    miniCaret.style.display = "inline-flex";
    miniCaret.style.alignItems = "center";
    miniCaret.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`;

    const miniSelect = document.createElement("select");
    miniSelect.className = "inv7-wfs-group-mini-select";
    miniSelect.setAttribute("aria-label", "Switch workflow group");
    attachSelectInteractionGuard(miniSelect);
    for (const grp of allGroups) {
      const opt = document.createElement("option");
      opt.value = grp.id;
      opt.selected = grp.id === activeGroup.id;
      const favCount = Array.isArray(grp.favorites) ? grp.favorites.length : 0;
      opt.textContent = grp.name + " (" + favCount + ")";
      miniSelect.appendChild(opt);
    }
    miniSelect.addEventListener("change", (e) => {
      e.stopPropagation();
      if (reloadConfigIfUserChanged()) {
        rehydrateAllSwitcherWidgets();
        return;
      }
      const nextGroupId = e.target.value;
      if (switcherConfig.groups.some((g) => g.id === nextGroupId)) {
        switcherConfig.activeGroupId = nextGroupId;
        saveConfig(switcherConfig);
        rehydrateAllSwitcherWidgets();
      }
    });

    miniWrap.appendChild(miniIcon);
    miniWrap.appendChild(miniLbl);
    miniWrap.appendChild(miniCaret);
    miniWrap.appendChild(miniSelect);
    headerRow.appendChild(miniWrap);

    if (isOpen) {
      const saveYoursIconBtn = document.createElement("button");
      saveYoursIconBtn.type = "button";
      saveYoursIconBtn.className = "inv7-wfs-info-btn";
      saveYoursIconBtn.setAttribute("data-icon-btn", "true");
      saveYoursIconBtn.setAttribute("aria-label", "Copy or save active workflow to Your Library");
      saveYoursIconBtn.title = "Copy / Save active workflow to Your Library (Yours)";
      saveYoursIconBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>`;
      saveYoursIconBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        saveOrUpdateCurrentWorkflowInYours(hostDiv, saveYoursIconBtn);
      });
      headerRow.appendChild(saveYoursIconBtn);
    }

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
    if (missingItems && missingItems.length > 0) {
      const warnBar = document.createElement("div");
      warnBar.className = "inv7-wfs-missing-banner";
      const warnTxt = document.createElement("span");
      warnTxt.textContent =
        "⚠ " +
        missingItems.length +
        (missingItems.length === 1
          ? " workflow in group \"" + activeGroup.name + "\" was deleted:"
          : " workflows in group \"" + activeGroup.name + "\" were deleted:") +
        " " +
        missingItems.map((m) => m.label).join(", ");
      const cleanBtn = document.createElement("button");
      cleanBtn.type = "button";
      cleanBtn.className = "inv7-wfs-btn";
      cleanBtn.style.borderColor = "rgba(248, 113, 113, 0.55)";
      cleanBtn.style.color = "#fecaca";
      cleanBtn.textContent = "🧹 Clean missing";
      cleanBtn.addEventListener("click", () => {
        confirmAndCleanMissingForGroup(boundViewGroupId);
      });
      warnBar.appendChild(warnTxt);
      warnBar.appendChild(cleanBtn);
      body.appendChild(warnBar);
    }
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
    const activeEl = document.activeElement;
    if (
      activeEl &&
      activeEl.isConnected &&
      activeEl.classList &&
      activeEl.classList.contains("inv7-wfs-select")
    ) {
      pendingRehydrateAfterSelect = true;
      return;
    }
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
