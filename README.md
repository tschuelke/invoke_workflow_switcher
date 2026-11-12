https://github.com/user-attachments/assets/fe99e686-2dae-43a9-a519-0049b8f4675c

# `invoke_workflow_switcher` (`⇄ Favorite Workflows`) — Stage & Pipeline Switcher for InvokeAI 7 (`webv2`)

A lightweight, zero-core-patch **Custom Node Pack** for **InvokeAI 7 (`webv2`)** that adds a **`⇄ Favorite Workflows`** form element to the Workflow Form Builder (`Edit` tab) and Form View (`View` tab).

---

## What is the purpose of this node?

**`⇄ Favorite Workflows`** is a fast, inline workflow switcher that lets you map **real-world creative production stages** directly at the top of your workflow form.

Take an architectural visualization studio as a practical example — turning an early design sketch, CAD screenshot, or basic clay render into a finished client presentation rarely happens in a single step. Instead, it follows a natural sequence of focused stages:

### Example Pipeline A — Architectural Presentation (Image & Cloud Video)
1. **Stage 1 — Image-to-Image (Concept to Photo):** Turn a CAD screenshot or hand sketch into a photorealistic architectural rendering (*e.g. Nano Banana Img2Img*).
2. **Stage 2 — Image Restyle (Lighting & Season Variants):** Re-evaluate the rendering and create a night-time or winter mood instead of daylight (*e.g. Nano Banana Remix / Restyle*).
3. **Stage 3 — Image-to-Video (Initial Motion Sequence):** Generate initial 10-second camera flights from the best still renderings (*e.g. Omni Image-to-Video*).
4. **Stage 4 — Video Extend:** Extend the best 10-second clips into 20-second presentation sequences (*e.g. Omni Video Extend*).

### Example Pipeline B — Local Video Production & Restyling (LTX 2.5)
1. **Stage 1 — Base Image / Initial Video Generation:** Create the base image or initial video clip (*Txt2Vid / Img2Vid*).
2. **Stage 2 — Video Pre-Processing (for LTX Control):** Extract structural control passes (Depth map, Canny edges, or DWPose) from the video (*LTX 2.5 — Vid to Control-Vid*).
3. **Stage 3 — Video Restyle & 2× Upscale:** Restyle the video while locking its camera motion and geometry via Union IC-LoRA (*LTX 2.5 — Restyle Video / 2× Upscale*).
4. **Stage 4 — Direct Effects / Extension:** Apply direct IC-LoRA effects or extend the sequence.

By placing **`⇄ Favorite Workflows`** at the top of each workflow in a group, you configure the relevant stages once in the first workflow, click **`★ Save to Global`**, and stamp the exact same favorite list into the sibling workflows with a single click (`↓ Load Global`). You can maintain separate favorite groups for different pipelines (e.g. one group for your Cloud/Nano/Omni pipeline, and another group for your LTX 2.5 pipeline).

---

## Why not put everything into one giant workflow — or just use the Library Dialog?

- **The problem with monolithic "all-in-one" workflows:**  
  Cramming image generation, video pre-processing, structural restyling, upscaling, and video extension into a single graph produces an overwhelming wall of 40+ sliders, switches, and conditional inputs. Splitting a complex process into smaller, stage-specific workflows keeps each form clean and easy to scan — while **`⇄ Favorite Workflows`** lets you jump to the next stage in 1 click.
- **The problem with the full Workflow Library dialog (`This project` / `Browse` / `Yours`):**  
  Opening the full library modal interrupts your flow, lists dozens of unrelated workflows, and prompts with copy-choice confirmation dialogs on repeated switches.

---

## Key Features (v0.5.0)

- **Central Named Workflow Groups (`📁 Group`) + Per-Workflow Overrides:**  
  Organize your workflows into named pipeline groups (e.g. `LTX 2.5 — Workflows`, `Nano Banana / Cloud`) right from the compact header pill or the `Edit` tab, with live group renaming and automatic strikethrough/cleanup for deleted library workflows.
- **1-Click Copy & Reset in `Your Library` (`💾` / `🗑 Remove Checked from Yours`):**  
  Copy checked Node Pack (`file:`) or Built-in (`Browse`) templates into `Yours` with one click (`💾` in `View` or `Edit` mode), update the active workflow in place (`💾 Save / Update in Yours`), or remove checked copies from `Yours` (`🗑 Remove Checked from Yours` in `Edit` mode) to fall back to fresh node-pack templates.
- **Collapsible Section State Persistence:**  
  Automatically remembers and restores which collapsible form sections (`type: 'collapsible'`) you expanded or folded across workflow switches.
- **3 Color-Coded Sources & Multi-Family Filter Chips:**
  - **Your Library (`Yours` — Cyan):** Your saved, fine-tuned personal workflows (default primary source).
  - **`+ Built-in (Browse)` (Lavender/Purple):** Optional toggle to include InvokeAI's built-in default workflows.
  - **`+ Node Pack Templates` (Amber/Gold):** Optional toggle to include factory templates shipped inside custom node packs.
- **Compact & Collapsible:**  
  Folds into a single header bar showing the active workflow badge and group selector, with a **`Colorized`** toggle and an **`Extra Info`** toggle (on-demand **`ⓘ`** button for workflow descriptions).

---

## Screenshots & Video Demo

<a href="https://github.com/user-attachments/assets/71404334-4390-4f3c-91e7-d423ef8beb45">
  <img width="450" alt="Workflow Favorites Switcher Overview (Click to enlarge)" src="https://github.com/user-attachments/assets/71404334-4390-4f3c-91e7-d423ef8beb45" />
</a>

*(Click image to open in full resolution)*

---

## Installation (InvokeAI 7)

This extension is a pure Custom Node Pack — **no InvokeAI core or frontend files on disk are modified**.

### Option A — Install via InvokeAI 7 UI (`Nodes → Add Nodes`)
1. In InvokeAI 7, open **Nodes → Add Nodes**.
2. Paste the repository URL into **Git URL**:
   ```text
   https://github.com/tschuelke/invoke_workflow_switcher.git
   ```
3. Click **Install**.

### Option B — Manual Drop-in Installation
1. Copy the **`invoke_workflow_switcher`** folder into your InvokeAI `nodes` directory:
   ```text
   <InvokeAI-Root>/nodes/invoke_workflow_switcher/
   ```
2. Restart InvokeAI 7 (or click **Reload Nodes** in the Node Manager) and refresh the browser (`Ctrl+F5`).

---

## How to Use

1. Open **Workflow → Edit (`Form`)** and click **`+ Add form element` → `⇄ Favorite Workflows`**.
2. Drag the **`⇄ Favorite Workflows`** card to the top of the form.
3. Filter by workflow family chips (e.g. `LTX 2.5`, `OMNI 1.1`, `Nano Banana`), check the workflows that belong to your pipeline, and click **`★ Save to Global`** to reuse the selection in sibling workflows.
4. Click **`💾 Save / Update in Yours`** to save your workflow in **Your Library** without creating duplicates.
5. Switch to **View** mode to jump between your pipeline stages with 1 click!

---

## Projects, single-user, multi-user & security hardening (v0.4.0)

**Projects:** InvokeAI 7 opens a library workflow as a *copy inside the current project*. Favorites you tick in project A live in A's copy until you click **`💾 Save / Update in Yours`**; in project B, open (or replace with) the library version to get them.

**Where settings live**
- *Favorites of a workflow* are stored inside the workflow itself, so they travel with it (export, sharing, node packs).
- *Global presets* (`★ Save to Global`) are stored in your browser, per InvokeAI address and per user.

**Single-user mode (default):** everything belongs to the built-in admin account and is visible everywhere. Nothing to configure.

**Multi-user mode & Server Security (v0.4.0)**
- **Authenticated endpoints (`GET` only):** `/workflow-switcher/scan` and `/workflow-switcher/file` authenticate through InvokeAI's `get_current_user_or_default`. You see **Your Library** (your own workflows) plus workflows other users **shared** (green *Shared* tag). Admins additionally see other users' private workflows (*Other User* tag).
- **Safe saving:** **`Save / Update in Yours`** only ever updates a workflow you own. Saving a shared or foreign workflow creates your own copy, so nobody's original is overwritten.
- **Zero client path control & opaque file IDs:** Only server-configured workflow folders (`<InvokeAI>/workflows` and `<InvokeAI>/nodes/<pack>`) are scanned. No server filesystem paths are exposed to the browser; templates are referenced exclusively by opaque 16-character SHA-256 IDs (`file:<id>`) against a server-side allowlist.
- **Resource & event-loop protection:** Directory traversal enforces strict depth (`<= 3`), file count, and pre-read file size (`<= 2 MiB`) limits, caches results in memory, and runs off the ASGI event loop via `asyncio.to_thread`.

---

## License

Apache License 2.0
