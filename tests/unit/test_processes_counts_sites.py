"""The e2e tests that look at processes ask for the processes of a site by its name. No browser.

With `fission.processSiteNames` on, a content process that hosts a site is named by that site, and on Linux the name
is cut at 15 characters. The browser starts processes of its own (the pool refill, a process for a Mozilla site
started by the New Tab page), so a test that compared two counts of processes read them as the effect of the frame
under test (test_same_site_other_port_stays_in_process, red in 14 of 30 runs of the e2e suite). The names below are
the ones measured on firefox-62 (Linux), with command lines as /proc gives them.
"""
import calendar
import pathlib
import time

import _processes
import pytest

pytestmark = pytest.mark.unit

ENGINE = "/cache/firefox-62/firefox"
DIR = "/cache/firefox-62"
A, B = "http://a.test", "http://b.test"


class _Proc:
    def __init__(self, pid, name, kind="tab", exe=ENGINE):
        self.pid, self._name, self._exe = pid, name, exe
        self._cmd = [exe, "-contentproc", "-isForBrowser", "-prefsLen", "1", "-parentBuildID", "20261111073531",
                     "-appDir", DIR + "/browser", str(pid), kind]

    def exe(self):
        return self._exe

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd


def _engine_at_a():
    """a.test shown: its process, the one the New Tab page started, and the engine's other children."""
    return [_Proc(184, "Socket Process", "socket"), _Proc(200, "WebExtensions"), _Proc(334, "http://a.test^u"),
            _Proc(377, "Privileged Cont"), _Proc(410, "mozgcp.net"), _Proc(433, "Web Content"),
            _Proc(438, "Web Content")]


def test_a_process_named_by_the_site_is_the_sites():
    assert _processes.site_pids(_engine_at_a(), A, DIR) == {334}


@pytest.mark.parametrize("name", ["mozgcp.net", "Web Content", "Privileged Cont", "WebExtensions", "Isolated Web Co"])
def test_a_process_with_another_name_is_not_the_sites(name):
    assert _processes.site_pids([_Proc(1, name)], A) == set()


def test_the_process_of_another_site_is_not_the_sites():
    assert _processes.site_pids([_Proc(1, "http://b.test^u")], A) == set()
    assert _processes.site_pids([_Proc(1, "http://b.test^u")], B) == {1}


def test_the_process_the_browser_starts_for_itself_does_not_move_the_sites():
    """The measured flaw: a second isolated process (mozgcp.net) arriving between two reads changed the old count."""
    before = _processes.site_pids(_engine_at_a(), A, DIR)
    after = _processes.site_pids(_engine_at_a() + [_Proc(549, "mozgcp.net"), _Proc(550, "Web Content")], A, DIR)
    assert before == after == {334}


def test_https_is_dropped_from_the_site():
    assert _processes.name_of_site("https://example.com") == "example.com"
    assert _processes.hosts_site("example.com", "https://example.com")
    assert _processes.hosts_site("example.com^use", "https://example.com")


def test_a_site_longer_than_the_name_matches_on_its_first_15_characters():
    site = "http://a-very-long-site.example"
    assert _processes.name_of_site(site) == "http://a-very-l"
    assert _processes.hosts_site("http://a-very-l", site)
    assert not _processes.hosts_site("http://a-very-x", site)


def test_a_site_that_starts_like_another_is_not_the_other():
    assert not _processes.hosts_site("http://a.testin", A)
    assert not _processes.hosts_site("http://a.testing.example", A)
    assert _processes.hosts_site("http://a.test^u", A)


def test_a_process_of_another_firefox_is_not_counted():
    other = _Proc(900, "http://a.test^u", exe="/usr/lib/firefox/firefox")
    assert _processes.site_pids(_engine_at_a() + [other], A, DIR) == {334}


def test_a_process_that_is_not_a_tab_is_not_counted():
    assert _processes.site_pids([_Proc(1, "http://a.test^u", kind="gpu")], A) == set()


# --- The type a process was given is read for the process INSTANCE, not for the pid alone ---------------------------
#
# Where the system names no process (Windows) the e2e tests that look at sites read the engine's own log (`Setting
# remoteType of process <pid> to <type>`, in the parent's file and in one `base.child-N` file per child). A plain
# `{pid: last type}` read the files in the order of their names (child-10 before child-2), and the system gives the
# number of an ended process to a later one, so a process of another kind with the pid of a dead site process was read
# as that site and a wait for "no process of the site is left" never became true. The lines carry their time (`MOZ_LOG`
# with `timestamp`): they are ordered by it, and a process takes only the types logged at or after its own creation.

T0 = calendar.timegm((2026, 11, 22, 10, 0, 0))


def _line(seconds, pid, remote_type, kind="Setting remoteType of process {pid} to {type}"):
    stamp = T0 + seconds
    whole = int(stamp)
    micro = int(round((stamp - whole) * 1e6))
    text = "%04d-%02d-%02d %02d:%02d:%02d.%06d UTC - [Child %d: Main Thread]: D/Process " % (
        *time.gmtime(whole)[:6], micro, pid)
    return text + kind.format(pid=pid, type=remote_type) + "\n"


def _changed(seconds, pid, remote_type):
    return _line(seconds, pid, remote_type, "Changing remoteType of process {pid} from prealloc to {type}")


def _logs(tmp_path, files):
    base = tmp_path / "engine.log"
    for suffix, lines in files.items():
        pathlib.Path(str(base) + suffix).write_text("".join(lines), encoding="utf-8")
    return base


class _LoggedProc:
    def __init__(self, pid, created):
        self.pid, self._created = pid, created

    def create_time(self):
        return T0 + self._created

    def exe(self):
        return "C:/engine/firefox.exe"

    def name(self):
        return "firefox.exe"

    def cmdline(self):
        return ["C:/engine/firefox.exe", "-contentproc", "-isForBrowser", str(self.pid), "tab"]


def test_the_lines_of_a_pid_are_read_in_the_order_they_were_logged_not_in_the_order_of_the_file_names(tmp_path):
    """child-2 holds the first line of pid 40 (a spare process), child-10 the last (its site): sorted by name child-10
    comes first and the spare type would win."""
    base = _logs(tmp_path, {
        ".child-2": [_line(1, 40, "prealloc")],
        ".child-10": [_changed(2, 40, "webIsolated=http://a.test^userContextId=1")],
    })
    types = _processes.remote_types(base)
    assert [t for _stamp, t in types[40]] == ["prealloc", "webIsolated=http://a.test^userContextId=1"]
    assert _processes.type_of_process(types, 40, T0) == "webIsolated=http://a.test^userContextId=1"


def test_the_child_files_are_ordered_by_their_number():
    names = [pathlib.Path("e.log" + s) for s in (".child-10", ".child-2", "", ".child-1")]
    assert [p.name for p in sorted(names, key=_processes._natural)] == ["e.log", "e.log.child-1", "e.log.child-2", "e.log.child-10"]


def test_a_pid_reused_by_a_later_process_does_not_inherit_the_type_of_the_old_one(tmp_path):
    """The known-bad case: pid 40 hosted a.test and ended; the system gave 40 to a process that has not logged its type
    yet. It is not a.test's, and `none_hosted` waits could never be true."""
    base = _logs(tmp_path, {".child-3": [_line(1, 40, "webIsolated=http://a.test^userContextId=1")]})
    types = _processes.remote_types(base)
    assert _processes.type_of_process(types, 40, T0 + 1 - 0.5) == "webIsolated=http://a.test^userContextId=1"  # the old one
    assert _processes.type_of_process(types, 40, T0 + 30) == ""  # born 29 s after the old type was logged
    old, reused = _LoggedProc(40, 0.5), _LoggedProc(40, 30)
    assert _processes.site_pids_by_type([old], A, types) == {40}
    assert _processes.site_pids_by_type([reused], A, types) == set()


@pytest.mark.parametrize("early_file, late_file", [(".child-9", ".child-10"), (".child-10", ".child-2")])
def test_a_reused_pid_takes_the_type_of_its_own_process_whichever_file_holds_it(tmp_path, early_file, late_file):
    """pid 40 was a.test (early), then the system gave 40 to a process of b.test (late): the process alive now, born
    late, is b.test's, whatever the order of the files (the second case reads the late line before the early one)."""
    base = _logs(tmp_path, {
        early_file: [_line(1, 40, "webIsolated=http://a.test^userContextId=1")],
        late_file: [_line(20, 40, "webIsolated=http://b.test^userContextId=1")],
    })
    types = _processes.remote_types(base)
    now = _LoggedProc(40, 19.5)
    assert _processes.site_pids_by_type([now], B, types) == {40}
    assert _processes.site_pids_by_type([now], A, types) == set()


def test_a_process_born_just_before_its_first_line_is_stamped_still_has_it():
    types = {7: [(T0 + 10, "webIsolated=http://a.test^userContextId=1")]}
    assert _processes.type_of_process(types, 7, T0 + 10 + _processes.BIRTH_SLACK / 2) != ""
    assert _processes.type_of_process(types, 7, T0 + 10 + _processes.BIRTH_SLACK * 4) == ""


def test_a_process_with_no_log_line_has_no_type(tmp_path):
    base = _logs(tmp_path, {".child-1": [_line(1, 5, "web")]})
    assert _processes.type_of_process(_processes.remote_types(base), 99, T0) == ""
