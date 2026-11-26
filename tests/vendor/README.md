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

Every file of this directory has one row below, with the sha256 of its bytes;
`tests/test_the_vendored_bundles_are_listed_with_their_hash.py` is red if a file has no row, a row has no
file, or a hash differs. To update a bundle: drop the pinned upstream dist here, bump the version in the
file name, and change its row (name, version, sha256).

| File | Kind | Version | SPDX license | sha256 | Licence notice |
|---|---|---|---|---|---|
| `bot-detector-2.0.0.esm.js` | client-side bot detector | 2.0.0 | MIT | `f438ed251dc7414ece9d4a2b6941441ad9ffae1a1905817f5f0c7366e701dd86` | in the file header (copyright line and licence reference) |
| `fingerprint-library-5.2.0.umd.min.js` | open-source fingerprint library | 5.2.0 | MIT | `a8de5ead580c42d2e2b01a5752aa08da510852230971aa18554d67cd5de5775b` | in the file header (copyright line and full licence text) |
| `headless-detector-10aa672.js` | headless / stealth / lies detector | git `10aa6724` | MIT | `80f43f364bd973bc03cb5b82de033ce28b2bbb145cb04237632a46ef040a92be` | not in the file: kept below |
| `rules-scanner-1.0.6.es.js` | automation-rules scanner | 1.0.6 | MIT | `5680b88e6075fceab5343d31b43dcb1892a98cff072d37084efdd244fd2cb809` | not in the file: kept below |

## Licence notice of the two bundles that carry none

The MIT licence asks that the copyright notice and the permission notice stay with every copy. Two
bundles are a compiled dist with no header, and the files stay byte-identical to upstream, so the
notice is kept here for both:

> Copyright (c) the authors of the headless detector and of the rules scanner (Abraham Juliot, Antoine Vastel)
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software without restriction,
> including without limitation the rights to use, copy, modify, merge, publish, distribute,
> sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or
> substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
> NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
> NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
> DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
> OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
