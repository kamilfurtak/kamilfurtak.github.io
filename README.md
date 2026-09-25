# Kamil Furtak — Angular engineering portfolio

Senior Angular Engineer focused on UI modernization, reusable component APIs and
geospatial interfaces. This portfolio connects working examples to their source,
tests and engineering decisions.

[Visit furtak.dev](https://furtak.dev/) · [GitHub profile](https://github.com/kamilfurtak) · [Contact on LinkedIn](https://linkedin.com/in/kamilfurtak)

## Where to start

| Work | Engineering focus | Review it |
| --- | --- | --- |
| **ng-openlayers** — maintained npm library | Connecting Angular components to OpenLayers lifecycle and projection behavior; validating the built package in an independent consumer. | [27 live examples](https://ng-openlayers.furtak.dev/) · [Source and validation](https://github.com/kamilfurtak/ng-openlayers) |
| **Angular UI workbench** — interactive portfolio sample | Replacing a table renderer while retaining query state, selected rows and unfinished form edits. | [Try the demo](https://furtak.dev/angular-ui-modernization-case-study/) · [Code and browser tests](reference-sources/angular-ui-modernization-case-study/demo) |
| **Identity integration** — written architecture case study | Browser/API responsibility, generated contracts and failure boundaries around SAML and SOAP/WSDL. | [Read the walkthrough](https://furtak.dev/epuap-login-gov-integration-portfolio/) |

For a focused technical review, use my [engineering decisions and source guide](https://github.com/kamilfurtak/kamilfurtak/blob/main/engineering-notes.md).
It explains the problem, approach, observable behavior and limits of each example.

## A workflow you can verify

Open the [UI workbench](https://furtak.dev/angular-ui-modernization-case-study/),
select a case, filter the table and write a draft. Switch between native and
PrimeNG tables: the feature state survives the renderer replacement. The source
includes separate boundaries for state, rendering and draft storage, with
browser regressions for replacement, retry and persistence failures.

The workbench is an independent, AI-assisted sample using fictional records.
The identity pages are a static architecture walkthrough. Each artifact states
what a reviewer can verify from the public material.

## Accepted contributions

- [bolt.diy #1322](https://github.com/stackblitz-labs/bolt.diy/pull/1322): searchable model selection with keyboard navigation and focus handling.
- [Hindsight #3656](https://github.com/vectorize-io/hindsight/pull/3656): consistent batch schema configuration with regression tests for conflicting flags.

Both changes were merged by their respective projects. The maintained
ng-openlayers library has its own [repository](https://github.com/kamilfurtak/ng-openlayers)
and [npm package](https://www.npmjs.com/package/ng-openlayers).

<details>
<summary>Publication and source provenance</summary>

This repository publishes the prerendered portfolio and selected public source
snapshots. The editable site source lives in the private
`portfolio-lab-monorepo/cv/furtak-dev` repository path. Update that source before
regenerating HTML and bundles; its publisher validates indexability.

Preserve `reference-sources/`, `angular-ui-modernization-case-study/` and
`epuap-login-gov-integration-portfolio/` when republishing. Source snapshots retain
their original licenses and commit IDs in
[snapshots.json](reference-sources/snapshots.json).

[Historical tools, source index and downloads](reference-sources) remain available
as references. Their preservation does not imply active maintenance. Full
repository histories and private experiments are stored separately.

</details>
