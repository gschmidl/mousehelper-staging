# Notes

Technical findings behind the port (DOSBox Staging 0.84.0-alpha d6406, eXo's
game versions: WarCraft CD 1.22h, Dune II 1.07, SkyNET 1.01, Future Shock).

## The originals

All three helpers are Osprey's AutoHotkey 1.1.27 scripts compiled into an
exe; the script is the plain-text RT_RCDATA resource `>AUTOHOTKEY SCRIPT<`.
eXo starts them from `!dos\<game>\exception.bat` with the DOSBox command line
as arguments. Future Shock uses SkyNET's exe (same file).

## DOSBox Staging API

- `/api/v1/memory/{addr}/{len}` reads linear addresses through paging, but
  refuses any range beyond the emulated RAM size *before* translating it:
  DOS/4GW games mapped at 0x10000000 (SkyNET, Future Shock) cannot be read or
  written. WarCraft's DOS4GW.EXE loads it at 0x1CF000, which is readable.
- `PUT` takes `If-Match: <base64 of expected bytes>` and answers 412 when
  memory differs: an atomic compare-and-swap (used for the IDT gate).
- `/api/v1/memory/allocate` is a DOS allocation owned by the current PSP;
  the helper re-owns its block (MCB owner 0008h, name MOUSEHLP) so it
  survives the batch menu programs exiting. Upper memory is tried first,
  except for SkyNET / Future Shock (see Game details).
- Nothing read from the block is trusted: the host checks its header and
  ring indices on every poll and its code with every heartbeat, and stops
  when they change. Version 0.1 did not: when SkyNET overwrote the block, a
  garbage event index (>= 64) never matched the wrapping tail, and the poll
  loop collected events until the host ran out of memory (21 GB, Windows'
  Explorer hung).
- `/dosbox/info` answers before the emulation runs; memory commands time out
  (500 "timeout") until it does, and while the window is dragged.

## Staging internals that matter

- The built-in DOS mouse driver defaults to the 2-button model: the middle
  button and the wheel never reach DOS. `builtin_dos_mouse_driver_model =
  wheel` fixes that; the wheel is reported once a program calls INT 33h fn 11h
  (the guest does after every reset). Event handlers get AL bit 7 and BH =
  wheel delta (negative = up). There is no horizontal wheel and no buttons 4/5.
- Keyboard controller command D2h puts a byte into the controller "as if from
  the keyboard" and raises IRQ 1, but first *flushes the controller's buffer*:
  a real keystroke waiting in it is lost. The guest only injects when the
  output buffer is empty, it is not inside an E0/E1 frame, and the
  controller has stayed idle for 1200 PIT counts (>= 0.3 ms, the controller's
  byte delay). While an injected byte is unread Staging holds real keystrokes
  back, so one byte at a time is safe.
- Translation (set 2 -> set 1) is off by default, so injected bytes are
  set-1 scancodes as games read them.
- E0-prefixed keys are not injected (a real keystroke could get between the
  two bytes). Keypad codes stand in for the cursor keys; NumLock is switched
  off in the BIOS flags while such a byte is in flight, for games that read it
  (Dune II), and restored right after the game's handler returns.
- `mouse_middle_release` (on by default) makes a middle click in a window
  capture or release the mouse instead of reaching the game, and all three
  games use the middle button: the helper turns it off (Ctrl+F10 still
  releases the mouse).
- Posted Windows mouse messages do not drive the guest mouse (positions come
  from the real cursor, buttons and wheel are ignored); posted key messages
  work. Tests fake mouse events inside the guest instead (test_ev_*).

## DOS/4GW under Staging

- DOS/4GW switches modes without V86 (raw switching): reflected interrupts run
  in real mode. SIDT there shows the real-mode IVT, but GDTR keeps the
  extender's GDT, and one of its data descriptors covers the protected-mode
  IDT (0xF200 for SkyNET's bound extender, 0x2010 for DOS4GW.EXE 1.97). Both
  route IRQ 1 through a dispatcher at 0070:4331.
- Games take IRQ 0 in protected mode without chaining, so the real-mode INT 8
  hook stops ticking. The RTC periodic interrupt (IRQ 8) is reflected to real
  mode and gives the guest its own steady tick (64 Hz; 1024 Hz while a BIOS
  INT 15h 86h wait counts on that rate - the BIOS switches the interrupt off
  when the wait ends, the guest switches it back on).
- Code is ring 0 with flat selectors (WarCraft 0160h/0038h, SkyNET
  0268h/0038h); the IDT stub uses the flat code selector, the GDT supplies a
  flat data selector.

## Timing

AutoHotkey's `~Esc::Send {F10}` puts F10 into the game's queue right behind
Esc, before the game acts on Esc. The host is several milliseconds late:
WarCraft then reopens the menu Esc just closed, and an Esc after F10 closes the
menu F10 just opened. The guest therefore injects such follow-up keys itself
right after the trigger key (kbd_follow), and the host only arms them.

## Game details

- WarCraft: keyboard handler at code offset 0x21104 (`inp(0x60)`, ignores
  typematic repeats, E0 keys translated to 70h-7Fh); scrolling checks both
  the cursor and the keypad codes. The menus dim the screen red; the
  original's 0x550000 test at the bottom centre detects that. The port reads
  those indices from video memory and only their DAC entries (write index
  saved and restored).
- Dune II 1.07: real mode, own INT 9 handler (reads port 60h directly, uses
  the BIOS NumLock flag), INT 33h event handler. The original's colours are
  IBM.PAL indices 128/233 (dialog) and 115/224 (production list).
- SkyNET / Future Shock: poll INT 33h fn 3 and read motion with fn 0Bh.
- SkyNET at 640x480 (VESA 101h; Staging puts S3's mode number 69h in the
  BIOS mode byte) draws its status bar, the bottom 96 lines, as if the
  screen were linear from A0000h: lines 384-479 land at DC000h-EAFFFh, past
  the 64 KB window, in Staging's upper memory block (D000h-DFFFh with EMS)
  and the EMS page frame. A helper block at the top of upper memory was
  overwritten when a mission started, and the next RTC tick ran into it
  (black screen or a sound loop). Without the helper nothing lives there and
  the game runs. The block goes into conventional memory for these games
  (the game left 20BA0h-9FE9Fh untouched; verified in a mission).
- That stray copy does not follow the weapon: with it as the weapon display,
  every wheel step pressed all seven weapon keys. The visible status bar is
  in video memory beyond the 64 KB window the API can see, so the guest
  copies the spots on its tick through VESA window A (4F05h), bank by bank,
  and reports the scan line length (4F06h) and display start (4F07h) the
  offsets depend on. Staging's VESA BIOS switches banks through CRTC
  register 6Ah, so the guest puts back the CRTC index along with the game's
  bank. INT 10h is a plain IRET callback in Staging: calling it from the
  RTC handler does not open an interrupt window.

- SkyNET's weapon display (the original's three spots, read as 5x5-pixel
  patches) differs per weapon and does not change with ammo. In a mission at
  640x480 it changed 60-150 ms after a weapon key; some reads failed (blank
  or no answer), and after turning or shooting the game sometimes ignored a
  40 ms key press of a weapon the player had. With the original's algorithm
  (a thread per wheel step, #MaxThreadsPerHotkey 10; a 40 ms press, one
  check 50 ms later) cycling then felt random. The original AutoHotkey helper
  under eXo's DOSBox ECE (same 50000 cycles) is just as unreliable with the
  wheel (compared 2026-10-07), so this is the original's behaviour. With
  the user's OK the port changes two things: one worker does the steps in
  turn (opposite steps cancel out), and each key is held until the display
  changes (0.45 s at most). A step also stops without another key when the
  display moved since its first key (that key took effect late), and a read
  of the display that fails is tried again (PixelGetColor never failed).
- Measured with that (SkyNET mission, 640x480, 50000 cycles): the display
  changed 0.12-0.30 s after the key went down (median 0.19 s), for every key
  of a weapon the player had. The original looks once, about 0.09 s after
  the key went down (40 ms press, 50 ms wait), so it mostly saw no change
  and passed over weapons the player had. The user found 0.4 better than the
  original.
- Future Shock runs at 320x200 (mode 13h) in eXo, with the same status bar.
  The original's spots fall on the lower edge of the weapon picture in the
  box under SAFE; sampling the whole picture every 50 ms showed the spots
  changing with every change of the picture. With only weapons 1 and 2
  owned, a step up from 2 tries 3-7 first, up to 0.45 s each (about 2.5 s
  before anything shows; a second wheel step in that time switches straight
  back). Remembering the weapons that did nothing was offered and declined
  (2026-10-07): the helper keeps trying each of them, like the original.

## Not verified in a game

- SkyNET at 320x200 (will not be tested). Verified with a real mouse in a
  SkyNET mission at 640x480 and in Future Shock at 320x200: wheel cycling
  with auto-skip, middle click (with `mouse_middle_release=false`, which the
  helper sets), the keyboard filter, mouse capture and polling path.
- Dune II with a real mouse (will not be tested): its tests used events
  faked inside the guest, and before `mouse_middle_release=false` a real
  middle click in a window never reached it. WarCraft with a real mouse, middle click included, was
  checked by the user (2026-10-07).
