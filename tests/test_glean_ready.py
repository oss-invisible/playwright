"""A launch of the oracles closes only AFTER Glean has initialised (scripts/ci_crash_evidence.py: `wait_for_glean`).

Run 45121964904 (the network A/A, Linux, launch `exit-pings`): a session that closes before Glean has initialised normally makes Glean start inside the shutdown, where its
`glean.init` thread can die with the panic "No database found" and the parent exits with -11. It is Mozilla's race, the same in the retail browser. The oracles do not
change the engine or a preference: they wait for a CONDITION read from the host, the file the database writes on its first write (`datareporting/glean/db/data.safe.bin`;
the folder `db` is born when the database is opened, the file at the first write). In normal sessions, stock and ours, it is there at about +8 s (run 43649596941).

The clock and the sleep are fakes: nothing here sleeps.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_crash_evidence as crash  # noqa: E402

pytestmark = pytest.mark.unit


class Clock:
    """A clock that only the fake sleep moves, and a record of how long the caller slept."""

    def __init__(self, on_sleep=None):
        self.t, self.on_sleep = 0.0, on_sleep

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.on_sleep:
            self.on_sleep(self.t)


def test_it_does_not_return_while_the_database_file_is_missing_and_returns_when_it_appears(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    target = profile / crash.GLEAN_DB

    def born(t):                                   # the file is written about +8 s into the wait
        if t >= 8.0 and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00")

    clock = Clock(born)
    waited = crash.wait_for_glean(profile, sleep=clock.sleep, now=clock.now)
    assert 8.0 <= waited < 8.5 and clock.t == waited, "it slept until the file existed, then returned"


def test_with_the_file_already_there_it_returns_at_once(tmp_path):
    target = tmp_path / crash.GLEAN_DB
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x00")
    clock = Clock()
    assert crash.wait_for_glean(tmp_path, sleep=clock.sleep, now=clock.now) == 0.0 and clock.t == 0.0


def test_the_folder_alone_is_not_enough_and_the_limit_is_red_with_the_reason(tmp_path):
    (tmp_path / crash.GLEAN_DB).parent.mkdir(parents=True)     # `db` is born when the database is opened: the file is the first write
    clock = Clock()
    with pytest.raises(crash.GleanNotReady) as caught:
        crash.wait_for_glean(tmp_path, sleep=clock.sleep, now=clock.now)
    message = str(caught.value)
    assert "Glean did not initialise in 60 s" in message and "data.safe.bin" in message and str(tmp_path) in message and "45121964904" in message
    assert 60.0 <= clock.t < 61.0, "it waited the whole limit and not more"


def test_the_limit_is_the_callers_and_a_launch_without_a_profile_is_red_at_once(tmp_path):
    clock = Clock()
    with pytest.raises(crash.GleanNotReady, match="did not initialise in 5 s"):
        crash.wait_for_glean(tmp_path, limit_s=5.0, sleep=clock.sleep, now=clock.now)
    clock = Clock()
    with pytest.raises(crash.GleanNotReady, match="the profile of the launch is not known"):
        crash.wait_for_glean(None, sleep=clock.sleep, now=clock.now)
    assert clock.t == 0.0, "no profile, nothing to wait for"


def test_it_writes_nothing_in_the_profile(tmp_path):
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    clock = Clock()
    with pytest.raises(crash.GleanNotReady):
        crash.wait_for_glean(tmp_path, limit_s=2.0, sleep=clock.sleep, now=clock.now)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
