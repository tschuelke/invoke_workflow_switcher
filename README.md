# `invoke_workflow_switcher` (`⇄ Favorite Workflows`) — Stage & Pipeline Switcher for InvokeAI 7 (`webv2`)

A lightweight, zero-core-patch **Custom Node Pack** for **InvokeAI 7 (`webv2`)** that adds a **`⇄ Favorite Workflows`** form element to the Workflow Form Builder (`Edit` tab) and Form View (`View` tab).

---

## Why not just use the Workflow Library Dialog?

In real-world production (such as architectural visualization and multi-stage video pipelines), a single creative task rarely happens inside one monolithic graph:

1. **Stage 1 — Concept / Base Rendering:** e.g. *Nano Banana (Img2Img)* for architectural material & lighting refinement.
2. **Stage 2 — Video Pre-Processing:** e.g. *LTX 2.5 — Video to Control-Vid (Depth / Canny / Pose)*.
3. **Stage 3 — Structural Video Generation / Restyle:** e.g. *LTX 2.5 — Restyle Video (with Union IC-LoRA)*.
4. **Stage 4 — Upscaling / Temporal Extension:** e.g. *LTX 2.5 — 2× Upscale* or *Video Extend*.

### The Problem with Monolithic Workflows vs. The Full Library Dialog
- **Putting all stages into one giant workflow** creates an overwhelming wall of 40+ sliders, switches, and conditional inputs where users constantly lose track of which controls belong to which step.
- **Using the full Workflow Library dialog (`This project` / `Browse` / `Yours`)** interrupts the creative flow: it opens a modal over the workspace, lists dozens of unrelated workflows, and triggers copy-choice confirmation dialogs on repeated switches.

### The Solution: Task-Specific Workflow Chains (`⇄ Favorite Workflows`)
With **`⇄ Favorite Workflows`**, you keep each workflow stage **short, focused, and clean**, and embed a curated **1-click stage switcher** right at the top of the workflow form:
- **Per-Workflow Favorites:** Each workflow stores its own curated list of companion/next-step workflows directly inside its workflow JSON.
- **Global Favorites Preset (`★ Save to Global` / `↓ Load Global`):** Configure your pipeline stages once in Workflow 1, click **`★ Save to Global`**, and stamp the exact same stage list into Workflows 2, 3, and 4 with a single click.
- **3 Color-Coded Sources & Multi-Family Filter Chips:**
  - **Your Library (`Yours` — Cyan):** Your saved, fine-tuned personal workflows.
  - **`+ Built-in (Browse)` (Lavender/Purple):** Optional toggle to include InvokeAI's built-in default workflows.
  - **`+ Node Pack Templates` (Amber/Gold):** Optional toggle to include factory templates shipped inside custom node packs.
- **Compact & Collapsible:** Folds into a single header bar showing the active workflow badge, with an on-demand **`ⓘ` Info** button for workflow descriptions.

---

## Screenshots & Video Demo

*(Add screenshots / video capture here)*

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
3. Filter by workflow family chips (e.g. `LTX 2.5`, `OMNI 1.1`, `Nano Banana`), check the workflows that belong to your pipeline, and optionally click **`★ Save to Global`** to reuse the same selection in sibling workflows.
4. Use the **`Colorized`** checkbox (adopts custom accordion/input surface brightness) and **`Extra Info`** checkbox (always show description vs. compact **`ⓘ`** toggle button) in the card header.
5. Switch to **View** mode (and save/update your workflow in **Your Library**) to switch between pipeline stages with 1 click!

---

## License

Apache License 2.0
