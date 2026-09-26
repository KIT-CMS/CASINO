from __future__ import annotations

from pathlib import Path

import awkward as ak
import uproot

DELPHES_TREE_NAME = "Delphes"


class DelphesReader:
    def __init__(self, path: str | Path, tree_name: str = DELPHES_TREE_NAME) -> None:
        # CASINO passes POSIX inputs as "file:<path>"; root:// URLs are opened via XRootD.
        self._file = uproot.open(str(path).removeprefix("file:"))
        self._tree = self._file[tree_name]

    def num_events(self) -> int:
        return int(self._tree.num_entries)

    def array(self, name: str) -> ak.Array:
        return self._tree[name].array()

    def maybe_array(self, name: str) -> ak.Array | None:
        return self._tree[name].array() if name in self._tree else None

    def close(self) -> None:
        self._file.close()
