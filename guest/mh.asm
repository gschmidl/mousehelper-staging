; mh.asm - guest-resident part of the mouse helper.
;
; Assembled as a flat binary and written by the host (mousehelper.py) into a DOS
; memory block through the DOSBox Staging HTTP API; the block starts at
; <segment>:0000 and every label below is an offset from the block start.
;
; Real-mode part (16-bit):
;   int33_hook   wraps the DOS mouse driver: installs our own event handler,
;                forwards events to the game's handler with buttons hidden as
;                configured, adds synthetic button presses
;   int8_hook    timer: drains the host's command queue, keeps a real-mode
;                INT 9 hook on top of a real-mode game's keyboard handler
;   int70_hook   RTC periodic interrupt, our own steady tick: protected-mode
;                games take over IRQ 0, but leave IRQ 8 to be reflected to
;                real mode
;   int9_hook    real-mode keyboard filter (for real-mode games)
; Protected-mode part (32-bit):
;   pm_kbd_entry keyboard filter for DOS/4GW games: the host points the
;                extender's IDT entry for IRQ 1 at it and it calls the
;                original gate as an interrupt (pm_chain)
;
; Keys are injected with keyboard controller command D2h ("write keyboard
; output buffer"): the game's own IRQ 1 handler then reads them from port 60h
; like real keystrokes.

        cpu     386
        org     0

        [map symbols mh.map]

EV_COUNT        equ     64              ; event ring entries (8 bytes each)
CQ_COUNT        equ     64              ; command queue entries (4 bytes each)

EV_MOUSE        equ     1               ; a=event mask, b=buttons, c=wheel, cx, dx
EV_KEY          equ     2               ; a=byte, b=flags, c=shift state
EV_INFO         equ     3               ; a=code (see INFO_*)

KF_DROPPED      equ     1               ; EV_KEY flags
KF_INJECTED     equ     2
KF_E1PART       equ     4               ; inside an E1 frame (after its first byte)
KF_EXTENDED     equ     8

INFO_SETCB      equ     1               ; game set its event handler (b:c=mask, cx=off, dx=seg)
INFO_RESET      equ     2               ; mouse driver reset (b=1 if wheel API on)
INFO_INT9_ON    equ     3               ; real-mode INT 9 hook installed (cx=off, dx=seg chained)
INFO_INT9_OFF   equ     4               ; INT 9 no longer ours (cx:dx = new vector)

CQ_KEY          equ     1               ; inject scancode byte (arg)
CQ_BTN_DOWN     equ     2               ; press synthetic buttons (arg bits)
CQ_BTN_UP       equ     3               ; release synthetic buttons (arg bits)
CQ_WAIT_POLL    equ     4               ; polling games: wait until INT 33h fn 3 ran

RTC_RATE_SLOW   equ     2Ah             ; RTC register A: 64 Hz
RTC_RATE_BIOS   equ     26h             ; 1024 Hz, what the BIOS wait (INT 15h 86h) counts on
BIOS_WAIT_ACTIVE equ    4A0h            ; BIOS data area: INT 15h 86h wait in progress
BIOS_KBD_FLAGS  equ     417h            ; bit 5: NumLock
; Keypad codes are injected without the E0 prefix (a real keystroke could get
; between the two bytes).  Games that read the BIOS NumLock flag would take
; them as digits, so NumLock is off while such a byte is in flight.

PIT_WAIT        equ     1200            ; PIT counts the controller must stay idle
PIT_LOOPS       equ     60000           ; upper bound of the idle wait loop

VRAM_RUNS       equ     16              ; video memory runs per request (4 bytes each)
VRAM_BUF        equ     128             ; bytes they may add up to

; ---------------------------------------------------------------------------
; Data shared with the host
; ---------------------------------------------------------------------------
hdr_magic       db      'MHLP'
hdr_version     dw      2
hdr_size        dw      block_end

cfg_enabled     db      0       ; 0 = pass everything through (suspended/not ready)
cfg_capture     db      0       ; physical buttons hidden from the game: 1=L 2=R 4=M
cfg_drop_pause  db      0       ; swallow Pause (E1 ...) frames
cfg_rmkbd       db      0       ; keep a real-mode INT 9 hook on top of the game's
cfg_polling     db      0       ; game polls fn 3: CQ_WAIT_POLL waits for a poll
kbd_active      db      0       ; a keyboard hook sees the game's keys
busy            db      0
ms_need_cb      db      0       ; host: put our event handler into the driver

kbd_drop_plain  times 16 db 0   ; bit n: swallow make code n without Shift
kbd_drop_shift  times 16 db 0   ; bit n: swallow make code n with Shift held
kbd_dropped     times 16 db 0   ; makes swallowed; their breaks go too
kbd_follow      times 16 db 0   ; host: (make code, key to tap right after it)
                                ; pairs - AutoHotkey's "~X::Send {Y}" lands in
                                ; the game's queue right behind X; the host
                                ; would be milliseconds late
pq_head         db      0       ; follow-up bytes, injected before the host's
pq_tail         db      0       ; queue
pq_buf          times 8 db 0
kbd_shift       db      0       ; bit 0 = left Shift down, bit 1 = right Shift down
kbd_prefix      db      0       ; 0E0h after an E0 prefix
kbd_e1_left     db      0       ; bytes left in the current E1 frame
kbd_e1_drop     db      0       ; the current E1 frame is swallowed
inj_inflight    db      0       ; injected byte not read yet
inj_byte        db      0
inj_ticks       db      0
inj_nl_restore  db      0       ; NumLock was switched off for the byte in flight
ms_buttons      db      0       ; physical buttons (last event)

ms_client       dd      0       ; game's event handler (off, seg); 0 = none
ms_client_mask  dw      0
ms_x            dw      0
ms_y            dw      0
ms_synth        db      0       ; synthetic buttons held down for the game
ms_wheel        db      0       ; wheel API enabled
ms_polled       db      0       ; fn 3 ran since the last synthetic change
                db      0
ms_spress       times 4 db 0    ; synthetic presses per button (for fn 5)
ms_srelease     times 4 db 0    ; synthetic releases per button (for fn 6)

test_ev_flag    db      0       ; tests: bit 0 = report this event next, bit 1 =
                                ; report all events at this position/buttons
test_ev_mask    db      0
test_ev_buttons db      0
test_ev_wheel   db      0
test_ev_x       dw      0
test_ev_y       dw      0
test_mickey_x   dw      0       ; tests: faked motion for fn 0Bh
test_mickey_y   dw      0
fn_count        times 32 dw 0   ; diagnostics: INT 33h calls per function
test_press      times 4 db 0    ; tests: faked presses/releases per button,
test_release    times 4 db 0    ; reported by fn 5/6 while the override is on
rtc_wanted      db      0       ; host: keep the RTC periodic interrupt running
host_alive      db      255     ; host: refreshed while it runs; counts down at
                                ; 64 Hz, and at 0 the guest stops taking input
dac_req         db      0       ; host: read this many DAC entries (dac_idx)
dac_seq         db      0       ; guest: incremented when they are in dac_rgb
dac_idx         times 16 db 0
dac_rgb         times 48 db 0   ; 6-bit R, G, B per entry
                db      0
vram_req        db      0       ; host: copy this many runs (vram_run) of video
                                ; memory into vram_buf (VESA modes: the host
                                ; sees only the 64 KB window)
vram_seq        db      0       ; guest: incremented when it has answered
vram_ok         db      0       ; guest: 1 = copied, 0 = no VESA window
                db      0
vram_pitch      dw      0       ; guest: bytes per scan line
vram_start_x    dw      0       ; guest: display start (pixel, scan line)
vram_start_y    dw      0
vram_run        times VRAM_RUNS*4 db 0  ; host: 64 KB bank, length, offset (dw)
vram_buf        times VRAM_BUF db 0

old_int33       dd      0
old_int8        dd      0
old_int70       dd      0
bios_int9       dd      0       ; INT 9 vector when the block was installed (info)
chain_int9      dd      0       ; real-mode game handler we chain to
int9_req        dd      0       ; host: hook INT 9 if it still holds this

cnt_int8        dw      0
cnt_int33       dw      0
cnt_mouse_cb    dw      0
cnt_kbd         dw      0
cnt_inject      dw      0
cnt_ev_lost     dw      0
cnt_pm_kbd      dw      0
cnt_int70       dw      0
sys_idtr        dw      0       ; SIDT/SGDT/SMSW as seen by the RTC tick: under
                dd      0       ; a DOS extender (V86 mode) these show its
sys_gdtr        dw      0       ; protected-mode tables
                dd      0
sys_msw         dw      0

pm_ds_store     dd      0       ; linear address of a flat data selector word
pm_ds_value     dw      0       ; host: the flat data selector (pm_ds_store -> here)
pm_chain        dd      0       ; host: original IDT gate (offset, selector)
                dw      0

ev_head         dw      0       ; next slot the guest writes
ev_tail         dw      0       ; next slot the host reads
ev_buf          times EV_COUNT*8 db 0

cq_head         dw      0       ; next slot the host writes
cq_tail         dw      0       ; next slot the guest reads
cq_buf          times CQ_COUNT*4 db 0

; ===========================================================================
; Real mode
; ===========================================================================
        bits    16

; log_event: AL=type AH=a BL=b BH=c CX DX -> one ring entry.  DS=CS.
log_event:
        push    si
        push    di
        mov     si, [ev_head]
        mov     di, si
        inc     di
        and     di, EV_COUNT-1
        cmp     di, [ev_tail]
        je      .full
        shl     si, 3
        mov     [ev_buf+si], ax
        mov     [ev_buf+si+2], bx
        mov     [ev_buf+si+4], cx
        mov     [ev_buf+si+6], dx
        mov     [ev_head], di
        pop     di
        pop     si
        ret
.full:  inc     word [cnt_ev_lost]
        pop     di
        pop     si
        ret

; read_pit16: AX = PIT channel 0 count
read_pit16:
        mov     al, 0
        out     43h, al
        in      al, 40h
        mov     ah, al
        in      al, 40h
        xchg    al, ah
        ret

; kbc_safe16: CF=0 when a byte can be injected without the controller's
; flush on D2h eating a real keystroke: no byte waiting, not inside a
; multi-byte frame, and the controller stays idle for PIT_WAIT counts.
kbc_safe16:
        push    ax
        push    bx
        push    cx
        push    dx
        in      al, 64h
        test    al, 1
        jnz     .unsafe
        cmp     byte [kbd_prefix], 0
        jne     .unsafe
        cmp     byte [kbd_e1_left], 0
        jne     .unsafe
        call    read_pit16
        mov     bx, ax
        xor     dx, dx
        mov     cx, PIT_LOOPS
.loop:  in      al, 64h
        test    al, 1
        jnz     .unsafe
        call    read_pit16
        cmp     ax, bx
        ja      .wrap
        sub     bx, ax
        add     dx, bx
        jc      .safe
        jmp     .next
.wrap:  add     dx, bx
        jc      .safe
.next:  mov     bx, ax
        cmp     dx, PIT_WAIT
        jae     .safe
        loop    .loop
.safe:  clc
        jmp     .out
.unsafe:
        stc
.out:   pop     dx
        pop     cx
        pop     bx
        pop     ax
        ret

; kbc_inject16: AL = byte to put into the controller's output buffer
kbc_inject16:
        push    ax
        push    cx
        push    es
        xor     cx, cx
        mov     es, cx
        mov     ah, al
        and     ah, 7Fh
        cmp     ah, 47h
        jb      .nonl
        cmp     ah, 53h
        ja      .nonl
        test    byte [es:BIOS_KBD_FLAGS], 20h
        jz      .nonl
        and     byte [es:BIOS_KBD_FLAGS], ~20h
        mov     byte [inj_nl_restore], 1
.nonl:  pop     es
        mov     ah, al
        mov     cx, 1000
.w1:    in      al, 64h
        test    al, 2
        loopnz  .w1
        mov     al, 0D2h
        out     64h, al
        mov     cx, 1000
.w2:    in      al, 64h
        test    al, 2
        loopnz  .w2
        mov     al, ah
        out     60h, al
        pop     cx
        pop     ax
        ret

; dac_service: read the palette entries the host asked for.  The game's
; DAC write position is put back afterwards, so a palette upload it was in
; the middle of carries on (at worst one entry restarts).  Clobbers AL.
dac_service:
        cmp     byte [dac_req], 0
        je      .r
        push    bx
        push    cx
        push    dx
        push    si
        mov     dx, 3C8h
        in      al, dx                  ; current write index
        mov     ah, al
        mov     cl, [dac_req]
        xor     ch, ch
        cmp     cx, 16
        jbe     .n
        mov     cx, 16
.n:     xor     si, si
        mov     bx, dac_rgb
.next:  mov     al, [dac_idx+si]
        mov     dx, 3C7h
        out     dx, al
        mov     dx, 3C9h
        in      al, dx
        mov     [bx], al
        in      al, dx
        mov     [bx+1], al
        in      al, dx
        mov     [bx+2], al
        add     bx, 3
        inc     si
        loop    .next
        mov     dx, 3C8h                ; back to writing where the game was
        mov     al, ah
        out     dx, al
        mov     byte [dac_req], 0
        inc     byte [dac_seq]
        pop     si
        pop     dx
        pop     cx
        pop     bx
.r:     ret

; vram_service: copy the runs of video memory the host asked for, bank by
; bank through VESA window A, and report the scan line length and display
; start.  The game's bank and the CRTC index (the VESA BIOS switches banks
; through CRTC register 6Ah) are put back afterwards.  Clobbers AL.
vram_service:
        cmp     byte [vram_req], 0
        je      .r
        pusha
        push    es
        mov     dx, 3D4h
        in      al, dx
        push    ax                      ; the game's CRTC index
        mov     byte [vram_ok], 0
        mov     ax, 4F06h
        mov     bl, 1                   ; get logical scan line length
        int     10h
        cmp     ax, 004Fh
        jne     .done
        mov     [vram_pitch], bx
        mov     ax, 4F07h
        mov     bx, 1                   ; get display start
        int     10h
        cmp     ax, 004Fh
        jne     .done
        mov     [vram_start_x], cx
        mov     [vram_start_y], dx
        mov     ax, 4F05h
        mov     bx, 0100h               ; get window A position
        int     10h
        cmp     ax, 004Fh
        jne     .done
        push    dx                      ; the game's bank
        push    cs
        pop     es
        cld                             ; IRET restores the game's DF
        mov     di, vram_buf
        mov     si, vram_run
        mov     cl, [vram_req]
        xor     ch, ch
        cmp     cx, VRAM_RUNS
        jbe     .next
        mov     cx, VRAM_RUNS
.next:  push    cx
        mov     dl, [si]                ; bank
        xor     dh, dh
        mov     ax, 4F05h
        xor     bx, bx                  ; set window A position
        int     10h
        mov     cl, [si+1]              ; length, kept within the buffer
        xor     ch, ch
        mov     ax, vram_buf+VRAM_BUF
        sub     ax, di
        cmp     cx, ax
        jbe     .copy
        mov     cx, ax
.copy:  push    si
        push    ds
        mov     si, [si+2]              ; offset in the window
        push    word 0A000h
        pop     ds
        rep     movsb
        pop     ds
        pop     si
        add     si, 4
        pop     cx
        loop    .next
        pop     dx
        mov     ax, 4F05h
        xor     bx, bx
        int     10h                     ; the game's bank again
        mov     byte [vram_ok], 1
.done:  pop     ax
        mov     dx, 3D4h
        out     dx, al                  ; the game's CRTC index again
        mov     byte [vram_req], 0
        inc     byte [vram_seq]
        pop     es
        popa
.r:     ret

; inj_watchdog: an injected byte that no hook of ours saw being read (the
; game read it elsewhere) must not block the queue forever.  Clobbers AL.
inj_watchdog:
        cmp     byte [inj_inflight], 0
        je      .r
        inc     byte [inj_ticks]
        cmp     byte [inj_ticks], 6
        jb      .r
        in      al, 64h
        test    al, 1
        jnz     .r
        mov     byte [inj_inflight], 0
        call    nl_restore16
.r:     ret

; nl_restore16: give NumLock back once the injected keypad byte was read
nl_restore16:
        cmp     byte [inj_nl_restore], 0
        je      .r
        cmp     byte [inj_inflight], 0
        jne     .r
        push    es
        push    ax
        xor     ax, ax
        mov     es, ax
        or      byte [es:BIOS_KBD_FLAGS], 20h
        mov     byte [inj_nl_restore], 0
        pop     ax
        pop     es
.r:     ret

; eff_capture16: AL = buttons hidden from the game right now
eff_capture16:
        xor     al, al
        cmp     byte [cfg_enabled], 0
        je      .r
        mov     al, [cfg_capture]
.r:     ret

; game_buttons16: BL = button state the game should see
game_buttons16:
        push    ax
        call    eff_capture16
        not     al
        mov     bl, [ms_buttons]
        and     bl, al
        and     bl, 7
        or      bl, [ms_synth]
        pop     ax
        ret

; call_client16: AX = event mask (already filtered), BX = buttons, SI/DI =
; mickeys.  Calls the game's event handler if it wants any of these events.
call_client16:
        and     ax, [ms_client_mask]
        jz      .r
        cmp     dword [ms_client], 0
        je      .r
        pusha
        push    ds
        push    es
        mov     cx, [ms_x]
        mov     dx, [ms_y]
        call    far [ms_client]
        pop     es
        pop     ds
        popa
.r:     ret

; deliver_btn16: AL = CQ_BTN_DOWN/UP, AH = button bits.  Updates the
; synthetic state and sends the matching events to the game.
deliver_btn16:
        pusha
        mov     cl, ah                  ; cl = button bits
        xor     ch, ch                  ; ch = event mask
        mov     bx, ms_spress
        cmp     al, CQ_BTN_DOWN
        je      .down
        mov     bx, ms_srelease
        mov     al, cl
        not     al
        and     [ms_synth], al
        jmp     .bits
.down:  or      [ms_synth], cl
.bits:  mov     al, ah                  ; bit0 L, bit1 R, bit2 M
        test    al, 1
        jz      .b2
        or      ch, 02h
        inc     byte [bx]
.b2:    test    al, 2
        jz      .b3
        or      ch, 08h
        inc     byte [bx+1]
.b3:    test    al, 4
        jz      .send
        or      ch, 20h
        inc     byte [bx+2]
.send:  cmp     bx, ms_spress
        je      .go
        shl     ch, 1                   ; press mask -> release mask
.go:    mov     byte [ms_polled], 0
        call    game_buttons16
        mov     al, ch
        xor     ah, ah
        xor     si, si
        xor     di, di
        call    call_client16
        popa
        ret

; drain16: work through the host's command queue.  Called with IF=0 from
; the timer, mouse and keyboard hooks.  One injected byte per call: the next
; one goes out after the game has read it.
drain16:
        cmp     byte [busy], 0
        jne     .ret
        mov     byte [busy], 1
        push    ax
        push    bx
        push    si
        mov     bl, [pq_tail]           ; follow-up bytes go first
        cmp     bl, [pq_head]
        je      .next
        cmp     byte [inj_inflight], 0
        jne     .done
        call    kbc_safe16
        jc      .done
        xor     bh, bh
        mov     al, [pq_buf+bx]
        call    kbc_inject16
        mov     [inj_byte], al
        mov     byte [inj_inflight], 1
        mov     byte [inj_ticks], 0
        inc     word [cnt_inject]
        inc     bl
        and     bl, 7
        mov     [pq_tail], bl
        jmp     .done
.next:  mov     si, [cq_tail]
        cmp     si, [cq_head]
        je      .done
        mov     bx, si
        shl     bx, 2
        mov     al, [cq_buf+bx]
        mov     ah, [cq_buf+bx+1]
        cmp     al, CQ_KEY
        je      .key
        cmp     al, CQ_BTN_DOWN
        je      .btn
        cmp     al, CQ_BTN_UP
        je      .btn
        cmp     al, CQ_WAIT_POLL
        je      .poll
        jmp     .adv                    ; unknown: skip
.key:   cmp     byte [kbd_active], 0
        je      .done
        cmp     byte [inj_inflight], 0
        jne     .done
        call    kbc_safe16
        jc      .done
        mov     al, ah
        call    kbc_inject16
        mov     [inj_byte], ah
        mov     byte [inj_inflight], 1
        mov     byte [inj_ticks], 0
        inc     word [cnt_inject]
        call    .bump
        jmp     .done
.btn:   cmp     byte [inj_inflight], 0
        jne     .done                   ; keys queued before must arrive first
        call    deliver_btn16
        jmp     .adv
.poll:  cmp     byte [cfg_polling], 0
        je      .adv
        cmp     byte [ms_polled], 0
        je      .done
.adv:   call    .bump
        jmp     .next
.done:  pop     si
        pop     bx
        pop     ax
        mov     byte [busy], 0
.ret:   ret
.bump:  mov     si, [cq_tail]
        inc     si
        and     si, CQ_COUNT-1
        mov     [cq_tail], si
        ret

; classify16: AL = byte read from port 60h.  Tracks prefixes and Shift,
; logs the byte, CF=1 if the game must not see it.  Preserves registers.
classify16:
        push    ax
        push    bx
        push    cx
        push    dx
        mov     ah, al                  ; ah = byte
        xor     dl, dl                  ; dl = flags
        cmp     byte [inj_inflight], 0
        je      .noinj
        cmp     al, [inj_byte]
        jne     .noinj
        mov     byte [inj_inflight], 0
        or      dl, KF_INJECTED
.noinj: cmp     byte [kbd_e1_left], 0
        je      .noe1
        dec     byte [kbd_e1_left]
        or      dl, KF_E1PART
        cmp     byte [kbd_e1_drop], 0
        je      .pass
        jmp     .drop
.noe1:  cmp     ah, 0E1h
        jne     .note1
        mov     byte [kbd_e1_left], 5
        mov     byte [kbd_e1_drop], 0
        test    dl, KF_INJECTED
        jnz     .pass
        mov     al, [cfg_drop_pause]    ; Pause toggles suspension: not gated
        mov     [kbd_e1_drop], al
        test    al, al
        jz      .pass
        jmp     .drop
.note1: cmp     ah, 0E0h
        jne     .note0
        mov     [kbd_prefix], ah
        jmp     .pass
.note0: mov     dh, [kbd_prefix]
        mov     byte [kbd_prefix], 0
        test    dh, dh
        jz      .plain
        or      dl, KF_EXTENDED
        jmp     .pass                   ; extended keys always pass
.plain: mov     bl, ah
        and     bl, 7Fh
        mov     cl, 1
        cmp     bl, 2Ah
        je      .shift
        mov     cl, 2
        cmp     bl, 36h
        jne     .other
.shift: test    ah, 80h
        jnz     .shup
        or      [kbd_shift], cl
        jmp     .pass
.shup:  not     cl
        and     [kbd_shift], cl
        jmp     .pass
.other: mov     cl, bl
        and     cl, 7
        mov     ch, 1
        shl     ch, cl                  ; ch = bit mask
        xor     bh, bh
        shr     bx, 3                   ; bx = byte index
        test    ah, 80h
        jnz     .break
        test    dl, KF_INJECTED
        jnz     .mkpass
        cmp     byte [cfg_enabled], 0
        je      .mkpass
        mov     al, [kbd_drop_plain+bx]
        cmp     byte [kbd_shift], 0
        je      .tbl
        mov     al, [kbd_drop_shift+bx]
.tbl:   test    al, ch
        jz      .mkpass
        or      [kbd_dropped+bx], ch
        jmp     .drop
.mkpass:
        not     ch
        and     [kbd_dropped+bx], ch
        test    dl, KF_INJECTED
        jnz     .pass
        cmp     byte [cfg_enabled], 0
        je      .pass
        call    follow16                ; AH = the make code
        jmp     .pass
.break: test    [kbd_dropped+bx], ch
        jz      .pass
        not     ch
        and     [kbd_dropped+bx], ch
        test    dl, KF_INJECTED
        jnz     .pass
        jmp     .drop
.pass:  call    .log
        clc
        jmp     .out
.drop:  or      dl, KF_DROPPED
        call    .log
        stc
.out:   pop     dx
        pop     cx
        pop     bx
        pop     ax
        ret
.log:   push    ax
        push    bx
        push    cx
        push    dx
        mov     al, EV_KEY              ; ah = byte
        mov     bl, dl
        mov     bh, [kbd_shift]
        xor     cx, cx
        xor     dx, dx
        call    log_event
        pop     dx
        pop     cx
        pop     bx
        pop     ax
        ret

; follow16: AH = make code the game is about to get; queue its follow-ups
follow16:
        push    ax
        push    cx
        push    si
        xor     si, si
        mov     cx, 8
.l:     mov     al, [kbd_follow+si]
        test    al, al
        jz      .n
        cmp     al, ah
        jne     .n
        mov     al, [kbd_follow+si+1]
        call    pq_push16
        or      al, 80h
        call    pq_push16
.n:     add     si, 2
        loop    .l
        pop     si
        pop     cx
        pop     ax
        ret

; pq_push16: AL = byte for the follow-up queue (dropped when full)
pq_push16:
        push    bx
        push    cx
        mov     bl, [pq_head]
        mov     cl, bl
        inc     cl
        and     cl, 7
        cmp     cl, [pq_tail]
        je      .full
        xor     bh, bh
        mov     [pq_buf+bx], al
        mov     [pq_head], cl
.full:  pop     cx
        pop     bx
        ret

; install_cb16: put our event handler into the driver (DS=CS)
install_cb16:
        pusha
        push    es
        mov     ax, 0Ch
        mov     cx, 0FFh
        push    cs
        pop     es
        mov     dx, mouse_cb
        pushf
        call    far [old_int33]
        pop     es
        popa
        ret

; ---------------------------------------------------------------------------
; INT 33h
; ---------------------------------------------------------------------------
int33_hook:
        cmp     ax, 1Fh
        ja      .nocnt
        push    bx
        mov     bx, ax
        shl     bx, 1
        inc     word [cs:fn_count+bx]
        pop     bx
.nocnt: cmp     ax, 0Bh
        je      .motion
        cmp     ax, 0Ch
        je      .setcb
        cmp     ax, 14h
        je      .xchg
        cmp     ax, 3
        je      .status
        test    ax, ax
        je      .reset
        cmp     ax, 21h
        je      .reset
        cmp     ax, 5
        je      .counts
        cmp     ax, 6
        je      .counts
        jmp     far [cs:old_int33]

.reset: pushf
        call    far [cs:old_int33]      ; AX/BX = result for the caller
        push    ds
        push    cs
        pop     ds
        inc     word [cnt_int33]
        pusha
        mov     dword [ms_client], 0
        mov     word [ms_client_mask], 0
        mov     byte [ms_synth], 0
        mov     ax, 11h                 ; WheelAPI: enable wheel reporting
        pushf
        call    far [old_int33]
        xor     bl, bl
        cmp     ax, 574Dh
        jne     .nowh
        inc     bl
.nowh:  mov     [ms_wheel], bl
        call    install_cb16
        mov     al, EV_INFO
        mov     ah, INFO_RESET
        xor     bh, bh
        xor     cx, cx
        xor     dx, dx
        call    log_event
        popa
        pop     ds
        iret

.setcb: push    ds
        push    cs
        pop     ds
        inc     word [cnt_int33]
        pusha
        mov     [ms_client], dx
        mov     [ms_client+2], es
        mov     [ms_client_mask], cx
        mov     al, EV_INFO
        mov     ah, INFO_SETCB
        mov     bx, cx
        mov     cx, dx
        mov     dx, es
        call    log_event
        call    install_cb16            ; our handler stays installed
        popa
        pop     ds
        iret

.xchg:  push    ds
        push    cs
        pop     ds
        inc     word [cnt_int33]
        push    word [ms_client_mask]
        push    word [ms_client+2]
        push    word [ms_client]
        mov     [ms_client], dx
        mov     [ms_client+2], es
        mov     [ms_client_mask], cx
        pusha
        mov     al, EV_INFO
        mov     ah, INFO_SETCB
        mov     bx, cx
        mov     cx, dx
        mov     dx, es
        call    log_event
        call    install_cb16
        popa
        pop     dx                      ; old handler offset
        pop     es                      ; old handler segment
        pop     cx                      ; old mask
        pop     ds
        iret

.motion:
        pushf
        call    far [cs:old_int33]      ; CX/DX = mickeys since the last call
        test    byte [cs:test_ev_flag], 2
        jz      .mret
        xor     cx, cx
        xchg    cx, [cs:test_mickey_x]
        xor     dx, dx
        xchg    dx, [cs:test_mickey_y]
.mret:  iret

.status:
        pushf
        call    far [cs:old_int33]      ; BL buttons, BH wheel, CX, DX
        push    ds
        push    cs
        pop     ds
        inc     word [cnt_int33]
        test    byte [test_ev_flag], 2
        jz      .stnot
        mov     cx, [test_ev_x]
        mov     dx, [test_ev_y]
        mov     bl, [test_ev_buttons]
.stnot: mov     [ms_x], cx
        mov     [ms_y], dx
        and     bl, 7
        mov     [ms_buttons], bl
        mov     byte [ms_polled], 1
        call    game_buttons16
        xor     bh, bh
        pop     ds
        iret

.counts:
        ; in: BX = button index; out: AX = state, BX = count, CX/DX = position
        push    ax                      ; [bp+6] function (5 = presses, 6 = releases)
        push    bx                      ; [bp+4] button index
        pushf
        call    far [cs:old_int33]
        push    ds                      ; [bp+2]
        push    bp
        mov     bp, sp
        push    cs
        pop     ds
        inc     word [cnt_int33]
        push    si
        test    byte [test_ev_flag], 2
        jz      .cnot
        mov     si, [bp+4]
        cmp     si, 2
        ja      .cnot
        mov     cx, [test_ev_x]
        mov     dx, [test_ev_y]
        xor     bx, bx
        cmp     byte [bp+6], 5
        jne     .ctrel
        xchg    bl, [test_press+si]
        jmp     .cnot
.ctrel: xchg    bl, [test_release+si]
.cnot:  push    bx                      ; driver's count
        call    game_buttons16
        movzx   ax, bl                  ; button state as the game sees it
        pop     bx
        mov     si, [bp+4]
        cmp     si, 2
        ja      .cdone                  ; wheel (FFFFh) or unknown index
        push    ax
        push    cx
        mov     cx, si
        mov     al, 1
        shl     al, cl
        mov     ah, al
        call    eff_capture16
        test    al, ah
        pop     cx
        pop     ax
        jz      .csyn
        xor     bx, bx                  ; hidden button: physical count dropped
.csyn:  push    ax
        xor     ax, ax
        cmp     byte [bp+6], 5
        jne     .crel
        xchg    al, [ms_spress+si]
        jmp     .cadd
.crel:  xchg    al, [ms_srelease+si]
.cadd:  add     bx, ax
        pop     ax
.cdone: pop     si
        pop     bp
        pop     ds
        add     sp, 4
        iret

; ---------------------------------------------------------------------------
; Our event handler (far call from the driver): AL = event mask, BL =
; buttons, BH = wheel, CX/DX = position, SI/DI = mickeys
; ---------------------------------------------------------------------------
mouse_cb:
        push    ds
        push    cs
        pop     ds
        inc     word [cnt_mouse_cb]
        test    byte [test_ev_flag], 3  ; bit 0: one event, bit 1: all events
        jz      .real
        mov     al, 01h                 ; sticky: motion at the test position
        xor     bh, bh
        test    byte [test_ev_flag], 1
        jz      .stick
        and     byte [test_ev_flag], ~1
        mov     al, [test_ev_mask]
        mov     bh, [test_ev_wheel]
        test    al, 02h
        jz      .t1
        inc     byte [test_press]
.t1:    test    al, 04h
        jz      .t2
        inc     byte [test_release]
.t2:    test    al, 08h
        jz      .t3
        inc     byte [test_press+1]
.t3:    test    al, 10h
        jz      .t4
        inc     byte [test_release+1]
.t4:    test    al, 20h
        jz      .t5
        inc     byte [test_press+2]
.t5:    test    al, 40h
        jz      .stick
        inc     byte [test_release+2]
.stick: mov     bl, [test_ev_buttons]
        mov     cx, [test_ev_x]
        mov     dx, [test_ev_y]
        xor     si, si
        xor     di, di
.real:  pusha
        mov     [ms_x], cx
        mov     [ms_y], dx
        mov     ah, al
        mov     al, EV_MOUSE
        call    log_event
        and     bl, 7
        mov     [ms_buttons], bl
        popa
        pusha
        ; events the game gets: hidden buttons' presses/releases removed
        mov     ah, al
        call    eff_capture16           ; al = hidden buttons
        and     ah, 7Fh
        test    al, 1
        jz      .h2
        and     ah, ~06h
.h2:    test    al, 2
        jz      .h3
        and     ah, ~18h
.h3:    test    al, 4
        jz      .h4
        and     ah, ~60h
.h4:    mov     al, ah
        xor     ah, ah
        call    game_buttons16
        xor     bh, bh
        call    call_client16           ; SI/DI = the driver's mickeys
        call    drain16
        popa
        pop     ds
        retf

; ---------------------------------------------------------------------------
; INT 8 (timer)
; ---------------------------------------------------------------------------
int8_hook:
        pushf
        call    far [cs:old_int8]
        push    ds
        push    cs
        pop     ds
        inc     word [cnt_int8]
        push    ax
        call    inj_watchdog
        call    dac_service
        call    vram_service
        cmp     byte [ms_need_cb], 0
        je      .nocb
        mov     byte [ms_need_cb], 0
        call    install_cb16
.nocb:  call    rtc_maintain
        call    drain16
        cmp     byte [cfg_rmkbd], 0
        je      .noi9
        call    rm_int9_maintain
.noi9:  pop     ax
        pop     ds
        iret

; rm_int9_maintain: put our filter in front of a real-mode game's INT 9
; handler when the host asks for it (int9_req = the game's vector; the host
; knows which memory belongs to the game), and notice when the vector stops
; pointing at us.  Runs with IF=0, so the check and the swap are atomic.
rm_int9_maintain:
        push    es
        push    bx
        push    cx
        push    dx
        xor     bx, bx
        mov     es, bx
        mov     cx, [es:9*4]
        mov     dx, [es:9*4+2]
        cmp     dword [chain_int9], 0
        je      .req
        mov     bx, cs
        cmp     dx, bx
        jne     .gone
        cmp     cx, int9_hook
        je      .req
.gone:  mov     dword [chain_int9], 0   ; restored or replaced by the game
        mov     byte [kbd_active], 0
        mov     ax, INFO_INT9_OFF*256+EV_INFO
        call    .log
.req:   cmp     dword [int9_req], 0
        je      .out
        cmp     dword [chain_int9], 0
        jne     .clr
        cmp     cx, [int9_req]
        jne     .clr
        cmp     dx, [int9_req+2]
        jne     .clr
        mov     [chain_int9], cx
        mov     [chain_int9+2], dx
        mov     word [es:9*4], int9_hook
        mov     [es:9*4+2], cs
        mov     byte [kbd_prefix], 0
        mov     byte [kbd_e1_left], 0
        mov     byte [kbd_active], 1
        mov     ax, INFO_INT9_ON*256+EV_INFO
        call    .log
.clr:   mov     dword [int9_req], 0
.out:   pop     dx
        pop     cx
        pop     bx
        pop     es
        ret
.log:   push    bx
        xor     bx, bx
        call    log_event
        pop     bx
        ret

; ---------------------------------------------------------------------------
; INT 70h (RTC periodic interrupt)
; ---------------------------------------------------------------------------
int70_hook:
        pushf
        call    far [cs:old_int70]      ; BIOS: acknowledge, EOI, INT 15h 86h count
        push    ds
        push    cs
        pop     ds
        inc     word [cnt_int70]
        cmp     byte [host_alive], 0    ; the host went away: pass everything
        je      .dead
        dec     byte [host_alive]
        jmp     .alive
.dead:  mov     byte [cfg_enabled], 0
        mov     byte [cfg_capture], 0
        mov     byte [cfg_drop_pause], 0
.alive: o32 sidt [sys_idtr]
        o32 sgdt [sys_gdtr]
        push    ax
        smsw    ax
        mov     [sys_msw], ax
        call    inj_watchdog
        call    dac_service
        call    vram_service
        call    rtc_maintain
        call    drain16
        cmp     byte [cfg_rmkbd], 0
        je      .noi9
        call    rm_int9_maintain
.noi9:  pop     ax
        pop     ds
        iret

; rtc_maintain: keep the periodic interrupt on (the BIOS switches it off when
; an INT 15h 86h wait ends) at 64 Hz, or at 1024 Hz while a BIOS wait counts
; on that rate.  Clobbers AL.
rtc_maintain:
        cmp     byte [rtc_wanted], 0
        je      .r
        push    es
        push    bx
        xor     bx, bx
        mov     es, bx
        mov     bl, RTC_RATE_SLOW
        cmp     byte [es:BIOS_WAIT_ACTIVE], 0
        je      .rate
        mov     bl, RTC_RATE_BIOS
.rate:  mov     al, 0Ah
        out     70h, al
        in      al, 71h
        and     al, 7Fh
        cmp     al, bl
        je      .pie
        mov     al, 0Ah
        out     70h, al
        mov     al, bl
        out     71h, al
.pie:   mov     al, 0Bh
        out     70h, al
        in      al, 71h
        test    al, 40h
        jnz     .mask
        or      al, 40h
        mov     bl, al
        mov     al, 0Bh
        out     70h, al
        mov     al, bl
        out     71h, al
        mov     al, 0Ch                 ; acknowledge anything pending
        out     70h, al
        in      al, 71h
.mask:  in      al, 0A1h                ; IRQ 8 on the slave PIC
        test    al, 1
        jz      .m2
        and     al, 0FEh
        out     0A1h, al
.m2:    in      al, 21h                 ; cascade (IRQ 2) on the master
        test    al, 4
        jz      .done
        and     al, 0FBh
        out     21h, al
.done:  pop     bx
        pop     es
.r:     ret

; ---------------------------------------------------------------------------
; INT 9 (real-mode games)
; ---------------------------------------------------------------------------
int9_hook:
        push    ax
        push    ds
        push    cs
        pop     ds
        inc     word [cnt_kbd]
        in      al, 60h
        call    classify16
        jc      .drop
        pop     ds
        pop     ax
        pushf
        call    far [cs:chain_int9]
        push    ds
        push    cs
        pop     ds
        call    nl_restore16
        call    drain16
        pop     ds
        iret
.drop:  mov     al, 20h
        out     20h, al
        pop     ds
        pop     ax
        iret

; ===========================================================================
; Protected mode (DOS/4GW games).  CS/DS flat; EBX = block linear base.
; ===========================================================================
        bits    32

; pm_base: EBX = linear address of the block, DS = the game's data selector
%macro PM_SETUP 1
        call    %%ip
%%ip:   pop     ebx
        sub     ebx, %%ip
        mov     eax, [cs:ebx+pm_ds_store]
        mov     ds, [cs:eax]
%endmacro

pm_kbd_entry:
        push    ds
        push    eax
        push    ebx
        PM_SETUP 0
        inc     word [ebx+cnt_pm_kbd]
        in      al, 60h
        call    pm_classify
        jc      .drop
        pushfd                          ; the original handler's IRETD comes back
        call    far [cs:ebx+pm_chain]
        PM_SETUP 0
        cmp     byte [ebx+inj_nl_restore], 0
        je      .nonl
        cmp     byte [ebx+inj_inflight], 0
        jne     .nonl
        or      byte [BIOS_KBD_FLAGS], 20h
        mov     byte [ebx+inj_nl_restore], 0
.nonl:  call    pm_drain
        pop     ebx
        pop     eax
        pop     ds
        iretd
.drop:  mov     al, 20h
        out     20h, al
        pop     ebx
        pop     eax
        pop     ds
        iretd

; pm_log: AL=type AH=a, ECX = b|c<<8 (low 16), EDX = cx|dx<<16
pm_log:
        push    esi
        push    edi
        movzx   esi, word [ebx+ev_head]
        mov     edi, esi
        inc     edi
        and     edi, EV_COUNT-1
        cmp     di, [ebx+ev_tail]
        je      .full
        mov     [ebx+ev_buf+esi*8], ax
        mov     [ebx+ev_buf+esi*8+2], cx
        mov     [ebx+ev_buf+esi*8+4], edx
        mov     [ebx+ev_head], di
        pop     edi
        pop     esi
        ret
.full:  inc     word [ebx+cnt_ev_lost]
        pop     edi
        pop     esi
        ret

pm_read_pit:
        mov     al, 0
        out     43h, al
        in      al, 40h
        mov     ah, al
        in      al, 40h
        xchg    al, ah
        ret

pm_kbc_safe:
        push    eax
        push    ecx
        push    edx
        push    esi
        in      al, 64h
        test    al, 1
        jnz     .unsafe
        cmp     byte [ebx+kbd_prefix], 0
        jne     .unsafe
        cmp     byte [ebx+kbd_e1_left], 0
        jne     .unsafe
        call    pm_read_pit
        movzx   esi, ax                 ; previous count
        xor     edx, edx                ; accumulated
        mov     ecx, PIT_LOOPS
.loop:  in      al, 64h
        test    al, 1
        jnz     .unsafe
        call    pm_read_pit
        movzx   eax, ax
        cmp     eax, esi
        ja      .wrap
        sub     esi, eax
        add     edx, esi
        jmp     .next
.wrap:  add     edx, esi
.next:  mov     esi, eax
        cmp     edx, PIT_WAIT
        jae     .safe
        dec     ecx
        jnz     .loop
.safe:  clc
        jmp     .out
.unsafe:
        stc
.out:   pop     esi
        pop     edx
        pop     ecx
        pop     eax
        ret

pm_kbc_inject:                          ; AL = byte
        push    eax
        push    ecx
        mov     ah, al
        and     ah, 7Fh
        cmp     ah, 47h
        jb      .nonl
        cmp     ah, 53h
        ja      .nonl
        test    byte [BIOS_KBD_FLAGS], 20h
        jz      .nonl
        and     byte [BIOS_KBD_FLAGS], ~20h
        mov     byte [ebx+inj_nl_restore], 1
.nonl:
        mov     ah, al
        mov     ecx, 1000
.w1:    in      al, 64h
        test    al, 2
        jz      .s1
        dec     ecx
        jnz     .w1
.s1:    mov     al, 0D2h
        out     64h, al
        mov     ecx, 1000
.w2:    in      al, 64h
        test    al, 2
        jz      .s2
        dec     ecx
        jnz     .w2
.s2:    mov     al, ah
        out     60h, al
        pop     ecx
        pop     eax
        ret

; pm_drain: keys only (button events need real mode); one byte per call
pm_drain:
        cmp     byte [ebx+busy], 0
        jne     .ret
        mov     byte [ebx+busy], 1
        push    eax
        push    esi
        movzx   esi, byte [ebx+pq_tail]  ; follow-up bytes go first
        mov     al, [ebx+pq_head]
        cmp     al, [ebx+pq_tail]
        je      .host
        cmp     byte [ebx+inj_inflight], 0
        jne     .done
        call    pm_kbc_safe
        jc      .done
        mov     al, [ebx+pq_buf+esi]
        call    pm_kbc_inject
        mov     [ebx+inj_byte], al
        mov     byte [ebx+inj_inflight], 1
        mov     byte [ebx+inj_ticks], 0
        inc     word [ebx+cnt_inject]
        mov     eax, esi
        inc     eax
        and     al, 7
        mov     [ebx+pq_tail], al
        jmp     .done
.host:  movzx   esi, word [ebx+cq_tail]
        cmp     si, [ebx+cq_head]
        je      .done
        mov     al, [ebx+cq_buf+esi*4]
        cmp     al, CQ_KEY
        jne     .done
        cmp     byte [ebx+kbd_active], 0
        je      .done
        cmp     byte [ebx+inj_inflight], 0
        jne     .done
        call    pm_kbc_safe
        jc      .done
        mov     al, [ebx+cq_buf+esi*4+1]
        call    pm_kbc_inject
        mov     [ebx+inj_byte], al
        mov     byte [ebx+inj_inflight], 1
        mov     byte [ebx+inj_ticks], 0
        inc     word [ebx+cnt_inject]
        inc     esi
        and     esi, CQ_COUNT-1
        mov     [ebx+cq_tail], si
.done:  pop     esi
        pop     eax
        mov     byte [ebx+busy], 0
.ret:   ret

; pm_follow: AH = make code; queue its follow-ups (see follow16)
pm_follow:
        push    eax
        push    ecx
        push    esi
        xor     esi, esi
.l:     mov     al, [ebx+kbd_follow+esi]
        test    al, al
        jz      .n
        cmp     al, ah
        jne     .n
        mov     al, [ebx+kbd_follow+esi+1]
        call    pm_pq_push
        or      al, 80h
        call    pm_pq_push
.n:     add     esi, 2
        cmp     esi, 16
        jb      .l
        pop     esi
        pop     ecx
        pop     eax
        ret

pm_pq_push:                             ; AL = byte
        push    ecx
        push    edx
        movzx   ecx, byte [ebx+pq_head]
        mov     edx, ecx
        inc     edx
        and     edx, 7
        cmp     dl, [ebx+pq_tail]
        je      .full
        mov     [ebx+pq_buf+ecx], al
        mov     [ebx+pq_head], dl
.full:  pop     edx
        pop     ecx
        ret

; pm_classify: same rules as classify16.  AL = byte, CF=1 to swallow it.
pm_classify:
        push    eax
        push    ecx
        push    edx
        push    esi
        mov     ah, al                  ; ah = byte
        xor     dl, dl                  ; dl = flags
        cmp     byte [ebx+inj_inflight], 0
        je      .noinj
        cmp     al, [ebx+inj_byte]
        jne     .noinj
        mov     byte [ebx+inj_inflight], 0
        or      dl, KF_INJECTED
.noinj: cmp     byte [ebx+kbd_e1_left], 0
        je      .noe1
        dec     byte [ebx+kbd_e1_left]
        or      dl, KF_E1PART
        cmp     byte [ebx+kbd_e1_drop], 0
        je      .pass
        jmp     .drop
.noe1:  cmp     ah, 0E1h
        jne     .note1
        mov     byte [ebx+kbd_e1_left], 5
        mov     byte [ebx+kbd_e1_drop], 0
        test    dl, KF_INJECTED
        jnz     .pass
        mov     al, [ebx+cfg_drop_pause]
        mov     [ebx+kbd_e1_drop], al
        test    al, al
        jz      .pass
        jmp     .drop
.note1: cmp     ah, 0E0h
        jne     .note0
        mov     [ebx+kbd_prefix], ah
        jmp     .pass
.note0: mov     dh, [ebx+kbd_prefix]
        mov     byte [ebx+kbd_prefix], 0
        test    dh, dh
        jz      .plain
        or      dl, KF_EXTENDED
        jmp     .pass
.plain: movzx   esi, ah
        and     esi, 7Fh                ; esi = code
        mov     cl, 1
        cmp     esi, 2Ah
        je      .shift
        mov     cl, 2
        cmp     esi, 36h
        jne     .other
.shift: test    ah, 80h
        jnz     .shup
        or      [ebx+kbd_shift], cl
        jmp     .pass
.shup:  not     cl
        and     [ebx+kbd_shift], cl
        jmp     .pass
.other: mov     ecx, esi
        and     cl, 7
        mov     ch, 1
        shl     ch, cl                  ; ch = bit mask
        shr     esi, 3                  ; esi = byte index
        test    ah, 80h
        jnz     .break
        test    dl, KF_INJECTED
        jnz     .mkpass
        cmp     byte [ebx+cfg_enabled], 0
        je      .mkpass
        mov     al, [ebx+kbd_drop_plain+esi]
        cmp     byte [ebx+kbd_shift], 0
        je      .tbl
        mov     al, [ebx+kbd_drop_shift+esi]
.tbl:   test    al, ch
        jz      .mkpass
        or      [ebx+kbd_dropped+esi], ch
        jmp     .drop
.mkpass:
        not     ch
        and     [ebx+kbd_dropped+esi], ch
        test    dl, KF_INJECTED
        jnz     .pass
        cmp     byte [ebx+cfg_enabled], 0
        je      .pass
        call    pm_follow               ; AH = the make code
        jmp     .pass
.break: test    [ebx+kbd_dropped+esi], ch
        jz      .pass
        not     ch
        and     [ebx+kbd_dropped+esi], ch
        test    dl, KF_INJECTED
        jnz     .pass
        jmp     .drop
.pass:  call    .log
        clc
        jmp     .out
.drop:  or      dl, KF_DROPPED
        call    .log
        stc
.out:   pop     esi
        pop     edx
        pop     ecx
        pop     eax
        ret
.log:   push    eax
        push    ecx
        push    edx
        mov     al, EV_KEY              ; ah = byte
        movzx   ecx, dl                 ; b = flags
        mov     ch, [ebx+kbd_shift]     ; c = shift state
        xor     edx, edx
        call    pm_log
        pop     edx
        pop     ecx
        pop     eax
        ret

block_end:
