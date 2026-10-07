# Mouse helpers for WarCraft, Dune II and SkyNET (DOSBox Staging)

A platform-independent port of Osprey's AutoHotkey "mouse helpers" (2018) for

- **WarCraft: Orcs & Humans** (`WarcraftMouseHelper.exe`)
- **Dune II: The Building of a Dynasty** and Super Dune II Classic (`Dune2MouseHelper.exe`)
- **SkyNET** and **The Terminator: Future Shock** (`SkyNETMouseHelper.exe`)

The originals hook the Windows mouse and keyboard and sample screen pixels, so
they only work on Windows. This port does the same job from inside the
emulated PC: it uses the HTTP API of DOSBox Staging (0.83 or newer) to plant a
small resident program between the game and the mouse driver / keyboard
controller, and runs the helper logic in Python on the host. It needs no OS
hooks or permissions and reads no screen, and it behaves the same on Windows,
macOS and Linux (X11 or Wayland).

## Requirements

- Python 3.8 or newer (standard library only; `tkinter` for the on-screen
  help and messages, otherwise they go to the console)
- DOSBox Staging 0.83 or newer (the HTTP API)

## Usage

Like the originals, the helper wraps DOSBox: it starts it, and exits when it
exits.

    python mousehelper.py --game warcraft -- <dosbox> [dosbox arguments...]
    python mousehelper.py --game dune2    -- <dosbox> [dosbox arguments...]
    python mousehelper.py --game skynet   -- <dosbox> [dosbox arguments...]

It adds `--set webserver_enabled=true --set webserver_port=<free port> --set
builtin_dos_mouse_driver_model=wheel --set mouse_middle_release=false` to the
DOSBox command line (the default 2-button mouse model hides the middle button
and the wheel from DOS, and by default a middle click in a window releases
the mouse instead of reaching the game; Ctrl+F10 still does).

`--attach PORT` uses a DOSBox that is already running with the API enabled
(start the helper before the game, so it can wrap the game's mouse handler).
`--no-splash` prints messages instead of showing them. `-v` logs everything.

Settings (WASD scrolling, auto-select, weapon mode, ...) are kept in
`mousehelper.json` next to the script.

## What the helpers do

The in-game help (Shift+F10) lists the features of each game, as in the
originals:

**WarCraft**: left drag selects without Ctrl; right click attacks / harvests /
moves; middle click repairs or moves, held it auto-selects; wheel and WASD
scroll the map; Esc opens the options menu; F6-F8 and F10-F12 close their
screens on a second press; held Backspace repeats; Shift+F11 / Shift+F12
toggle WASD scrolling / auto-select; Pause suspends the helper.

**Dune II**: right click moves / attacks / harvests, builds and places; held,
it repairs or upgrades; middle click attacks or opens the production screen;
left click held auto-selects; wheel and WASD scroll; Esc opens the options;
F1-F3 close their screens on a second press.

**SkyNET / Future Shock**: wheel cycles weapons (types or all 13), skipping
the ones you do not have; middle click cycles weapons of
the same type; ~ and Shift+~ cycle without a wheel; Shift+F9 works like F9.

## How it works

`guest/mh.asm` (assembled by `build.py` into `guestcode.py`) is written into a
DOS memory block (upper memory if possible; conventional memory for SkyNET
and Future Shock, which draw over upper memory at 640x480) and hooks:

- **INT 33h**: keeps the helper's own event handler in the mouse driver and
  passes events on to the game's handler (or answers its polls) with the
  buttons the helper takes over hidden, plus the helper's synthetic clicks;
- **INT 70h** (RTC periodic interrupt, 64 Hz): a steady tick to work through
  the host's commands, even when a protected-mode game owns the timer;
- **the keyboard**: for real-mode games (Dune II) an INT 9 filter in front of
  the game's own handler; for DOS/4GW games (WarCraft, SkyNET) the extender's
  protected-mode IDT entry for IRQ 1 is pointed at a 32-bit filter that then
  calls the original. Keys the helper remaps are swallowed there;
- **key injection**: keyboard controller command D2h puts a byte into the
  controller as if typed, so the game's own keyboard handler reads it.

The host polls an event ring in that block every few milliseconds and queues
key presses and clicks for the guest to deliver. Keys that the originals sent
right behind another one (Esc -> F10 in WarCraft and the like) are injected by
the guest itself, since the game must see them before it acts on the first.

If the helper stops (crash, killed), the guest notices within a few seconds
and passes all input through unchanged. A restarted helper reuses the block
that is already installed.

## eXo

`exo/patch_exo.py` puts the helper into an eXoDOS installation (Windows).
The release zip on the
[releases page](https://github.com/gschmidl/mousehelper-staging/releases)
has it with the helper files next to it; unpack it anywhere and run

    python patch_exo.py [--check | --revert] [--staging FOLDER] EXO_FOLDER

- `util\mousehelper\` gets mousehelper.py, guestcode.py, README.md and NOTES.md.
- In every launcher that starts one of the AutoHotkey helpers (English,
  German and Spanish), that line starts this helper with `py` instead, with
  `--staging <FOLDER>\dosbox.exe` and the AutoHotkey helper as `--fallback`.
  The original line stays for when there is no `py` or no helper.
  `exception.bat.orig` keeps each launcher. SkyNET's and Future Shock's
  launchers run the helper with eXo's alternate (Staging) options too.
- Installed games' run.bat lose the note that the helper is "not fully
  compatible with Dosbox Staging" (`run.bat.orig` keeps it).

`--staging` is the DOSBox Staging folder relative to the eXo folder (default
`emulators\dosbox\staging0.83.0`); it must be 0.83 or newer. Run the patch
again with another `--staging` to switch; `--check` shows what would change,
`--revert` undoes it.

Which DOSBox runs: the launcher's own (`%dosbox%`) when it is Staging 0.83+
(eXo's alternate launcher), else the `--staging` one (eXo's normal launch
uses DOSBox ECE for these games). Without any Staging 0.83+ the helper runs
the original AutoHotkey helper (Windows) or DOSBox alone. `-nomenu` and an
empty `-conf` (ECE launch lines) are dropped for Staging.

On Linux and macOS (not covered by the patch) the equivalent of the launch
line is

    python3 util/mousehelper/mousehelper.py --game <game> -- <staging> -conf ...

with `<game>` = `warcraft`, `dune2` or `skynet` (also for Future Shock).

`--game probe` hooks everything without changing anything and logs the
game's input (`-v`), for looking at other games.

## Differences from the originals

- 4th/5th mouse buttons and horizontal wheels: DOS mouse drivers have no way
  to report them; use Shift+wheel (WarCraft, Dune II) for left/right.
- The pixel tests are done on the game's video memory instead of the screen,
  so they work with any window size, full screen and shaders.
- Dune II: the original only sent Harvest (`h`) to version 1.07 if it had been
  told where the game was (eXo never did, so there it always acted as 1.00).
  The port detects the version itself.
- SkyNET: weapon auto-skip no longer loops forever when it runs off the bottom
  of the weapon list. Wheel steps, ~ and middle clicks are done one after
  the other, and each weapon key is held until the weapon display changes
  (0.45 s at most) instead of tapped for 40 ms and checked once 50 ms later:
  the game shows the new weapon 0.12-0.30 s after the key, so the original
  mostly saw no change and passed over weapons you had. Passing over a
  weapon you do not have takes up to 0.45 s.
- The Windows-only parts (tray icon, shortcut maker, settings window) are
  gone; edit `mousehelper.json` for the options without a hotkey
  (`InvertCycle` for SkyNET).

## Files

- `mousehelper.py`: the helper
- `guest/mh.asm`, `build.py`, `guestcode.py`: the guest program (NASM; set
  `MH_NASM` if `nasm` is not on the PATH)
- `tests/`: test drivers (Windows; they post window messages to a test DOSBox
  and fake mouse events inside the guest); `test_vram_service.py` runs the
  guest's video memory reader in the Unicorn emulator (`pip install
  unicorn`), `test_weapon_cycle.py` SkyNET's weapon cycling against a
  simulated game
- `exo/patch_exo.py`: the eXoDOS patch; `make_release.py` zips it with the
  helper (`release/`)
- `NOTES.md`: how the games and DOSBox Staging behave, and what is untested

## License

MIT, see `LICENSE`. The original helpers are Osprey's; none of their files
are included here, `NOTES.md` only describes what they do.
