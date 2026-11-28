r"""Read the full user-mode dumps Windows Error Reporting left for a python.exe that died, and say what each one holds (CI evidence, no debugger needed).

The Windows e2e jobs set ``HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\python.exe`` (full dump, folder ``<evidence>/dumps/python``)
before their tests; when a pytest-xdist worker dies from an unhandled exception ("node down: Not properly terminated", "Windows fatal exception: access violation")
WER writes ``python.exe.<pid>.dmp`` there and the evidence artifact carries it. This script runs after the tests (``if: always()``): it lists every dump with its size
and reads the header of the MINIDUMP file, with ``struct`` only, to name what a debugger would name first: the thread that raised, the exception code, the faulting
address and the module that holds it (with the offset in it), plus the access that failed for an access violation. The summary goes to stdout and to
``summary.txt`` beside the dumps. It never fails the job: no dump is the normal case.

Format of the file: https://learn.microsoft.com/windows/win32/api/minidumpapiset/ (MINIDUMP_HEADER, MINIDUMP_DIRECTORY, MINIDUMP_EXCEPTION_STREAM,
MINIDUMP_MODULE). Dump types and the LocalDumps key: https://learn.microsoft.com/windows/win32/wer/collecting-user-mode-dumps
"""
from __future__ import annotations

import shutil
import struct
import sys
import time
from pathlib import Path

SIGNATURE = b"MDMP"
MODULE_LIST_STREAM = 4
EXCEPTION_STREAM = 6
MODULE_SIZE = 108  # sizeof(MINIDUMP_MODULE)

EXCEPTION_NAMES = {
    0xC0000005: "EXCEPTION_ACCESS_VIOLATION",
    0xC0000374: "STATUS_HEAP_CORRUPTION",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN (fail fast)",
    0xC00000FD: "EXCEPTION_STACK_OVERFLOW",
    0xC000001D: "EXCEPTION_ILLEGAL_INSTRUCTION",
    0xC0000094: "EXCEPTION_INT_DIVIDE_BY_ZERO",
    0xC0000026: "STATUS_INVALID_DISPOSITION",
    0xC0000096: "EXCEPTION_PRIV_INSTRUCTION",
    0xC0000006: "EXCEPTION_IN_PAGE_ERROR",
    0x80000003: "EXCEPTION_BREAKPOINT",
    0xE06D7363: "C++ exception",
}
ACCESS = {0: "read", 1: "write", 8: "execute (DEP)"}


def read_dump(path: Path) -> dict:
    """The facts of one minidump: thread id, exception code and address, the module holding the address. Raises ValueError on a file that is not a minidump."""
    with open(path, "rb") as f:
        head = f.read(32)
        if len(head) < 32 or head[:4] != SIGNATURE:
            raise ValueError("not a minidump (no MDMP signature)")
        _sig, _version, n_streams, dir_rva = struct.unpack_from("<4sIII", head, 0)
        f.seek(dir_rva)
        directory = [struct.unpack("<III", f.read(12)) for _ in range(n_streams)]
        out: dict = {"streams": n_streams}
        modules: list[tuple[int, int, str]] = []
        for kind, size, rva in directory:
            if kind == MODULE_LIST_STREAM:
                f.seek(rva)
                (count,) = struct.unpack("<I", f.read(4))
                records = f.read(count * MODULE_SIZE)
                for i in range(count):
                    base, image_size, _cs, _ts, name_rva = struct.unpack_from("<QIIII", records, i * MODULE_SIZE)
                    here = f.tell()
                    f.seek(name_rva)
                    (nbytes,) = struct.unpack("<I", f.read(4))
                    name = f.read(nbytes).decode("utf-16-le", "replace")
                    f.seek(here)
                    modules.append((base, image_size, name))
            elif kind == EXCEPTION_STREAM:
                f.seek(rva)
                raw = f.read(8 + 8 + 8 + 8 + 8 + 15 * 8)
                tid, _pad, code, flags, _chain, address, n_params, _pad2 = struct.unpack_from("<IIIIQQII", raw, 0)
                params = struct.unpack_from("<15Q", raw, 8 + 32)
                out.update(thread_id=tid, code=code, flags=flags, address=address, params=list(params[:n_params]))
        out["module_count"] = len(modules)
        if "address" in out:
            for base, image_size, name in modules:
                if base <= out["address"] < base + image_size:
                    out["module"] = name
                    out["offset"] = out["address"] - base
                    break
    return out


def describe(path: Path) -> str:
    size = path.stat().st_size
    head = "%s: %.1f MB" % (path.name, size / 1e6)
    try:
        d = read_dump(path)
    except (ValueError, OSError, struct.error) as e:
        return "%s, unreadable header: %s" % (head, e)
    if "code" not in d:
        return "%s, %d modules, no exception stream (a dump taken on request, not on a fault)" % (head, d["module_count"])
    name = EXCEPTION_NAMES.get(d["code"], "unknown")
    lines = [
        head,
        "  exception 0x%08X %s on thread %d" % (d["code"], name, d["thread_id"]),
        "  at 0x%016X in %s" % (d["address"], ("%s+0x%X" % (d["module"], d["offset"])) if "module" in d else "no module of the dump (JIT or freed code)"),
    ]
    p = d["params"]
    if d["code"] in (0xC0000005, 0xC0000006) and len(p) >= 2:
        lines.append("  failed access: %s of 0x%016X" % (ACCESS.get(p[0], str(p[0])), p[1]))
    return "\n".join(lines)


def summarize(folder: Path) -> str:
    dumps = sorted(folder.glob("*.dmp")) if folder.is_dir() else []
    cdb = shutil.which("cdb") or shutil.which("windbg")
    lines = ["WER dumps of python.exe in %s: %d" % (folder, len(dumps)), "debugger on the runner: %s" % (cdb or "none (the header reader above is all there is)")]
    for p in dumps:
        lines.append(describe(p) + "\n  written %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(p.stat().st_mtime)))
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    folder = Path(argv[1]) if len(argv) > 1 else Path(".")
    text = summarize(folder)
    sys.stdout.write(text)
    if folder.is_dir():
        (folder / "summary.txt").write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
