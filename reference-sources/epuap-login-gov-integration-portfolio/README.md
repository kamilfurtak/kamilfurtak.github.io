# ePUAP / login.gov Integration Portfolio

Portfolio package for a cross-stack identity integration case study.

This project presents the architecture, technology breadth, tradeoffs and
technical communication behind the work. It is written for recruiters, hiring
managers and technical reviewers who want to see how frontend expertise connects
with backend integration, identity protocols and enterprise service workflows.

![Integration portfolio hero](assets/hero-integration-lab-branded.png)

## What is public — and what is not

This repository is a **static architectural walkthrough**, not an executable identity
integration or a login service. The Angular/NestJS/.NET/Java descriptions below discuss
prototype tracks; their application source, certificates and runtime environments are
not distributed here. Opening these pages does not exercise SAML, SOAP, XML signatures
or real login.gov/ePUAP endpoints.

The diagrams and images are explanatory portfolio material, not proof of certification,
provider approval, a production deployment or successful production authentication.
No affiliation with or endorsement by the service operators is claimed. My emphasis is
frontend integration, contract analysis and explaining engineering decisions; protocol
and security correctness would require independent implementation review and tests.

## Case Study Focus

The case study shows that the work goes beyond Angular UI implementation:

- Angular and TypeScript integration test client.
- NestJS / Node.js SAML-oriented API prototype.
- ASP.NET Core / .NET 8 API prototype with Swagger/OpenAPI visibility.
- Java / Spring client work with JAX-WS, Apache CXF, OpenSAML-oriented flows and
  XML security libraries.
- SAML, certificates, XML signatures, SOAP/WSDL and generated contracts.
- Clear presentation of complex public-sector identity integration concerns.

## What This Demonstrates

The project combines a browser-facing test client with several backend
implementation tracks. Angular is used to make login flows, callbacks, API
responses and error states visible. NestJS and ASP.NET Core provide alternative
API prototypes for SAML-oriented endpoints and generated contracts. The Java
track covers older but still important enterprise integration concerns such as
JAX-WS, Apache CXF, WSDL-generated models, XML security and OpenSAML-oriented
flows.

The portfolio artifact focuses on architecture and engineering judgment: how to
compare runtime stacks, how to keep contracts visible, how to reason about
certificate-heavy workflows, and how to explain complex integration work clearly.

The landing page also includes a design-decision section describing why Angular
was used as the inspection surface, why backend tracks were compared separately,
why generated contracts mattered and how the case study supports a technical
interview or client conversation.

## Technical Scope

The case study focuses on integration concerns that are valuable in real
enterprise and public-sector systems:

- SAML request initiation, browser handoff, callback state and artifact-oriented
  response handling.
- XML signature and canonicalization awareness, certificate validity concerns
  and protocol validation.
- Swagger/OpenAPI schemas, request/response DTO contracts and generated
  TypeScript models for frontend/backend agreement.
- SOAP/WSDL service contracts, Java client generation, Apache CXF/JAX-WS,
  XMLSec and OpenSAML-oriented research.
- Failure-mode analysis around schema drift, invalid callback state, artifact
  mismatch, certificate/configuration problems and protocol-level errors.

## Portfolio Pages

- [Overview](index.html) - visual case-study landing page with the technology
  map, architecture flow, artifacts and engineering impact.
- [Architecture](docs/architecture.html) - system shape and responsibility split
  across Angular, NestJS, ASP.NET Core and Java/Spring.
- [Stack comparison](docs/stack-comparison.html) - how the runtime tracks
  complement each other in identity integration work.

## Why It Matters

Many enterprise frontend roles eventually touch integration problems that are
not purely visual: redirects, callbacks, generated contracts, backend error
states, legacy SOAP services, certificate configuration and identity-provider
behavior. This case study shows how those concerns can be investigated without
losing the frontend perspective.

The Angular client represents the browser side of the workflow, while the
backend tracks show how the same identity problem changes when explored through
NestJS, ASP.NET Core and Java/Spring. The result is a portfolio artifact that
connects UI engineering, API design, service integration and technical
communication into one coherent story.

## View Locally

This is a static site. Open `index.html` directly or serve the folder:

```bash
python3 -m http.server 4173
```

Then open:

```text
http://localhost:4173
```

## Deeper Notes

- [Architecture](docs/architecture.html)
- [Stack comparison](docs/stack-comparison.html)
