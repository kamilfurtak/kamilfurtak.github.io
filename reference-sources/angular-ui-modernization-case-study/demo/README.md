# Public Angular migration workbench

A small, original, AI-assisted Angular 21 sample showing **state ownership across a UI-adapter replacement**. It is not extracted from the private implementation described by the parent case study.

[Live demo](https://furtak.dev/angular-ui-modernization-case-study/) · [CI](https://github.com/kamilfurtak/angular-ui-modernization-case-study/actions)

## Run and verify

Use Node.js 24 and npm. All records are fictional and local.

```sh
cd demo
npm ci
npm start
```

Open the Angular CLI URL. For the verification suite:

```sh
npm run typecheck
npm test
npm run build
npx playwright install chromium
npm run e2e
npm audit --omit=dev
```

The same browser suite can verify a deployed build:

```sh
DEMO_URL=https://furtak.dev/angular-ui-modernization-case-study/ npm run e2e
```

## A useful review path

1. `src/domain.ts`: readonly domain records, pure query/selection operations and validated draft decoding.
2. `src/workbench.store.ts`: component-owned signals, last-request-wins loading, preserved selection on failure and teardown protection.
3. `src/grid-adapters.ts` / `src/prime-grid.ts`: the same typed inputs and domain events; PrimeNG details do not reach the state owner.
4. `src/app.ts` / `src/draft-storage.ts`: a typed reactive form and replaceable browser-storage boundary.
5. `src/*.spec.ts` / `e2e/workbench.spec.ts`: transition tests plus real rendering, adapter replacement, retry, reload, malformed/blocked storage and keyboard/mobile flows.

## Design decisions and tradeoffs

- **Feature state outlives either renderer.** Switching adapters deliberately destroys the table component, not the owning store or form. The user can filter selected rows out of view without silently losing their selection.
- **Native first, PrimeNG on demand.** `@defer` keeps the vendor table outside the initial bundle. Both renderers use the same visual tokens; PrimeNG runs in unstyled mode. This is a rendering boundary, not a reimplementation of the vendor's full grid API.
- **Controlled failures, not pretend backend evidence.** `CASE_API` is a replaceable port backed by an asynchronous in-memory fixture. The failure button rejects that fixture; there is no HTTP backend, authentication or database here.
- **Local drafts are explicit.** The form saves only when requested. A schema/version check rejects damaged storage, and blocked/quota-exhausted storage leaves the unsaved draft visible. Browser storage is not encryption; the demo tells users not to enter sensitive data.
- **Deliberately small.** No global state library, generic component factory or new monorepo is needed for this slice. Sorting and filtering are client-side; large datasets would require a paged server contract. No claim of Kendo compatibility, full accessibility certification or production readiness is made.

## Verification evidence

At implementation acceptance, 18 unit tests and 5 Chromium browser scenarios pass. CI repeats type checking, unit tests, the production build and browser scenarios before deployment; consult the current run rather than treating these counts as a permanent guarantee.

Tests demonstrate fake-service behavior, not external-service uptime. The browser suite uses isolated contexts and synthetic drafts only. The mobile check establishes no document-level horizontal overflow at 390 px; the dense table has its own horizontal scroll region.

## Authorship and licensing

New sample code was developed for this public portfolio with AI assistance and reviewed and exercised before publication. It contains no employer implementation, private repository history, credentials or customer data. `LICENSE` applies to this directory's new code; framework dependencies retain their own licenses. The parent's historical images and private case-study claims are separate artifacts.
