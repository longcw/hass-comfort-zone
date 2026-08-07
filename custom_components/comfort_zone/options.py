"""How a zone's stored options become the options the controller runs on.

A mode is a saved profile, not a template: it owns a set of knobs, and tuning one
while the mode is active edits that mode and stays there. Switch away and back and
the edits are still in force, the same way the target curve has always been keyed
by mode. Everything else — the safety rails, the setpoint limits, the night window
— belongs to the zone and reads the same in every mode.

``STRATEGY_PRESETS`` is the factory seed a mode starts from, consulted only for the
knobs a mode has never had tuned. A mode with no preset simply starts at the
defaults.

Kept free of Home Assistant so the rule can be tested off-server; the entity
platforms only carry the result to the config entry and the zone store.
"""
from __future__ import annotations

from .const import OPT_STRATEGY, OPTION_DEFAULTS, STRATEGY_PRESETS

# The knobs a mode owns. Deliberately not every knob: the hard safety bounds and
# the AC's setpoint limits are properties of the room and the unit, and a mode
# switch has no business moving them.
MODE_KEYS = frozenset(k for preset in STRATEGY_PRESETS.values() for k in preset)


def resolve(stored: dict, saved_knobs: dict) -> dict:
    """Effective options for the mode named in ``stored``.

    ``saved_knobs`` is what the user has tuned in that mode; the mode's preset
    seeds whatever they have not.
    """
    strategy = stored.get(OPT_STRATEGY, OPTION_DEFAULTS[OPT_STRATEGY])
    opts = dict(OPTION_DEFAULTS)
    opts.update(STRATEGY_PRESETS.get(strategy, {}))
    opts.update(saved_knobs)
    # Zone-wide settings win, but they may not reach into the mode's own knobs —
    # a stale copy left in the entry would shadow the mode for good.
    opts.update({k: v for k, v in stored.items() if k not in MODE_KEYS})
    opts[OPT_STRATEGY] = strategy
    return opts


def split_modal(stored: dict) -> tuple[dict, dict]:
    """Separate an entry's zone-wide options from the active mode's knobs.

    Entries written before modes were profiles keep every knob in one flat bag,
    where a tuned value shadowed every mode at once. Returns what stays on the
    entry and what belongs to the mode it names.
    """
    zone = {k: v for k, v in stored.items() if k not in MODE_KEYS}
    knobs = {k: v for k, v in stored.items() if k in MODE_KEYS}
    return zone, knobs
