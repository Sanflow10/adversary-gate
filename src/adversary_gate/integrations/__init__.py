"""Integrations: things the gate talks to, kept outside the decision path.

Nothing here can turn ``INCONCLUSIVE`` or ``BLOCK`` into ``MERGE``. A triage
model may only make the gate *stricter*; an agent may only say *what* to judge,
never *how strictly*.
"""
