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

## Key Features

- **Per-Workflow Favorites stored in the Workflow JSON:**  
  Each workflow remembers its own curated list of companion workflows.
- **Global Favorites Preset (`★ Save to Global` / `↓ Load Global`):**  
  Set up your favorite stages once and copy them into sibling workflows with a single click.
- **Direct 1-Click Library Update (`💾 Save / Update in Yours`):**  
  Update the current workflow directly in **Your Library** in place without creating duplicate copies, while automatically preserving InvokeAI's native library link (`🔗`).
- **3 Color-Coded Sources & Multi-Family Filter Chips:**
  - **Your Library (`Yours` — Cyan):** Your saved, fine-tuned personal workflows (default primary source).
  - **`+ Built-in (Browse)` (Lavender/Purple):** Optional toggle to include InvokeAI's built-in default workflows.
  - **`+ Node Pack Templates` (Amber/Gold):** Optional toggle to include factory templates shipped inside custom node packs.
- **Compact & Collapsible:**  
  Folds into a single header bar showing the active workflow badge, with a **`Colorized`** toggle (adopts custom accordion/input surface brightness) and an **`Extra Info`** toggle (on-demand **`ⓘ`** button for workflow descriptions).

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
3. Filter by workflow family chips (e.g. `LTX 2.5`, `OMNI 1.1`, `Nano Banana`), check the workflows that belong to your pipeline, and click **`★ Save to Global`** to reuse the selection in sibling workflows.
4. Click **`💾 Save / Update in Yours`** to save your workflow in **Your Library** without creating duplicates.
5. Switch to **View** mode to jump between your pipeline stages with 1 click!

---

## License

Apache License 2.0
