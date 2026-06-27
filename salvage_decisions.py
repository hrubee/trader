#!/usr/bin/env python3
"""Salvage a decisions JSON from the agent's stdout when it PRINTED the decision instead of writing the
file. The brain sometimes describes its decision in its text reply rather than writing decisions.json;
without this, that decision is silently discarded. Reads stdin, finds the LAST balanced {...} block that
parses as JSON and looks like a decisions object (has 'entries' or 'manage'), writes it to argv[1].
Exit 0 on success, 1 if nothing salvageable."""
import sys, json


def _candidates(text):
    """Yield every top-level brace-balanced {...} substring (cheap, ignores quoting — good enough since we
    json.loads-validate each candidate anyway)."""
    out = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    out.append(text[start:i + 1])
                    start = None
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(2)
    text = sys.stdin.read()
    best = None
    for blk in _candidates(text):
        try:
            obj = json.loads(blk)
        except Exception:
            continue
        if isinstance(obj, dict) and ("entries" in obj or "manage" in obj):
            best = obj                      # keep the LAST decisions-shaped object
    if best is None:
        sys.exit(1)
    best.setdefault("market_view", "")
    best.setdefault("manage", [])
    best.setdefault("entries", [])
    with open(sys.argv[1], "w") as f:
        json.dump(best, f)
    print("salvaged decisions.json: %d entries / %d manage" % (len(best["entries"]), len(best["manage"])))


if __name__ == "__main__":
    main()
