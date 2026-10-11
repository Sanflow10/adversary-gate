# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Sanflow (Sanflow10). Additional terms: see NOTICE (AGPL section 7).

"""``python -m adversary_gate`` -- the same entry point as the console script."""

import sys

from adversary_gate.cli import main

sys.exit(main())
