# Source of the fixture of run 45221868374

- what: Windows host oracle, the idle launch of side A (`firefox-68`), the launch in which the tree "did not exit within 120 s of the close"
- repository: oss-invisible/playwright
- run: 45221868374
- job: 137858126009 (`e2e (host, windows)`)
- origin file: `art/invpw-evidence/ab-host/footprint-68.json`, key `idle`, key `lifecycle` (kept outside the repository with the rest of the artifact of the job)
- sha256 of the origin file: 53e383c8e808bbdcf1802007a36129c021c34cba324c12d966685cc157783aab (519890 bytes)

| fixture | sha256 of the fixture | bytes |
|---|---|---|
| idle-lifecycle.json | c4a661f8d421397e4e483bd0bae3a50ca06f71a1a6b0e15b871b07eb9a925375 | 6372 |

## Reduction

`{"launch": "idle", "lifecycle": [{"time", "event", "kind", "how"}]}`: the 53 rows of `idle.lifecycle` as they are (the start of each process of the tree, from its 4688 event, and
its end, from the Kernel-Process stop, in the order of the time each carries). Nothing is added, nothing is dropped. The rows hold no address, no account name, no path.

## What the footprint does not hold

The footprint has no pid, no creator pid, no record id and no `ParentProcessName`: the raw 4688 events of the job are not in its artifact (`wevtutil` output is read in memory and
not kept). The test (`test_the_idle_launch_of_run_45221868374_...` in tests/test_ab_host.py) therefore rebuilds the events from these rows, with the kinds and the order that
the run had and pids and creators that the test gives; what it gives is written in the test.
