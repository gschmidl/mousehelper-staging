#!/usr/bin/env python3
"""Puts the platform-independent mouse helpers into an eXoDOS installation.

The launchers of WarCraft, Dune II, SkyNET and The Terminator: Future Shock
(eXoDOS\\!dos\\...\\exception.bat, the German and Spanish ones too) then run the
port in util\\mousehelper instead of the AutoHotkey helpers, in DOSBox Staging
0.83 or newer.

- util\\mousehelper: mousehelper.py, guestcode.py, README.md, NOTES.md.
- Launchers: the line that starts an AutoHotkey helper starts the port with
  Python ("py") when there is one, with the given DOSBox Staging; the original
  line stays for when there is not. The port uses the launcher's own DOSBox
  if that is Staging 0.83+ (eXo's alternate launcher) and the given Staging
  otherwise. SkyNET's and Future Shock's launchers run the helper with eXo's
  Staging options as well (they used to skip it there). exception.bat.orig
  keeps each launcher as it was.
- The installed games' run.bat: the note that the helper is not fully
  compatible with DOSBox Staging goes (run.bat.orig keeps it). Games installed
  later get it from their zip.

usage: python patch_exo.py [--check | --revert] [--staging FOLDER] EXO_FOLDER

EXO_FOLDER is eXoDOS's "eXo" folder, the one with eXoDOS, emulators and util in
it. --check shows what would change, --revert undoes it. --staging is the
DOSBox Staging folder, relative to the eXo folder (default:
emulators\\dosbox\\staging0.83.0); it has to be Staging 0.83 or newer, which has
the HTTP API the helper uses. Run again with another --staging to switch.
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ('mousehelper.py', 'guestcode.py', 'README.md', 'NOTES.md')
DEST = Path('util') / 'mousehelper'
GAMES = {'warcraft': 'warcraft', 'dune2': 'dune2', 'skynet': 'skynet'}

AHK = re.compile(r'^(?P<indent>\s*)(?P<ahk>\S*\\(?P<name>Warcraft|Dune2|SkyNET)MouseHelper\.exe)'
                 r'\s+(?P<rest>.+?)\s*$', re.I)
OURS = 'py .\\util\\mousehelper\\mousehelper.py'
ALT_SKIP = 'if [%conf%]==[] goto main'
NOTE_OLD = ('echo game which adds a few nice features. This tool is not fully compatible',
            'echo with Dosbox Staging resulting in only some of the features working when',
            "echo the game is launched using the 'Pixel Perfect & Shaders' Launchbox option.")
NOTE_NEW = ('echo game which adds a few nice features.',)


def block(indent, game, exe, ahk, rest, original):
    """What replaces an AutoHotkey helper's line."""
    return [
        f'{indent}where py >nul 2>nul',
        f'{indent}if errorlevel 1 goto mousehelper_ahk',
        f'{indent}if not exist .\\util\\mousehelper\\mousehelper.py goto mousehelper_ahk',
        f'{indent}{OURS} --game {game} --staging {exe} --fallback {ahk} -- {rest}',
        f'{indent}goto mousehelper_done',
        ':mousehelper_ahk',
        original,
        ':mousehelper_done',
    ]


def convert(text, exe):
    """The launcher with its AutoHotkey helper line switched to the port
    (exe: the quoted Staging dosbox.exe path); None if it has none."""
    nl = '\r\n' if '\r\n' in text else '\n'
    out, found = [], False
    for line in text.split(nl):
        m = AHK.match(line)
        if m:
            found = True
            game = GAMES[m.group('name').lower()]
            out += block(m.group('indent'), game, exe, m.group('ahk'), m.group('rest'), line)
        elif line.strip().lower() == ALT_SKIP:
            out.append(line[:len(line) - len(line.lstrip())] + 'goto main')
        else:
            out.append(line)
    return nl.join(out) if found else None


def fix_note(text):
    nl = '\r\n' if '\r\n' in text else '\n'
    lines = text.split(nl)
    i = 0
    while i <= len(lines) - len(NOTE_OLD):
        if tuple(ln.strip() for ln in lines[i:i + len(NOTE_OLD)]) == NOTE_OLD:
            lines[i:i + len(NOTE_OLD)] = list(NOTE_NEW)
        i += 1
    return nl.join(lines)


def same(a, b):
    """Equal apart from line ends."""
    return a.replace(b'\r\n', b'\n') == b.replace(b'\r\n', b'\n')


def has_webserver(exe):
    """DOSBox Staging 0.83+ (the webserver's settings are in the program)."""
    tail = b''
    with open(exe, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                return False
            if b'webserver_enabled' in tail + chunk:
                return True
            tail = chunk[-32:]


class Patcher:
    def __init__(self, exo, staging, check):
        self.exo, self.check, self.changes = exo, check, 0
        self.staging = (exo / staging).resolve()
        try:
            rel = str(self.staging.relative_to(exo))
            self.exe = f'".\\{rel}\\dosbox.exe"'   # launchers run from the eXo folder
        except ValueError:
            self.exe = f'"{self.staging}\\dosbox.exe"'

    def say(self, text):
        print(('would ' if self.check else '') + text)
        self.changes += 1

    def rel(self, p):
        try:
            return str(p.relative_to(self.exo))
        except ValueError:
            return str(p)

    def launchers(self):
        """(exception.bat, its .orig, current text, original text) of the
        launchers that start an AutoHotkey mouse helper.  A patched one keeps
        the helper's line for when Python is missing; an unpatched one (also
        one an eXo update put back) is its own original."""
        root = self.exo / 'eXoDOS' / '!dos'
        for bat in sorted(root.rglob('exception.bat'), key=lambda p: str(p).lower()):
            orig = bat.with_name('exception.bat.orig')
            text = bat.read_bytes().decode('latin-1')
            if OURS in text:
                if not orig.is_file():
                    print(f'warning: {self.rel(bat)} was patched but has no {orig.name}; left alone')
                    continue
                yield bat, orig, text, orig.read_bytes().decode('latin-1')
            elif any(AHK.match(line) for line in text.splitlines()):
                yield bat, orig, text, text

    def game_folders(self, text):
        """eXoDOS\\<game> folders of the AutoHotkey helpers a launcher names."""
        for line in text.splitlines():
            m = AHK.match(line)
            if m:
                yield (self.exo / m.group('ahk').lstrip('.\\')).parent

    def write(self, path, data):
        if path.is_file() and same(path.read_bytes(), data):
            return
        self.say(f'write {self.rel(path)}')
        if not self.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def apply(self):
        for name in FILES:
            src = HERE.parent / name if (HERE.parent / name).is_file() else HERE / name
            self.write(self.exo / DEST / name, src.read_bytes())
        for bat, orig, text, original in self.launchers():
            new = convert(original, self.exe)
            if new != text:
                self.say(f'switch {self.rel(bat)} to the helper'
                         + ('' if OURS in text else f' (old one kept as {orig.name})'))
                if not self.check:
                    if OURS not in text:
                        orig.write_bytes(text.encode('latin-1'))
                    bat.write_bytes(new.encode('latin-1'))
            for folder in self.game_folders(original):
                run = folder / 'run.bat'
                if not run.is_file():
                    continue
                old = run.read_bytes().decode('latin-1')
                new = fix_note(old)
                if new == old:
                    continue
                self.say(f'drop the "not fully compatible with Dosbox Staging" note from '
                         f'{self.rel(run)} (old one kept as run.bat.orig)')
                if not self.check:
                    run.with_name('run.bat.orig').write_bytes(old.encode('latin-1'))
                    run.write_bytes(new.encode('latin-1'))

    def revert(self):
        for bat, orig, text, original in self.launchers():
            if OURS in text:
                self.say(f'restore {self.rel(bat)}')
                if not self.check:
                    bat.write_bytes(orig.read_bytes())
                    orig.unlink()
            for folder in self.game_folders(original):
                run, ro = folder / 'run.bat', folder / 'run.bat.orig'
                if not ro.is_file() or not run.is_file():
                    continue
                if run.read_bytes().decode('latin-1') != fix_note(ro.read_bytes().decode('latin-1')):
                    print(f'warning: {self.rel(run)} changed since it was patched; left alone')
                    continue
                self.say(f'restore {self.rel(run)}')
                if not self.check:
                    run.write_bytes(ro.read_bytes())
                    ro.unlink()
        dest = self.exo / DEST
        for name in FILES:
            if (dest / name).is_file():
                self.say(f'remove {self.rel(dest / name)}')
                if not self.check:
                    (dest / name).unlink()
        if dest.is_dir() and not self.check:
            cache = dest / '__pycache__'
            if cache.is_dir():
                shutil.rmtree(cache)
            if not any(dest.iterdir()):
                dest.rmdir()
            else:
                print(f'kept {self.rel(dest)} (settings: mousehelper.json)')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('exo', help="eXoDOS's eXo folder (with eXoDOS, emulators and util in it)")
    ap.add_argument('--check', action='store_true', help='only show what would change')
    ap.add_argument('--revert', action='store_true', help='undo the changes')
    ap.add_argument('--staging', default='emulators\\dosbox\\staging0.83.0',
                    help='the DOSBox Staging folder, relative to the eXo folder (default: %(default)s)')
    a = ap.parse_args()
    exo = Path(a.exo).resolve()
    if not (exo / 'eXoDOS' / '!dos').is_dir() and (exo / 'eXo' / 'eXoDOS' / '!dos').is_dir():
        exo = exo / 'eXo'
    if not (exo / 'eXoDOS' / '!dos').is_dir():
        sys.exit(f'{exo} is not an eXo folder (no eXoDOS\\!dos in it)')
    p = Patcher(exo, Path(a.staging), a.check)
    if a.revert:
        p.revert()
    else:
        exe = p.staging / 'dosbox.exe'
        if not exe.is_file():
            sys.exit(f'no DOSBox Staging at {exe} (--staging names another folder)')
        if not has_webserver(exe):
            sys.exit(f'{exe} has no HTTP API: the helper needs DOSBox Staging 0.83 or newer')
        missing = [n for n in FILES if not (HERE.parent / n).is_file() and not (HERE / n).is_file()]
        if missing:
            sys.exit('not next to this script or in the folder above: ' + ', '.join(missing))
        if not shutil.which('py'):
            print('note: no "py" (the Python launcher) on the PATH here: the launchers '
                  'then keep using the AutoHotkey helpers')
        p.apply()
    if not p.changes:
        print('nothing would change' if a.check else 'nothing to do')
    elif not a.check:
        print('done')


if __name__ == '__main__':
    main()
