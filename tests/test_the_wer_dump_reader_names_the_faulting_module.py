"""scripts/ci_wer_dumps.py reads the header of a Windows Error Reporting dump and names the thread, the exception and the module that holds the faulting address.

A minidump is built here byte by byte (MINIDUMP_HEADER, a directory of a module list and an exception stream, MINIDUMP_MODULE records with their UTF-16 names,
MINIDUMP_EXCEPTION_STREAM; layouts: https://learn.microsoft.com/windows/win32/api/minidumpapiset/), so no Windows and no debugger is needed:

* an access violation inside the second module is reported with its code name, the thread id, ``module+offset`` and the failed access (a write of an address);
* an address in no module says so; a dump with no exception stream says it was taken on request;
* a file that is not a minidump and an empty folder do not raise: the step must never fail the job, and no dump is the normal case;
* the summary lists every dump with its size and is written to ``summary.txt`` beside them.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_wer_dumps  # noqa: E402

pytestmark = pytest.mark.unit

PYTHON_BASE, PYTHON_SIZE = 0x00007FF700000000, 0x20000
XUL_BASE, XUL_SIZE = 0x00007FFA10000000, 0x5000000


def _minidump(address: int | None, code: int = 0xC0000005, thread: int = 4242, params=(1, 0xDEAD0010)) -> bytes:
    """A minidump with two modules and, when ``address`` is not None, an exception stream."""
    names = ["C:\\hostedtoolcache\\python.exe", "D:\\a\\_temp\\firefox\\xul.dll"]
    mods = [(PYTHON_BASE, PYTHON_SIZE), (XUL_BASE, XUL_SIZE)]
    n_streams = 2 if address is not None else 1
    dir_at = 32
    module_list_at = dir_at + 12 * n_streams
    module_list_size = 4 + 108 * len(mods)
    exception_at = module_list_at + module_list_size
    exception_size = 8 + 32 + 15 * 8 + 8
    strings_at = exception_at + (exception_size if address is not None else 0)
    blobs, rvas, at = [], [], strings_at
    for n in names:
        raw = n.encode("utf-16-le")
        blob = struct.pack("<I", len(raw)) + raw
        rvas.append(at)
        blobs.append(blob)
        at += len(blob)
    out = struct.pack("<4sIIIIIQ", b"MDMP", 0xA793, n_streams, dir_at, 0, 0, 0x2)  # signature, version, streams, directory rva, checksum, time, flags
    out += struct.pack("<III", 4, module_list_size, module_list_at)
    if address is not None:
        out += struct.pack("<III", 6, exception_size, exception_at)
    out += struct.pack("<I", len(mods))
    for (base, size), rva in zip(mods, rvas):
        out += struct.pack("<QIIII", base, size, 0, 0, rva) + b"\0" * (108 - 24)
    if address is not None:
        padded = list(params) + [0] * (15 - len(params))
        out += struct.pack("<IIIIQQII", thread, 0, code, 0, 0, address, len(params), 0) + struct.pack("<15Q", *padded) + struct.pack("<II", 0, 0)
    for b in blobs:
        out += b
    return out


def test_an_access_violation_inside_a_module_is_named_with_its_offset(tmp_path):
    dump = tmp_path / "python.exe.1234.dmp"
    dump.write_bytes(_minidump(XUL_BASE + 0x1234))
    d = ci_wer_dumps.read_dump(dump)
    assert (d["thread_id"], d["code"], d["address"]) == (4242, 0xC0000005, XUL_BASE + 0x1234)
    assert d["module"].endswith("xul.dll") and d["offset"] == 0x1234 and d["module_count"] == 2
    text = ci_wer_dumps.describe(dump)
    assert "EXCEPTION_ACCESS_VIOLATION on thread 4242" in text
    assert "xul.dll+0x1234" in text
    assert "failed access: write of 0x00000000DEAD0010" in text


def test_the_first_module_is_found_too_and_an_address_in_none_says_so(tmp_path):
    first = tmp_path / "a.dmp"
    first.write_bytes(_minidump(PYTHON_BASE + 0x10))
    assert ci_wer_dumps.read_dump(first)["module"].endswith("python.exe")
    nowhere = tmp_path / "b.dmp"
    nowhere.write_bytes(_minidump(0x1000))
    assert "module" not in ci_wer_dumps.read_dump(nowhere)
    assert "no module of the dump" in ci_wer_dumps.describe(nowhere)


def test_a_dump_with_no_exception_stream_says_it_was_taken_on_request(tmp_path):
    dump = tmp_path / "c.dmp"
    dump.write_bytes(_minidump(None))
    assert "no exception stream" in ci_wer_dumps.describe(dump)


def test_a_file_that_is_not_a_minidump_does_not_raise(tmp_path):
    junk = tmp_path / "d.dmp"
    junk.write_bytes(b"not a dump" * 10)
    assert "unreadable header" in ci_wer_dumps.describe(junk)
    short = tmp_path / "e.dmp"
    short.write_bytes(b"MDMP")
    assert "unreadable header" in ci_wer_dumps.describe(short)


def test_the_summary_lists_every_dump_with_its_size_and_is_saved_beside_them(tmp_path, capsys):
    (tmp_path / "python.exe.1.dmp").write_bytes(_minidump(XUL_BASE + 8))
    (tmp_path / "python.exe.2.dmp").write_bytes(_minidump(XUL_BASE + 16))
    assert ci_wer_dumps.main(["x", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert ": 2\n" in out and "python.exe.1.dmp" in out and "python.exe.2.dmp" in out and "xul.dll+0x10" in out
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8") == out


def test_no_dump_and_no_folder_are_the_normal_case(tmp_path, capsys):
    assert ci_wer_dumps.main(["x", str(tmp_path)]) == 0
    assert ": 0\n" in capsys.readouterr().out
    assert ci_wer_dumps.main(["x", str(tmp_path / "missing")]) == 0
    assert not (tmp_path / "missing").exists()
