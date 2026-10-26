# Design DNA: XRML Research Dashboard

## 1. Overall IA
The information architecture is centered around the **"Observational Monolith"** philosophy. The dashboard is a single-pane instrument where the **Main Workbench** acts as the stage for research data, flanked by stable navigation (Left) and contextual observation (Right). Data flows from the Workbench (selection) into the Inspector (deep-dive).

## 2. Menu Placement Rationale
- **Primary SideNav**: Houses the core lifecycle of research (Dashboard -> Topics -> Runs -> Evidence -> Reports). Placing this on the left ensures immediate orientation for the user upon landing.
- **Top Command Bar**: Reserved for global, destructive, or "creation" actions (New Research) and search. This separates "doing" from "navigating."
- **Settings/Status Footer**: Positioned at the bottom of the sidebar to indicate stable, system-level configuration that is accessed less frequently.

## 3. Wide Desktop Grid
- **System**: 12-column fluid grid.
- **SideNavBar**: Fixed 280px.
- **RightInspector**: Fixed 320px.
- **Workbench**: Fluid, filling the remaining `100vw - 600px`.
- **Spacing**: 24px (L) standard gutters, 16px (M) internal padding for workbench cards.

## 4. 1920px Behavior
- **Layout**: Full 3-column expansion. 
- **Workbench**: Topic Portfolio cards displayed in a 4-column row. Recent Runs table shows all available data columns (ID, Profile, Start, End, Status, Nodes, Latency, Cost).
- **Inspector**: Fully expanded and pinned to the right edge.

## 5. 1440px Behavior
- **Layout**: 2-column core. 
- **Workbench**: Topic Portfolio cards shift to a 3-column row. Recent Runs table hides secondary columns (Latency/Cost) to maintain readability.
- **Inspector**: Collapses into a toggleable Drawer/Sheet that overlays the workbench when activated via an icon in the Top Bar.

## 6. Narrow Responsive Behavior
- **Layout**: Single-column vertical flow.
- **Sidebar**: Collapses into a hamburger-triggered drawer.
- **Workbench**: Cards are full-width list entries. Recent Runs table becomes a simplified "Vertical List" of runs with status chips.
- **Footer**: A compact system bar remains at the bottom to show provider health.

## 7. Action Hierarchy
1. **Primary Action**: `New Research` (Top Bar, floating monolith gradient).
2. **Global Navigation**: Left Sidebar links.
3. **Selection Actions**: `Compare Selected`, `Start Selected` (Workbench actions triggered via checkboxes).
4. **Contextual Actions**: `Export Data` (Inspector view).

## 8. Card/Table Density Guidance
- **Principle**: "Compact Clinical." 
- **Tables**: Row height 40px. No borders; use `surface-container-low` background on hover.
- **Cards**: Minimalistic frames. Vertical whitespace (16px) is used to group data points instead of horizontal lines.

## 9. Drawer/Modal Guidance
- **Profile Add/Edit**: Always modal-based (Drawer or Dialog). This flow is ephemeral and should not occupy permanent screen real estate.
- **Run Compare**: Full-screen modal overlay to prevent background visual noise from distracting from data comparison.

## 10. Accessibility Notes
- **Contrast**: Maintain a 4.5:1 ratio for `on-surface-variant` text.
- **Keys**: `New Research` flow is triggerable via keyboard shortcuts indicated in the tooltips.
- **Statuses**: Critical status readouts (e.g., `fallback_only`) must include an icon for visual distinction and an ARIA label for screen readers.
- **Focus**: Blue primary bottom-glow signifies intentional focus on inputs.
