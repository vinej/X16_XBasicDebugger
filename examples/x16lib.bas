' =====================================================================
' x16lib.bas -- XBasic (XC=BASIC 3) wrappers for the x16_library (DASM)
' =====================================================================
' Thin SUB/FUNCTION wrappers that set up the library's A/X/Y + X16_P*
' calling convention and JSR into the routines. Inline asm uses {name}
' substitution to read each STATIC parameter from its fixed address.
'
' USAGE -- paste, don't INCLUDE. XC=BASIC resolves INCLUDE too late in
' its compile for cross-file SUB/FUNCTION calls (a CALL to a sub defined
' in an INCLUDEd file errors "Unknown identifier"), so these wrappers
' must live in the SAME .bas file as the code that calls them. Copy the
' ones you need to the top of your program, add ONE asm block that
' INCLUDEs "x16_code.asm" AFTER your END, and you are set. examples/
' bounce.bas is a complete worked example with these inlined.
'
' This file is the reference catalogue of the wrappers; it also compiles
' on its own (as a program that just calls x16cls) so it stays correct.
'
' The library scratch ZP is relocated to $70 to clear XC=BASIC's
' pseudo-registers ($22-$34) and FAST-variable window; keep FAST vars
' below $70 (this demo uses none). The X16 is a 65C02, which the
' debug-info fork now targets, so the library's trb/tsb/stz assemble.
' =====================================================================

asm
X16_ZP = $70
X16_USE_VERA    = 1
X16_USE_SCREEN  = 1
X16_USE_PALETTE = 1
X16_USE_SPRITE  = 1
X16_USE_TILE    = 1
X16_USE_IRQ     = 1
X16_USE_INPUT   = 1
X16_USE_PSG     = 1
X16_USE_YM      = 1
    INCDIR "C:/quartus/projects/x16_library/src_dasm"
    INCLUDE "x16.asm"
end asm

' --- screen ----------------------------------------------------------
SUB x16cls () STATIC
    asm
    jsr screen_cls
    end asm
END SUB

SUB x16locate (col AS BYTE, row AS BYTE) STATIC
    asm
    ldx {col}
    ldy {row}
    jsr screen_locate
    end asm
END SUB

SUB x16palset (idx AS BYTE, grb AS BYTE, r AS BYTE) STATIC
    asm
    ldx {idx}
    lda {grb}
    ldy {r}
    jsr pal_set
    end asm
END SUB

' --- sprites ---------------------------------------------------------
SUB x16spriteinit () STATIC
    asm
    jsr sprite_init_all
    end asm
END SUB

SUB x16spriteson () STATIC
    asm
    jsr sprites_on
    end asm
END SUB

SUB x16spritesoff () STATIC
    asm
    jsr sprites_off
    end asm
END SUB

' Point sprite `spr` at a 24-bit VRAM address, 8bpp.
SUB x16spriteimage (spr AS BYTE, vlo AS BYTE, vmid AS BYTE, vhi AS BYTE) STATIC
    asm
    lda {vlo}
    sta X16_P0
    lda {vmid}
    sta X16_P1
    lda {vhi}
    sta X16_P2
    ldx {spr}
    lda #SPRITE_MODE_8BPP
    jsr sprite_image
    end asm
END SUB

' 16x16 sprite, palette offset `paloff`.
SUB x16spritesize (spr AS BYTE, paloff AS BYTE) STATIC
    asm
    lda {paloff}
    sta X16_P0
    ldx {spr}
    lda #SPRITE_SIZE_16
    ldy #SPRITE_SIZE_16
    jsr sprite_size
    end asm
END SUB

SUB x16spritefront (spr AS BYTE) STATIC
    asm
    ldx {spr}
    lda #SPRITE_Z_FRONT
    jsr sprite_flags
    end asm
END SUB

' Move sprite `spr` to display pixel (x, y).
SUB x16spritepos (spr AS BYTE, x AS WORD, y AS WORD) STATIC
    asm
    lda {x}
    sta X16_P0
    lda {x}+1
    sta X16_P1
    lda {y}
    sta X16_P2
    lda {y}+1
    sta X16_P3
    ldx {spr}
    jsr sprite_pos
    end asm
END SUB

' Fill a 16x16 block of palette index `palidx` into the sprite image
' area at VRAM $13000 (the KERNAL sprite image slot).
SUB x16buildsprite (palidx AS BYTE) STATIC
    asm
    vera_addr 0, $13000, VERA_INC_1
    lda {palidx}
    ldx #<256
    ldy #>256
    jsr vera_fill
    end asm
END SUB

' --- tile map --------------------------------------------------------
SUB x16tileput (col AS BYTE, row AS BYTE, char AS BYTE, attr AS BYTE) STATIC
    asm
    lda {char}
    sta X16_P0
    lda {attr}
    sta X16_P1
    ldx {col}
    ldy {row}
    jsr tile_put
    end asm
END SUB

' --- irq / frame lock ------------------------------------------------
SUB x16irqinstall () STATIC
    asm
    jsr irq_install
    end asm
END SUB

SUB x16irqremove () STATIC
    asm
    jsr irq_remove
    end asm
END SUB

SUB x16vsync () STATIC
    asm
    jsr vsync_wait
    end asm
END SUB

' --- input -----------------------------------------------------------
' Non-blocking: returns the PETSCII code of a waiting key, or 0.
FUNCTION x16key AS BYTE () STATIC
    asm
    jsr key_get
    sta {x16key}
    end asm
END FUNCTION

' --- PSG (bounce blip) -----------------------------------------------
SUB x16psginit () STATIC
    asm
    jsr psg_init
    end asm
END SUB

SUB x16psgfreq (voc AS BYTE, freq AS WORD) STATIC
    asm
    lda {freq}
    sta X16_P0
    lda {freq}+1
    sta X16_P1
    ldx {voc}
    jsr psg_set_freq
    end asm
END SUB

' Square wave (pulse, 50% duty).
SUB x16psgsquare (voc AS BYTE) STATIC
    asm
    ldx {voc}
    lda #PSG_WAVE_PULSE
    ldy #32
    jsr psg_set_wave
    end asm
END SUB

SUB x16psgvol (voc AS BYTE, vol AS BYTE) STATIC
    asm
    lda {vol}
    ldx {voc}
    ldy #PSG_PAN_BOTH
    jsr psg_set_vol
    end asm
END SUB

' --- YM2151 FM (note while inside the box) ---------------------------
SUB x16yminit () STATIC
    asm
    jsr ym_init
    end asm
END SUB

SUB x16ympatch (chan AS BYTE, patch AS BYTE) STATIC
    asm
    sec
    lda {chan}
    ldx {patch}
    jsr ym_patch
    end asm
END SUB

SUB x16ymvol (chan AS BYTE, atten AS BYTE) STATIC
    asm
    lda {chan}
    ldx {atten}
    jsr ym_vol
    end asm
END SUB

SUB x16ympan (chan AS BYTE, pan AS BYTE) STATIC
    asm
    lda {chan}
    ldx {pan}
    jsr ym_pan
    end asm
END SUB

SUB x16ymnote (chan AS BYTE, note AS BYTE) STATIC
    asm
    clc
    lda {chan}
    ldx {note}
    jsr ym_note_bas
    end asm
END SUB

SUB x16ymrelease (chan AS BYTE) STATIC
    asm
    lda {chan}
    jsr ym_release_note
    end asm
END SUB
