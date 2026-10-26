# Component Hierarchy Mapping: XRML Dashboard

## 1. Global Shell (Root)
- **RootContainer**: The full-screen viewport managing the "Observational Monolith" shell.
    - **Data Dependencies**: Global theme state, color mode.

---

## 2. Structural Pillars
- **SideNavBar (Left)**
    - **Role**: Primary Navigation and Account Footer.
    - **Data Dependencies**: `active_route`, `user_metadata`.
    - **Responsive Notes**: Fixed (280px) on wide desktops; collapses to drawer on Narrow.
    - **Primary Actions**: Menu routing (Dashboard, Topics, Runs, etc.), Logout.

- **GlobalCommandBar (Top)**
    - **Role**: Instrumental interaction layer.
    - **Data Dependencies**: `search_query`, `selection_state`.
    - **Responsive Notes**: Glassmorphic, sticky.
    - **Primary Actions**: `Search`, `New Research`, `Start Selected`.

- **RightInspector (Right)**
    - **Role**: Contextual observation panel.
    - **Data Dependencies**: `selected_run_id`, `real_time_logs`.
    - **Responsive Notes**: Persistent at 1920px; Drawer-based at 1440px; Overlay at Narrow.
    - **Primary Actions**: `Export Run`, `Full Report Link`.

---

## 3. Main Workbench (Central)
- **Workbench (Fluid Grid)**
    - **Role**: Main Stage for research data.
    - **Responsive Notes**: 12-column system. Transitions to vertical stack on Narrow.

    - **TopicPortfolio (Grid)**
        - **Role**: Lifecycle management for research targets.
        - **Data Dependencies**: `topic_profiles`, `node_metrics`.
        - **Primary Actions**: `Add Topic Profile`, `Edit Profile`.

    - **RecentRunsTable (Data Grid)**
        - **Role**: Telemetry history and run monitoring.
        - **Data Dependencies**: `runs_history`, `gemini_health_data`.
        - **Primary Actions**: `Compare Runs`, `Open Rerun`, `Open Report`.
        - **Responsive Notes**: Adaptive column visibility (High-density).

---

## 4. Operational Modules (Status & Modals)
- **StatusStrip (Global Readout)**
    - **Role**: Real-time health status for Gemini/Stitch.
    - **Status Values**: `Gemini: fallback_only`, `Stitch: Seam prepared`.

- **ScheduleStatusPanel (Inset)**
    - **Role**: Clinical readout for scheduled tasks and MCP health.

- **ProfileDrawer (Ephemeral Overlay)**
    - **Role**: Scoped editing environment for research profiles.
    - **Trigger**: Activated from **TopicPortfolio**.
