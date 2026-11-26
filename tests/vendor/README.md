# Vendored detection libraries (test-only)

These are upstream, unmodified, MIT-licensed browser-fingerprinting / bot-detection
libraries, vendored so the detector e2e tests run **hermetically and identically**
on a dev box and on a GitHub runner (no external CDN at test time, and we want CI offline).

They are served from a localhost HTTP server and loaded into the patched Firefox;
the tests assert the REAL detectors don't flag the stealth build (bot detector: `bot===false`;
rules scanner: engine-agnostic rules clean) and that the fingerprint is stable (fingerprint
library: same `visitorId` across launches).

Every file of this directory has one row below, with the sha256 of its bytes;
`tests/test_the_vendored_bundles_are_listed_with_their_hash.py` is red if a file has no row, a row has no
file, or a hash differs. To update a bundle: drop the pinned upstream dist here, bump the version in the
file name, and change its row (name, version, sha256).

| File | Kind | Version | SPDX license | sha256 | Licence notice |
|---|---|---|---|---|---|
| `bot-detector-2.0.0.esm.js` | client-side bot detector | 2.0.0 | MIT | `f438ed251dc7414ece9d4a2b6941441ad9ffae1a1905817f5f0c7366e701dd86` | in the file header (copyright line and licence reference) |
| `fingerprint-library-5.2.0.umd.min.js` | open-source fingerprint library | 5.2.0 | MIT | `a8de5ead580c42d2e2b01a5752aa08da510852230971aa18554d67cd5de5775b` | in the file header (copyright line and full licence text) |
| `rules-scanner-1.0.6.es.js` | automation-rules scanner | 1.0.6 | MIT | `a5ec46ae9e412470088531b97ff7c1bdbff1da338aef0647d3eb91afc57e73d5` | in the file header (the upstream MIT notice, verbatim) |
