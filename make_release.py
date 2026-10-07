#!/usr/bin/env python3
"""Builds release/mousehelper-<version>.zip: the eXo patch and the helper in
one folder (patch_exo.py finds the helper files next to itself)."""
import pathlib
import re
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
FILES = ["exo/patch_exo.py", "mousehelper.py", "guestcode.py", "README.md",
         "NOTES.md", "LICENSE"]


def main():
    version = re.search(r'^VERSION = "([^"]+)"',
                        (HERE / "mousehelper.py").read_text(), re.M).group(1)
    top = f"mousehelper-{version}"
    out = HERE / "release" / f"{top}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            z.write(HERE / name, f"{top}/{pathlib.Path(name).name}")
    print(f"{out}: {out.stat().st_size} bytes, {len(FILES)} files")


if __name__ == "__main__":
    main()
