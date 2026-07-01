"""
Automatic transaction classification rule applier.

Reads the rule list from auto_rules.json and, when a rule matches a row (dict),
fills in the set values.
- match: all keys/values must match the row
  - strings use 'contains' matching (partial match, case-insensitive)
  - numbers use exact matching
- sources: which sources to apply among ['paypay', 'amazon', 'mcdonald', 'receipt', 'manual'] (all if omitted)
- set: values to overwrite on the row (but only fills fields that are empty)
"""
import json
import os

from .config import user_path

RULE_FILE = user_path("auto_rules.json")


def _load_rules():
    if not os.path.exists(RULE_FILE):
        return []
    try:
        with open(RULE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[auto_rules] load error: {e}")
        return []


def _match_one(rule_match, row):
    for k, v in rule_match.items():
        rv = row.get(k)
        if rv is None:
            return False
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                if float(rv) != float(v):
                    return False
            except (TypeError, ValueError):
                return False
        else:
            if str(v).lower() not in str(rv).lower():
                return False
    return True


def apply(row, source=None):
    """Match a row (dict) against the rules and auto-fill empty fields. Modifies the row in-place and returns it."""
    rules = _load_rules()
    for rule in rules:
        srcs = rule.get('sources')
        if srcs and source and source not in srcs:
            continue
        if not _match_one(rule.get('match', {}), row):
            continue
        for k, v in rule.get('set', {}).items():
            cur = row.get(k)
            if cur in (None, '', '(세부내역 없음)'):
                row[k] = v
        break  # apply only the first match
    return row
