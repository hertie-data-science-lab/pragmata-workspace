#!/usr/bin/env python3
"""Deep-merge YAML files and print the result to stdout.

Used by `run_querygen.sh` to compose `configs/annotation/querygen_specs/_runtime.yaml` with
each per-spec YAML before passing the result to `pragmata querygen gen-queries
--config-path`. Reuses pragmata's own `deep_merge`, so the merged file behaves identically
to what pragmata's layered config resolution would produce. Files merge left-to-right:
each subsequent file overrides keys in the running result.

Usage:
    python scripts/annotation/merge_yaml.py runtime.yaml spec.yaml > merged.yaml
"""

import sys
from pathlib import Path

import yaml
from pragmata.core.settings.settings_base import deep_merge

merged: dict = {}
for path in sys.argv[1:]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        sys.exit(f"{path}: YAML root must be a mapping, got {type(data).__name__}")
    merged = deep_merge(merged, data)

yaml.safe_dump(merged, sys.stdout, sort_keys=False)
