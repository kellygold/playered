"""Initial single-plate placement; the actual Bambu slice remains the final gate."""

from __future__ import annotations

import math


def single_plate_placement(
    width_mm: float,
    height_mm: float,
    *,
    color_count: int,
    layer_height_mm: float,
) -> tuple[float, float, tuple[float, float] | None]:
    """Keep artwork centered when possible and reserve rear-left space for priming.

    Bambu's default y=220 puts larger multi-color towers outside a 256 mm bed.
    This is a conservative initial reserve for the supported PLA profiles, not
    a prediction of the generated tower. Slicing must still check collisions
    and bed bounds; changed filament/profile behavior must never bypass those.
    """
    if (
        not all(math.isfinite(v) and v > 0 for v in (width_mm, height_mm, layer_height_mm))
        or max(width_mm, height_mm) > 256
        or color_count < 1
    ):
        raise ValueError("Artwork must fit the 256 mm bed and use a valid print profile.")
    centered = ((256 - width_mm) / 2, (256 - height_mm) / 2)
    if color_count == 1:
        return *centered, None

    # 45 mm³ priming per change, 150% spacing, plus ribs, brim and slack.
    # The reserve grows for more colors and finer layers. Actual placement is
    # checked by Bambu, including profiles whose defaults differ from these.
    reserve = max(44, math.ceil(math.sqrt(45 * (color_count - 1) / layer_height_mm * 1.5) + 18))
    tower_left, tower_bottom = 1.0, 255.0 - reserve
    tower = (tower_left + 3, tower_bottom + 3)
    gap = 2.0
    x, y = centered
    if y + height_mm <= tower_bottom - gap or x >= tower_left + reserve + gap:
        return x, y, tower

    # Translate only, preserving image size, orientation and all mesh vertices.
    candidates = []
    available = 254 - reserve - gap
    if height_mm <= available and width_mm <= 254:
        candidates.append((x, 1 + (available - height_mm) / 2))
    if width_mm <= available and height_mm <= 254:
        candidates.append((tower_left + reserve + gap + (available - width_mm) / 2, y))
    if not candidates:
        raise ValueError(
            "The artwork leaves no room for a prime tower at these color/layer settings. "
            "Reduce the canvas size or use mural tiles."
        )
    x, y = min(candidates, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
    return x, y, tower
