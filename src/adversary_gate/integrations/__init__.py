# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

"""Integrations: things the gate talks to, kept outside the decision path.

Nothing here can turn ``INCONCLUSIVE`` or ``BLOCK`` into ``MERGE``. A triage
model may only make the gate *stricter*; an agent may only say *what* to judge,
never *how strictly*.
"""
