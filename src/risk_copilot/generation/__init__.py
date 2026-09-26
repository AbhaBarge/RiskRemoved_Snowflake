"""Phase 2 -- synthetic data generation over the real AMLworld graph.

Generation never invents the transaction graph. It reads IBM AMLworld's
ledger, accounts and laundering patterns, samples a connected subgraph, and
then *dresses* the preserved keys (GAP-02). Every module here is a pure
function of the source files, ``conf/`` and the master seed, so the same inputs
give byte-identical output.

Pipeline order::

    source / patterns  ->  graph  ->  reskin + labels  ->  entities + relationships
                       ->  inject + eval_grain + demo_entity  ->  validate + writer
"""

from __future__ import annotations


class GenerationError(RuntimeError):
    """Raised when the source data or the configuration breaks a generation contract.

    Always fatal. A contract that fails quietly produces a dataset that loads
    and then answers every evaluation question wrongly, which is worse than no
    dataset at all.
    """
