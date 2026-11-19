#!/usr/bin/env python3
"""Notify the session's usage worker without changing a review-gate decision."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.usage.lifecycle import record_event

if __name__ == "__main__":
    try:
        print(json.dumps(record_event(json.load(sys.stdin))))
    except Exception:
        print(json.dumps({"systemMessage": "Usage cleanup could not be signaled; the worker still monitors session status"}))
