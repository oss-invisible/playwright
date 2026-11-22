"""What the e2e tests of the font list need to know about the bundle and about the host, read from files and never typed.

Two sources, both plain data:

* the bundle manifest the core hands to the engine (``invisible_core._fpforge.profile.FONT_MANIFEST``, the text of
  ``bundle-fonts.list``: ``F|<family>`` opens a family, ``f|<file>|<index>|...|<psname>|<upem>|<ascent>|<descent>|...`` is one of its faces,
  ``A|<alias>|<target>`` is a Windows font-substitute). The engine prefers this text to the copy in its own directory, so it is the list of
  the families the engine answers to;
* the font files of the machine that runs the test (the host): the folders a system installs fonts in. Each file's ``name`` table gives the
  family, PostScript and full names of each of its faces. A host font whose names are not in the bundle is a "host-only" face: the engine
  must not know it, and a test that asks the page for it has a name that exists on this machine and in no bundle.

Only the header and the ``name`` table of a file are read (a font collection is several faces; ``simsun.ttc`` is 15 MB).
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

#: What an sfnt file starts with: TrueType 1.0, OpenType with CFF outlines, the Apple TrueType tag, a collection.
_SFNT = (b"\x00\x01\x00\x00", b"OTTO", b"true")
_COLLECTION = b"ttcf"
_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}

#: name ids of the strings that name a face.
FAMILY, FULL, POSTSCRIPT, TYPOGRAPHIC_FAMILY = 1, 4, 6, 16


class Manifest:
    """The bundle manifest, parsed: families in order, the faces of each (file, index, PostScript name, metrics) and the aliases."""

    def __init__(self, text: str):
        self.families: List[str] = []
        self.faces: Dict[str, List[dict]] = {}
        self.aliases: Dict[str, str] = {}
        current: Optional[str] = None
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("|")
            if fields[0] == "F" and len(fields) >= 2:
                current = fields[1]
                self.families.append(current)
                self.faces[current] = []
            elif fields[0] == "A" and len(fields) >= 3:
                self.aliases[fields[1]] = fields[2]
            elif fields[0] == "f" and current is not None and len(fields) >= 19:
                self.faces[current].append({
                    "file": fields[1], "index": int(fields[2]), "psname": fields[8],
                    "upem": int(fields[9]), "ascent": int(fields[10]), "descent": int(fields[11]),
                })

    def family_of_psname(self, psname: str) -> Optional[str]:
        for family, faces in self.faces.items():
            if any(face["psname"] == psname for face in faces):
                return family
        return None

    def face_with_psname(self, psname: str) -> dict:
        for faces in self.faces.values():
            for face in faces:
                if face["psname"] == psname:
                    return face
        raise AssertionError("the bundle manifest has no face with the PostScript name %r" % psname)

    def known_names(self) -> Set[str]:
        """Every name the bundle answers to, lower case: its families, its aliases and the PostScript names of its faces."""
        names = {family.lower() for family in self.families}
        names |= {alias.lower() for alias in self.aliases}
        names |= {face["psname"].lower() for faces in self.faces.values() for face in faces}
        return names


def bundle_manifest() -> Manifest:
    """The manifest of the core installed with the wrapper under test, the one the engine is handed as ``zoom.stealth.fonts.manifest``."""
    from invisible_core._fpforge.profile import FONT_MANIFEST

    manifest = Manifest(FONT_MANIFEST)
    assert manifest.families, "the manifest of the core has no family: the parser read nothing"
    return manifest


def _decode(platform: int, encoding: int, raw: bytes) -> Optional[str]:
    try:
        if platform in (0, 3):
            return raw.decode("utf-16-be")
        if platform == 1 and encoding == 0:
            return raw.decode("mac_roman")
    except UnicodeDecodeError:
        return None
    return None


def _name_strings(table: bytes) -> Dict[int, List[tuple]]:
    """name id -> [(is english windows name, text)] of one ``name`` table."""
    out: Dict[int, List[tuple]] = {}
    if len(table) < 6:
        return out
    _format, count, string_offset = struct.unpack(">HHH", table[:6])
    for i in range(count):
        record = table[6 + 12 * i: 18 + 12 * i]
        if len(record) < 12:
            break
        platform, encoding, language, name_id, length, offset = struct.unpack(">6H", record)
        text = _decode(platform, encoding, table[string_offset + offset: string_offset + offset + length])
        if text:
            out.setdefault(name_id, []).append((platform == 3 and language == 0x409, text))
    return out


def face_names(path: Path) -> List[dict]:
    """For each face of a font file: ``family`` (every family string, any language), ``psname`` and ``full`` (the English ones, or None).

    An unreadable file, or a file that is not an sfnt, has no faces: a host folder holds other things than fonts."""
    faces: List[dict] = []
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
            if len(head) < 12:
                return faces
            if head[:4] == _COLLECTION:
                count = struct.unpack(">I", head[8:12])[0]
                if count > 64:
                    return faces
                offsets = struct.unpack(">%dI" % count, handle.read(4 * count))
            elif head[:4] in _SFNT:
                offsets = (0,)
            else:
                return faces
            for base in offsets:
                handle.seek(base + 4)
                tables = struct.unpack(">H", handle.read(2))[0]
                handle.seek(base + 12)
                directory = handle.read(16 * tables)
                name_table = None
                for i in range(tables):
                    tag, _checksum, offset, length = struct.unpack(">4sIII", directory[16 * i: 16 * i + 16])
                    if tag == b"name" and 0 < length <= 1 << 20:
                        handle.seek(offset)
                        name_table = handle.read(length)
                        break
                if name_table is None:
                    continue
                strings = _name_strings(name_table)

                def english(name_id: int) -> Optional[str]:
                    for is_english, text in strings.get(name_id, []):
                        if is_english:
                            return text
                    return None

                families = []
                for name_id in (FAMILY, TYPOGRAPHIC_FAMILY):
                    for _english, text in strings.get(name_id, []):
                        if text not in families:
                            families.append(text)
                faces.append({"file": str(path), "families": families, "psname": english(POSTSCRIPT), "full": english(FULL)})
    except (OSError, struct.error):
        return []
    return faces


def host_font_files() -> List[Path]:
    """The font files the host has installed, in a stable order."""
    if sys.platform == "win32":
        roots = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    else:
        roots = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
                 Path.home() / ".local" / "share" / "fonts"]
    files: List[Path] = []
    for root in roots:
        if root.is_dir():
            files += [p for p in root.rglob("*") if p.suffix.lower() in _EXTENSIONS and p.is_file()]
    return sorted(files, key=lambda p: str(p).lower())


def _plain(name: Optional[str]) -> bool:
    """A name that goes into a CSS string between double quotes without an escape: printable ASCII, no quote, no backslash."""
    return bool(name) and all(32 <= ord(ch) < 127 and ch not in "\"\\" for ch in name)


def bundle_file_names(manifest: Manifest, binary: str) -> Set[str]:
    """Every name (families of any language, PostScript, full), lower case, that the files of the bundle carry in their own ``name`` tables.

    The engine answers to the other-language family names of a bundled face as it does to its family (a localized name is an alias of the
    face it names), so a host font that carries such a name is not host-only. The folder is ``<engine>/fonts``; when the build has no such
    folder the set is empty and the manifest alone decides."""
    folder = Path(binary).resolve().parent / "fonts"
    names: Set[str] = set()
    if not folder.is_dir():
        return names
    for name in sorted({face["file"] for faces in manifest.faces.values() for face in faces}):
        for face in face_names(folder / name):
            names |= {family.lower() for family in face["families"]}
            names |= {value.lower() for value in (face["psname"], face["full"]) if value}
    return names


def host_only_faces(manifest: Manifest, extra_known: Iterable[str] = ()) -> List[dict]:
    """The faces of the host whose names the bundle does not know: no family, no alias and no PostScript name of the bundle."""
    known = manifest.known_names() | set(extra_known)
    out: List[dict] = []
    for path in host_font_files():
        for face in face_names(path):
            names = [name.lower() for name in face["families"]]
            if face["psname"]:
                names.append(face["psname"].lower())
            if names and not any(name in known for name in names):
                out.append(face)
    return out


def host_only_families(manifest: Manifest, extra_known: Iterable[str] = ()) -> List[str]:
    """The family names (every language) of the host-only faces, once each, in the order of the files."""
    extra_known = set(extra_known)
    known = manifest.known_names() | extra_known
    seen: Set[str] = set()
    out: List[str] = []
    for face in host_only_faces(manifest, extra_known):
        for family in face["families"]:
            key = family.lower()
            if key not in seen and key not in known and _plain(family):
                seen.add(key)
                out.append(family)
    return out


def a_host_only_face(manifest: Manifest, prefer: Iterable[str] = (), extra_known: Iterable[str] = ()) -> dict:
    """One host-only face with a PostScript name and a full name a page can write: the first of the ``prefer`` families the host has, else the first.

    The face is what a ``src: local()`` is given: its names exist on this machine (the file is on its disk) and in no bundle."""
    faces = [f for f in host_only_faces(manifest, extra_known) if _plain(f["psname"]) and _plain(f["full"])]
    assert faces, "the host has no font whose names are not the bundle's: there is nothing to ask the engine for that only the host knows"
    for wanted in prefer:
        for face in faces:
            if wanted.lower() in (family.lower() for family in face["families"]):
                return face
    return faces[0]
