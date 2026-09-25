; HEADER_BLOCK_START
; BambuStudio 02.07.01.62
; total layer number: 3
; total filament length [mm] : 100.00,50.00,25.00
; total filament volume [cm^3] : 240.53,120.26,60.13
; total filament weight [g] : 0.32,0.16,0.08
; max_z_height: 0.60
; filament: 1,2,3
; HEADER_BLOCK_END
; CONFIG_BLOCK_START
; filament_colour = #CBC6B8;#F99963;#0078BF
; CONFIG_BLOCK_END
G90
M83
T0
; CHANGE_LAYER
; Z_HEIGHT: 0.2
; FEATURE: Outer wall
G1 X10 Y20 E0.20
G1 X30 Y20 E0.20
; FEATURE: Prime tower
G1 X200 Y200 E0.20
; CP TOOLCHANGE START
; toolchange #1
T1
; CP TOOLCHANGE END
; CHANGE_LAYER
; Z_HEIGHT: 0.4
; FEATURE: Top surface
G1 X30 Y40 E0.20
; CP TOOLCHANGE START
; toolchange #2
T2
; CP TOOLCHANGE END
; CHANGE_LAYER
; Z_HEIGHT: 0.6
; FEATURE: Top surface
G1 X10 Y40 E0.20

