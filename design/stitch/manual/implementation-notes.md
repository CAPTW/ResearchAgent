# Implementation Notes: XRML Dashboard Handoff

## 1. Technical Architecture (React/Vite)
- **Component Naming**: Use PascalCase for all functional components (e.g., `TopicCard`, `RecentRunsTable`). Follow the pattern `[Feature][Domain][Type].tsx`.
- **Styling**: Utilize Vanilla CSS or CSS Modules. Map CSS Variables directly to the `tokens-extended.json`.
- **Local-First Pattern**: Implement a React Context or a lightweight state store (e.g., Zustand) to mirror the "Observational Monolith" state (selected runs, active topic).

## 2. Layout Behavior
- **Grid Layout**: Implement the 12-column grid system using **CSS Grid**.
- **The "Monolith" Shell**: The `AppShell` should use `display: grid` with `grid-template-columns: 280px auto 320px` for the 1920px view.
- **Glassmorphism**: Use `backdrop-filter: blur(24px)` with a semi-transparent `surface-container-highest` background for the `GlobalCommandBar`.

## 3. Responsive Behavior
- **Breakpoint Strategy**:
    - **Wide (1920px+)**: Standard 3-column layout.
    - **Table (1440px - 1919px)**: Hide `RightInspector` by default (`transform: translateX(100%)`). Trigger it as a sidebar sheet.
    - **Narrow (< 768px)**: Switch `AppShell` to a single column. Move Sidebar to a Drawer. Convert `RecentRunsTable` to a simplified mobile list.

## 4. Accessibility Notes
- **Semantic HTML**: Use `<nav>` for the Sidebar, `<main>` for the Workbench, and `<header>` for the Command Bar.
- **Run Status ARIA**: Status chips (`fallback_only`, `Seam prepared`) must have `aria-live="polite"` if they update dynamically.
- **Keyboard Targets**: Use `tabindex="0"` for topic cards and ensure `Enter` triggers the selection flow.

## 5. Interaction Notes
- **The "No-Line" Selection**: Indicate selection in the `RecentRunsTable` via a 2px vertical bar of `var(--primary)` on the left edge of the row, avoiding full-row borders.
- **Drawer Animation**: Use a `spring` transition for the `InspectorPanel` and `ProfileDrawer` for a premium, heavy feel (low bounce, high damping).
- **Status Indicators**: Consistently display the following values as clinical chips:
    - Gemini Health: `fallback_only`
    - Stitch Status: `Seam prepared`
