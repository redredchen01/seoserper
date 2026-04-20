"""2026-07-19 sunset tripwire for the Suggest-only pivot.

Rationale: the dormant Unit 3 / Unit 5 code paths (seoserper/core/render.py +
the SERP branch of engine.py) are kept in-tree behind ENABLE_SERP_RENDER for
90 days to allow for a potential network change + reactivation. Without a
concrete enforcement artifact, the pivot drifts into silent permanence (see
ce-brainstorm + ce-plan adversarial-review finding ADV-2 / F2).

On 2026-07-19 this assertion fails and the CI suite turns red. The correct
response is one of:

  1. Flag has been flipped and full mode is in active use → delete this
     test and extend no further; the infrastructure earned its keep.
  2. Flag has stayed False and nothing is blocking deletion → remove
     seoserper/core/render.py + tests/test_render.py + the SERP branch of
     engine.py + this test.
  3. User still wants optionality → bump the SUNSET_DATE below by another
     90 days and record the rationale in MEMORY.md.
"""

from __future__ import annotations

from datetime import date


SUNSET_DATE = date(2026, 7, 19)


def test_dormant_code_sunset_not_reached() -> None:
    today = date.today()
    assert today < SUNSET_DATE, (
        f"Dormant Unit 3 / Unit 5 sunset reached ({SUNSET_DATE}). "
        "Delete the dormant code, flip the flag into active use, or extend "
        "SUNSET_DATE in tests/test_sunset.py — see the module docstring."
    )
