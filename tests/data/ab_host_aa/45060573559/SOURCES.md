# Source of the fixtures of run 45060573559

- what: Windows, A/A and A/B (the candidate was installed in another folder)
- repository: oss-invisible/firefox_antidetect_patch
- run: 45060573559
- job: 137346793388 (gate / wrapper.e2e.ab_host-win-x86_64)
- artifact: wrapper.e2e.ab_host-windows (created 2026-11-22T21:08:09Z)
- downloaded: 2026-11-23, `gh run download 45060573559 --repo oss-invisible/firefox_antidetect_patch -n wrapper.e2e.ab_host-windows -D <folder>/data`
- the downloaded artifact is kept outside the repository, with a `meta.json` of the sha256 of every file

| fixture | origin file | sha256 of the origin file | sha256 of the fixture | bytes |
|---|---|---|---|---|
| footprint-68-again.json | ab-host/footprint-68-again.json | 41263526d97abe22270698c22b4d2a2d163ccf61382c272abeb78fd9cb6982b6 | c8ca6c583f5fbcbac9ac0648dac29f58882c152c78ef82b9ed0f0986150f1fab | 89073 |
| footprint-68.json | ab-host/footprint-68.json | 55bf8cb960d099670fc80b9fab1afe6932c77ea967e783274f7d404e293326dd | e231b2b166157af9328374a2ecbd4ee0ca2f3072acdc24cfcd380fa39db880c2 | 105945 |
| footprint-candidate.json | ab-host/footprint-candidate.json | 3dd39734573cab78294c73efb084cf5dbd78cb4f6afa2e6ba015f43f82bcecda | 94a1857d12a6c43b9278393eacc87f11ca68f5043d59bfbd38a22a8b991b5adb | 98675 |

## Reduction

For every footprint the oracle wrote (`footprint-68.json`, `footprint-68-again.json`, `footprint-candidate.json`) the reduction keeps, per launch (the warm-up, each seed `s<seed>`,
`idle`, `bare`, in that order): `kinds`, `pingsenders`, `net` and `inventory` as they are, and for the host paths a table `paths: {path: {"mask", "by"}}` where `mask` has one
character per launch (`1` when the launch touched the path) and `by` is the union of the processes (`touched_by`) that touched it. The paths are the `files` of each launch (host paths,
already classified by the oracle), nothing is added, nothing is dropped. Everything else of the footprint (the processes, the lifecycle, the close, the crash evidence, the trace, the
creates, the paths of ours) is left out: the rule does not read it. The test rebuilds a footprint of the oracle's format from it (`rebuild` in tests/test_ab_host_aa_stable.py).

    launches = {"warmup": fp["warmup"], **{"s%s" % s["seed"]: s for s in fp["seeds"]}, "idle": fp["idle"], "bare": fp["bare"]}
    paths = {p: {"mask": "".join("1" if p in l["files"] else "0" for l in launches.values()),
                 "by": sorted({k for l in launches.values() for k in l.get("touched_by", {}).get(p, [])})} for p in sorted({p for l in launches.values() for p in l["files"]})}
    out = {"engine", "platform", "launches": [names], "seeds": {name: seed}, "launch": {name: {kinds, pingsenders, net, inventory}}, "paths": paths}   # json, sort_keys, compact

Checked: the old judge (b44c5d64) on the rebuilt footprints gives the same verdicts and counts as on the original footprints.

## Neutralisation (after the reduction)

The reduced footprints carry the addresses of the hosts the engine reached and the account folder of the runner; they are made neutral, in the same way in every fixture of every run:

- every public IPv4 address (the scanner's own definition: not private, not documentation, not CGNAT, not loopback, not multicast) is replaced by an address of the documentation
  ranges (203.0.113.0/24, then 198.51.100.0/24, then 192.0.2.0/24), ONE to ONE: the distinct public addresses of all the fixtures, in numeric order, take the next free address, so two equal
  entries stay equal and two different ones stay different. The judge reads a destination only as its class (`net_class`: `<kind>: connect tcp public:443`, whatever the address); the check
  before and after on every net entry of every launch (917 of them) gives the same class, and the same pingsender destination, entry by entry;
- the folder `users` of the C drive becomes `profiles`, and the first folder under `/home/` becomes the same name under `/var/`, in every path and every key. The replacement is the
  same string for every path, so two paths are equal after it if and only if they were before (checked: the number of distinct paths of each footprint is the same, every mask and every set of
  touching processes is unchanged). The judge does not read the class `host` or `ours` of a path, only the paths, so nothing else depends on the spelling of those prefixes.

The fixtures are the reduction followed by this step; the sha256 of the fixtures in the table above are the ones after it.

## The old judgement on these data (scripts/ci_ab_host.py at b44c5d64)

A/A red: 1 problem, 13 rare system modules only run A has in its measured launches (more than 5). A/B red: 217 differences (the install-folder hash paths among them).
