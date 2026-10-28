"""pinbench: treat a model upgrade like a deploy.

Refuses to do: call a model without a priced id and a cost estimate under a cap.
"""

__version__ = "0.1.0"


def pin(slot: str, pins="pins.yaml") -> str:
    """Return the model id pinned for `slot` in `pins` (default: ./pins.yaml).

    This is the read side of `pinbench bump`: an app that takes its model id from here
    switches models when a bump PR merges, and never otherwise.
    """
    from . import registry

    return registry.load(pins).slot(slot).model
