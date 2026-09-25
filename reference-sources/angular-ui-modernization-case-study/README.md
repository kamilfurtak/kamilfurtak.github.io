# Angular UI Modernization Case Study

![Angular UI Modernization Case Study](assets/angular-ui-modernization-social-preview.png)

Public portfolio case study by Kamil Furtak, Senior Angular Engineer.

This repository presents an enterprise Angular UI modernization slice: preserving dense Kendo-style workflows while moving rendering behind neutral contracts and PrimeNG-backed wrappers.

## Run the public example

**[Live Angular workbench](https://furtak.dev/angular-ui-modernization-case-study/) · [Source, tests and design decisions](demo/README.md)**

The new public sample switches between a native table and PrimeNG without losing filters,
sorting, selection or a working draft. It includes typed state, validated local draft
recovery and controlled failure/retry behavior, with unit and browser tests in CI.

![Public Angular workbench — synthetic data](assets/public-workbench.png)

This is an independent, AI-assisted implementation created for public review — not an
export of employer code or of the broader private working implementation. The historical
screens and architecture narrative below illustrate the larger case study; they are **not
screenshots of this smaller executable sample**. The runnable sample's exact capabilities,
tradeoffs and limitations are documented in `demo/README.md`.

```sh
cd demo
npm ci
npm start
```

Use Node.js 24. Verification: `npm run typecheck && npm test && npm run build && npm run e2e`
(after `npx playwright install chromium`). No credentials or backend services are needed.

## Broader case-study context

![Prime Migration Workbench](assets/prime-migration-demo.png)

## The Challenge

Enterprise Angular applications rarely fail modernization because a new UI library cannot render a button or table. They fail because mature screens depend on accumulated behavior:

- grid state, filters, sorting, paging, selection, and row styling;
- toolbar actions, dropdowns, toggles, payloads, and multi-row workflows;
- dialogs/windows, confirmations, previews, uploads, and document flows;
- Formly-style forms, map/range actions, communication panels, and shared UI contracts;
- stable selectors and APIs used across existing feature modules.

A direct rewrite pushes that risk into every screen. The goal here was to create a migration boundary that keeps behavior reviewable, typed, and incremental.

## The Approach

I used a three-layer model:

- Neutral UI contracts for tables, toolbars, dialogs, forms, files, maps, and interaction state.
- Compatibility adapters for legacy-shaped selectors, inputs, outputs, services, and models.
- PrimeNG-backed wrappers that render the new UI while keeping app-facing contracts stable.
- A public-safe operational demo that proves the pattern with fake data and realistic workflows.

## Architecture

```mermaid
flowchart LR
  Legacy["Legacy-shaped screen contracts"] --> Compat["Compatibility adapters"]
  Compat --> Core["Neutral UI contracts"]
  Core --> Prime["PrimeNG-backed wrappers"]
  Prime --> Demo["Public-safe operations demo"]
  Core --> Demo
```

The important idea is the migration boundary: feature screens can keep familiar app-level contracts while rendering moves to a new component system behind adapters and reusable wrappers.

## What This Demonstrates

- Angular/Nx architecture with separated UI contracts, compatibility adapters, and PrimeNG-backed wrappers.
- Grid-first operational UX: selection, dependent tabs, dialogs, uploads, map/range actions, messages, and event history.
- Migration thinking across grids, forms, dialogs, documents, maps, files, and shared UI contracts.
- A reusable-library mindset: PrimeNG details stay inside wrapper components instead of leaking into every feature screen.
- Public-safe presentation: no customer data, no private endpoints, no copied implementation, and no private repository history.

## Featured Screens

### Prime Migration Workbench

A dense back-office screen centered on a master grid, dependent records, toolbar actions, selected-row context, local dialogs, document flows, status indicators, and public-safe fake data.

![Prime Migration Workbench](assets/prime-migration-demo.png)

### Grid And Detail Workflow

A focused view of the migration boundary in action: search form, action toolbar, selected row, status tones, detail panel, and local workflow actions in one operational layout.

![Grid and Detail Workflow](assets/prime-migration-grid-detail.png)

### Starter Workbench

A smaller starter view used to present the migration shell, scenario navigation, and public-safe proof surface before the full operational scenario was expanded.

![Starter Workbench](assets/prime-migration-starter-workbench.png)

## My Role

I owned the case study end to end:

- researched the legacy-shaped UI surface and grouped it into migration families;
- designed the architecture around UI-neutral contracts, compatibility adapters, and PrimeNG wrappers;
- implemented Angular standalone components, services, directives, pipes, and typed manifests in the private working repository;
- built the public-safe operational showcase;
- cleaned public naming and documentation so the portfolio version can stand apart from employer-specific or customer-specific history.

## Public Boundary

This repository intentionally excludes private source code, customer names, production routes, endpoint URLs, credentials, real internal screenshots, private repository history, and copied implementation details.

The broader implementation remains private. Only the independently authored `demo/` sample is executable here; the parent case-study assets are separate explanatory material.

## Links

- GitHub: https://github.com/kamilfurtak
- LinkedIn: https://linkedin.com/in/kamilfurtak
- Medium: https://medium.com/@kamilfurtak
- ng-openlayers: https://github.com/kamilfurtak/ng-openlayers
