# Tiny-hole and hollow-ring correction

Linear issue: `24K-46`

Preview derivation v7 introduced a hash-verified `hole-analysis` JSON artifact bound to the exact
region graph. The classifier identifies truly enclosed topology and remains read-only. Review,
keep, fill, explicit recolor, and local ring collapse are separate, explicit policy operations with
changed-pixel and lineage evidence.

## Enclosure topology

An active center is hole-like only when it is four-connected, does not touch the canvas, touches
exactly one surrounding region, and its label matches a representative region outside that
surround. This distinguishes a cream counter inside a dark ring on cream from an ordinary isolated
dark dot on cream. Open centers connect to the outside and are not holes; a diagonal corner alone
does not create a four-connected opening.

Transparent pixels receive their own stable, content-addressed four-connected void components.
Canvas-connected transparency is outside; a transparent component touching exactly one active
surround is an enclosed cutout. The serialized graph cannot represent transparent components as
regions, so full reconstruction uses the private assignment/active plane while reopen validation
checks every persisted active-region reference and physical bound.

Area and equivalent circular diameter are independent physical triggers. Equality is retained:
only a value below a configured threshold becomes actionable. Large and threshold-equal counters
are preserved.

## Hollow-ring safety

A small enclosed center becomes a `hollow_ring` only when its surrounding wall is off the canvas,
has an outside, and its measured local wall is wide enough to remain. Wall width is sampled on the
four-connected inner boundary using exact horizontal/vertical run spans.

Collapse never targets an entire connected palette region. The engine derives a bounded local shell
from the center and measured wall, records its pixel/physical bounds and area ratio, and changes only
ring pixels inside that shell. Excessively large shells are downgraded to `tiny_hole`; shells that
contain another known center are also downgraded, and overlapping destructive bulk requests are
rejected atomically. This matters on the Wager golf ball, where many dimples can touch a larger dark
line network.

## Explicit correction policies

- `review` and `keep` change no pixels.
- `fill_hole` replaces/activates the center with its surrounding ring label.
- `recolor_center` activates the center with an explicitly selected declared palette label.
- `collapse_ring` replaces the bounded local shell with the active center label, or makes that shell
  transparent when the enclosed center is transparent.

Every request validates all features, target labels, feature kinds, and overlap before mutation.
The result contains the exact active plane and exhaustive labels, before/after graph fingerprints,
per-feature decisions, changed-pixel totals, and region lineage.

## Evidence and verification

At the 1024×683, 200 mm-wide Wager panel scale, the reference run found 942 enclosed features (904
tiny holes and 38 safely local hollow rings) in about 0.44 seconds; the artifact was about 1.0 MB.
The native golf-ball regression crop exposes more than 100 physical center decisions and at least
one bounded collapse candidate without modifying a source label byte.

Tests cover solid dots, open and enclosed centers, exact threshold equality, large holes, wall
survival, local-shell bounds, multiple centers, overlap rejection, transparent holes, fill,
recolor, collapse, review/keep, invalid targets, changed pixels, lineage, deterministic
reconstruction, graph validation, risk actions, and the private Wager golf-ball crop. The API flow
proves hole-analysis publication, exact download, complete taxonomy coverage, legacy nullability,
supersession safety, and process-restart recovery.
