# Angular 17 / ng-openlayers starter

A small, runnable **legacy-version example**, not a standalone product. The
[application source](src/main.ts) composes a map, view, geographic center,
OpenStreetMap tile source, default interactions, scale line, zoom slider and
zoom buttons using `ng-openlayers`.

## Version boundary

This example uses Angular **17** and `ng-openlayers` **17.1.2**. It is retained
as an older integration sample, not the current recommended dependency baseline.
Use the maintained [ng-openlayers repository](https://github.com/kamilfurtak/ng-openlayers),
[installation guide](https://github.com/kamilfurtak/ng-openlayers#installation)
and [current demo](https://ng-openlayers.furtak.dev/) for new applications.
Choose a library version whose Angular peer dependencies match your application.

## Run the example

Use Node.js 20, supported by this Angular version:

```sh
npm install
npm start
```

Open the URL printed by Angular CLI. `npm run build` builds the sample.
The tile layer needs network access to OpenStreetMap; a successful build alone
does not verify tile availability or interactions. There is no automated test
suite in this starter; tested lifecycle behavior belongs to the maintained library.

[Open on StackBlitz](https://stackblitz.com/github/kamilfurtak/stackblitz-starters-amwibo)

This repository is intentionally not a featured portfolio project. Its role is
an inspectable, minimal composition example linked to the canonical library.
