#!/usr/bin/env python3
"""UserPromptSubmit hook wrapper for the drowsiness-framed context tracker."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from context_tracker import estimate_usage, format_drowsiness

usage = estimate_usage()
if usage:
    msg = format_drowsiness(usage)
    if msg:
        print(msg)
