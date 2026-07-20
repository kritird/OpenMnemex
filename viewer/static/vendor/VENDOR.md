# Vendored libraries (offline-first, no build step)

Loaded as classic scripts in this order (each UMD attaches a global the next one reads):

| File | Package | Version | License | Global |
| --- | --- | --- | --- | --- |
| `cytoscape.min.js` | [cytoscape](https://js.cytoscape.org) | 3.34.0 | MIT | `cytoscape` |
| `layout-base.js` | layout-base | 2.0.1 | MIT | `layoutBase` |
| `cose-base.js` | cose-base | 2.2.0 | MIT | `coseBase` |
| `cytoscape-fcose.js` | cytoscape-fcose | 2.2.0 | MIT | `cytoscapeFcose` (auto-registers the `fcose` layout when `cytoscape` is global) |
| `marked.min.js` | [marked](https://marked.js.org) | 15.0.12 | MIT | `marked` |
| `purify.min.js` | [dompurify](https://github.com/cure53/DOMPurify) | 3.2.6 | Apache-2.0 / MPL-2.0 | `DOMPurify` |

fcose is vendored because the built-in `cose` layout is too slow for the V1.2 spike
gate (1k nodes < 2s); fcose is the maintained fast successor from the same lab.

marked renders atom bodies in the V1.3 atom view; DOMPurify sanitizes its output
before insertion — atom bodies can contain ingested third-party markdown (real-repo
ingestion), so raw HTML must never reach the DOM unsanitized.

To update: fetch `https://cdn.jsdelivr.net/npm/<pkg>@<version>/<file>` and bump this
table. Do not edit the files in place.
