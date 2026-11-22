"""``FailureEvidence`` that also keeps a folder of extra files (the engine's own log of the launch, a lab server's record, a page's timeline).

The test writes whatever it wants to keep into ``evidence.files`` while it runs. When the body fails, the folder is copied next to the
rest of the evidence (``<evidence folder>/files/``) and ``meta.json`` lists it; when the body passes, it is deleted with the rest.
Nothing else differs from ``tests/_evidence_on_failure.py``.
"""
from __future__ import annotations

import json
import shutil

from _evidence_on_failure import FailureEvidence


class FailureEvidenceWithFiles(FailureEvidence):
    def __init__(self, name: str):
        super().__init__(name)
        #: Under the helper's own temporary folder: removed with it when the body passes.
        self.files = self.work / "files"
        self.files.mkdir(parents=True, exist_ok=True)

    def _save(self, exc_type, exc) -> None:
        super()._save(exc_type, exc)
        if self.saved_to is None or not self.files.exists():
            return
        shutil.copytree(self.files, self.saved_to / "files", dirs_exist_ok=True)
        meta_path = self.saved_to / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["files"] = sorted(p.name for p in self.saved_to.iterdir())
        meta["extra_files"] = sorted(str(p.relative_to(self.files)).replace("\\", "/") for p in self.files.rglob("*") if p.is_file())
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
