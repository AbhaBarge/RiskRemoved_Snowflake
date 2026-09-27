"""Per-key attribute draws shared across generation modules.

Every categorical attribute is a pure function of ``(key, seed, namespace)``:
:func:`risk_copilot.ids.derived_rng` seeds a private stream per entity, and
:func:`risk_copilot.ids.weighted_choice` consumes exactly one value from it.
An attribute therefore never depends on how many other entities were drawn or
in what order, and two modules that need the same attribute -- the sampler and
the entity builder both need an account's type -- get the same answer by
calling the same function.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from risk_copilot.generation import GenerationError
from risk_copilot.ids import derived_rng, weighted_choice


def weighted_options(spec: Any, what: str) -> list[tuple[str, float]]:
    """Normalise a config distribution to ``[(code, weight), ...]``, config order.

    Accepts either a list of ``{code, weight}`` mappings (``account_types``) or a
    ``code -> weight`` mapping (``risk_rating_distribution``).
    """
    if isinstance(spec, Mapping):
        options = [(str(k), float(v)) for k, v in spec.items()]
    elif isinstance(spec, Sequence) and not isinstance(spec, str):
        try:
            options = [(str(o["code"]), float(o["weight"])) for o in spec]
        except (KeyError, TypeError) as exc:
            raise GenerationError(f"{what}: entries need 'code' and 'weight'") from exc
    else:
        raise GenerationError(f"{what}: expected a list or mapping, got {type(spec).__name__}")
    if not options or any(w < 0 for _, w in options) or sum(w for _, w in options) <= 0:
        raise GenerationError(f"{what}: weights must be non-negative and sum to more than 0")
    return options


def draw(key: object, *, seed: int, namespace: str, options: Sequence[tuple[str, float]]) -> str:
    """One weighted draw from a stream private to ``(key, namespace)``."""
    return weighted_choice(derived_rng(key, seed=seed, namespace=namespace), options)


def account_type(account_id_value: str, generation: Mapping[str, Any]) -> str:
    """The account type of ``account_id_value`` (``entities.account_types``)."""
    options = weighted_options(generation["entities"]["account_types"], "entities.account_types")
    return draw(
        account_id_value, seed=int(generation["seed"]), namespace="account_type", options=options
    )
