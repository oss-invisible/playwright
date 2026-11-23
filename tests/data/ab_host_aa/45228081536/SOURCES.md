# Source of the fixtures of run 45228081536

- what: Windows, A/A of the 68 (the published engine twice), the run whose A/A was red on the pingsenders
- repository: oss-invisible/playwright
- run: 45228081536
- job: 137879971705 (`e2e (host, windows)`)
- origin: the artifact of the job, `art/invpw-evidence/ab-host/` (kept outside the repository, with the rest of the artifact of the job)

| fixture | origin file | sha256 of the origin file | sha256 of the fixture | bytes |
|---|---|---|---|---|
| footprint-68.json | ab-host/footprint-68.json | 0f45112a954a29913136ea39fe941f5948c4e05ae73bc2c67982ab0e7b249512 | 1e3dd3ef409ebd80e98cdb55bddcb42e1d060e3e7f5c3dd6999edd3907c42b21 | 90783 |
| footprint-68-again.json | ab-host/footprint-68-again.json | 487933bc13edb6f1194833a05598e8bf89549fa71211abf628cb9cb2d6e1ff91 | e2d92338c4282f4acd8e8fcca699d0da23809404466365360472a96165fa66a8 | 71783 |

## What the run said

The verdict of the A/A (`diff-a-a.json`) was red on two lines:

- seed 4, side B: `2 pingsender processes, not 0 or 3`;
- seed 63: `proc.socket<-pingsender: A 3 | B <missing>`.

The pingsenders of the 68 (`backgroundtask/pingsender`) per launch, warm-up and seeds 0, 2, 4, 6, 63: first run 3, 3, 3, 3, 3, 3; second run 1, 3, 0, 2, 0, 3. The sockets they start
(`socket<-pingsender`): first run warm-up, seed 6 and seed 63; second run none.

## Reduction

The reduction is the one of tests/data/ab_host_aa/45088980743/SOURCES.md (`kinds`, `pingsenders`, `net`, `inventory` of every launch as they are, and the host paths as a table `paths` with a
mask per launch and the union of the processes that touched each), with one field more per launch, `spawned_by` (the kinds that spawned each kind: the process to blame for a kind the rule
lists). The launches are the warm-up, `s0`, `s2`, `s4`, `s6`, `s63` and `idle`. Nothing else of the footprint is kept (the processes, the lifecycle, the close, the trace, the creates, the
paths of ours); the test rebuilds a footprint of the oracle's format from it (`rebuild` in tests/test_ab_host_aa_stable.py).

## Neutralisation

The same as in the other fixtures: every public IPv4 address becomes an address of the documentation ranges one to one (the footprints of this run hold none: the net of a Windows run is not
covered) and the folder `users` of the C drive becomes `profiles` in every path (the folder itself included), the same string for every path (the number of distinct paths, every mask and every set of touching
processes are unchanged).

## The old judgement on these data (scripts/ci_ab_host.py at e3572f28)

A/A red: the two problems above. After the change (the kinds of process judged by the rule of the paths, the pingsender kinds only listed) the A/A is green and the pingsender kinds are in
`not_judged["kinds"]`: `backgroundtask/pingsender` in 6 of 6 measured launches of the first run and 4 of 6 of the second, `socket<-pingsender` in 3 of 6 and 0 of 6.
