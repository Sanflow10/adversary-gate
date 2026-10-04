"""AdversaryGate: an evidence-based, fail-closed verification gate.

Everything lives under this one package (AG-027). Until 2.3.0 the wheel
installed ``cli``, ``core``, ``sandbox`` and ``verifiers`` as top-level
modules, and the gate runs inside the project's own environment -- so a
project with a ``core`` package of its own shadowed, or was shadowed by, the
gate's.
"""

__version__ = "2.3.0"
