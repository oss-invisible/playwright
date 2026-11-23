# Source of the fixtures of tests/data/ab_host_geo

What the geolocation rules of `scripts/ci_ab_host.py` read, on real Windows footprints: the stock control (`stock_control_problems`: it must load
`capabilityaccessmanagerclient.dll`, the first read of the host for the position; the utility process `-sandboxingKind 3` is not asked of it) and the rule on
our engine (`geolocation_problems`: no such module and no such process in any launch).

## Runs

| run | repository | workflow | job | what |
|---|---|---|---|---|
| 45257636942 | oss-invisible/firefox_antidetect_patch | republish (created 2026-11-23T20:16:19Z) | 137993937441 (gate / wrapper.e2e.ab_host-win-x86_64) | windows-2022: stock WITH the module and WITHOUT the utility process; candidate (the 69) with neither |
| 45165805370 | oss-invisible/playwright | e2e (created 2026-11-23T11:30:21Z) | 137662424125 (e2e (host, windows)) | windows-2022, pull request: stock WITH the module AND the utility process |

Origin: the artifact of the job, `ab-host/` (kept outside the repository, with the rest of the artifact: `art/ab-host/` for 45257636942, `art/invpw-evidence/ab-host/` for 45165805370).

| fixture | origin file | sha256 of the origin file | sha256 of the fixture | bytes |
|---|---|---|---|---|
| 45257636942/footprint-stock.json | ab-host/footprint-stock.json | 82a75bf50e2d7a9f96ba00ea57980e0eb2f06bc6925ecba0bb8d8c47ef3069c9 | a1ffeaeb4746c3d0f8a1c2a16bd137f533f15b4a688bc08719f764bfa035255f | 11426 |
| 45257636942/footprint-candidate.json | ab-host/footprint-candidate.json | 086f9b075617d849798a9743d9e442d65cdb2cfb93a59ba4ef9532b67a3bd334 | 7daaba9a1282e032bcedeb3fbdb1aad76cf60ff431423545800dc071540c3e96 | 110594 |
| 45165805370/footprint-stock.json | ab-host/footprint-stock.json | 1ea907d416d4fb3fe661d9b0339a21d473fe4b3474c6534fe0210f0d873cddcb | d6de929f7c7816b259d2360653a46c116d21ce93860e0c0c5e0e80e33544f803 | 10721 |

## Reduction

The stock footprint keeps `format`, `engine`, `platform`, `leg` and, of `stock`: `seed`, `steps`, `kinds`, `files`, `problems`, `started` and `touched_by` reduced to the
entry of `capabilityaccessmanagerclient.dll` (the rule reads `kinds`, `files`, `steps`, `problems` and `started`). The candidate footprint keeps `format`, `engine`, `platform`,
`problems` and, per launch (`warmup`, `idle`, each seed): `seed`, `kinds` and `files` as they are. Everything else (processes, lifecycle, net, trace, creates, paths of ours) is
left out. JSON, sorted keys, compact.

## Neutralisation

The same as in tests/data/ab_host_aa/45088980743/SOURCES.md: the folder `users` of the C drive becomes `profiles` in every path and key, the same string everywhere, so two paths are
equal after it if and only if they were before. The reduction drops `net`, so no address of the hosts the engine reached is left (checked: no IPv4 literal in the fixtures).

## What was measured (every footprint with a stock control, and the engine in every launch)

| run | stock: `capabilityaccessmanagerclient.dll` | stock: utility `-sandboxingKind 3` |
|---|---|---|
| 45165805370, 45170547415, 45252267809 (pull requests, windows-2022) | yes | yes |
| 45206303882 (gate, windows-2025), 45257636942 (gate republish, windows-2022) | yes | no |

The 68 (twice) and the 69 candidate, in EVERY launch (warm-up, idle, seeds) of 45121964904, 45206303882, 45257629988 and 45257636942: neither the module nor the process.
