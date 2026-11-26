# Vendored detection libraries (test-only)

These are upstream, unmodified, MIT-licensed browser-fingerprinting / bot-detection
libraries, vendored so the detector e2e tests run **hermetically and identically**
on a dev box and on a GitHub runner (no external CDN at test time, and we want CI offline).

They are served from a localhost HTTP server and loaded into the patched Firefox;
the tests assert the REAL detectors don't flag the stealth build (bot detector: `bot===false`;
rules scanner: engine-agnostic rules clean; headless detector: `headlessRating===0` + no JS-proxy
tells) and that the fingerprint is stable (fingerprint library: same `visitorId` across
launches). The headless detector runs fully offline - the tests abort every non-loopback request,
so its optional crowd-comparison POST never fires and the verdict is computed locally.

| File | Kind | Version | License |
|---|---|---|---|
| `bot-detector-2.0.0.esm.js` | client-side bot detector | 2.0.0 | MIT |
| `fingerprint-library-5.2.0.umd.min.js` | open-source fingerprint library | 5.2.0 | MIT |
| `rules-scanner-1.0.6.es.js` | automation-rules scanner | 1.0.6 | MIT |
| `headless-detector-10aa672.js` | headless / stealth / lies detector | git `10aa6724` | MIT |

Each file is a pinned upstream dist, unmodified: its own header names the project, its
authors and the MIT licence, and a changed byte shows in `git diff`. To update: fetch the
pinned dist of the project (the npm dist for a versioned package, the file at a commit SHA
for the headless detector), drop it here, and bump the version in the filename + this table.
