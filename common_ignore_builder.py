"""
common_ignore_builder.py — Scan repo expectedJSON files to build commonIgnore presets.
"""

import glob
import json
import os
from collections import Counter


def build_standard_ignore_list(project_root: str) -> list:
    """
    Glob all expectedJSON files in the repo, count commonIgnore field occurrences,
    return the top 10 most-used values.
    """
    counter = Counter()
    pattern = os.path.join(project_root, "fileFromJson", "expectedJSON", "*.json")
    for filepath in glob.glob(pattern):
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                entries = json.load(fh)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    counter.update(entry.get("commonIgnore", []))
        except Exception:
            continue
    return [field for field, _ in counter.most_common(10)]


def get_presets(project_root: str) -> dict:
    """
    Return all commonIgnore presets.

    Keys:
      standard_ignore_list — top 10 auto-extracted from repo
      no_commonignore       — empty (no fields ignored)
    """
    standard_ignore_list = build_standard_ignore_list(project_root)

    # Fallback if repo scan returns nothing (e.g. no read access)
    if not standard_ignore_list:
        standard_ignore_list = [
            "createdAt",
            "updatedAt",
            "createdDate",
            "lastModifiedDate",
            "version",
            "createdBy",
            "lastModifiedBy",
        ]

    return {
        "standard_ignore_list": standard_ignore_list,
        "no_commonignore": [],
    }
