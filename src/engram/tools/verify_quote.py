#!/usr/bin/env python3
"""Pre-verify that a phrase exists in an evidence file before calling engram_add_observation.

Usage:
    verify_quote.py <file_path> <search_phrase>

Searches for the phrase as a substring of the file content. If found, prints
the surrounding context so you can copy the exact verbatim text for quoted_text.
If not found, reports why (file empty, phrase absent, possible encoding issues).

For JSONL session transcripts, user messages are JSON-encoded — this script
searches the raw file content including JSON escaping, so straight quotes
and escaped characters will match naturally.
"""
import sys
import os

def main():
    if len(sys.argv) < 3:
        print("Usage: verify_quote.py <file_path> <search_phrase>")
        print("Example: verify_quote.py ~/.claude/projects/.../session.jsonl 'lost of identity'")
        sys.exit(1)

    file_path = os.path.expanduser(sys.argv[1])
    search = sys.argv[2]

    if not os.path.exists(file_path):
        print(f"FILE NOT FOUND: {file_path}")
        sys.exit(1)

    with open(file_path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    if not content.strip():
        print("FILE IS EMPTY — messages may not have been flushed yet.")
        print(f"File size: {os.path.getsize(file_path)} bytes")
        sys.exit(1)

    if search in content:
        idx = content.find(search)
        # Show generous context around the match
        start = max(0, idx - 150)
        end = min(len(content), idx + len(search) + 200)
        context = content[start:end]
        print(f"FOUND at position {idx}:")
        print(f"---")
        print(context)
        print(f"---")
        print(f"\nTip: Copy the exact text between the quotes for quoted_text.")
    else:
        # Check file stats to help diagnose
        lines = content.strip().split('\n')
        print(f"NOT FOUND in {os.path.basename(file_path)}")
        print(f"  File lines: {len(lines)}")
        print(f"  File size:  {os.path.getsize(file_path)} bytes")

        # Check if this looks like a JSONL with only metadata (not flushed)
        if file_path.endswith('.jsonl') and len(lines) < 10:
            print(f"\n  LIKELY CAUSE: Session JSONL has only {len(lines)} lines —")
            print(f"  messages probably haven't been flushed yet.")
            print(f"  Wait for more conversation turns, or cite a different source.")
        else:
            # Try case-insensitive search
            lower_content = content.lower()
            lower_search = search.lower()
            if lower_search in lower_content:
                idx = lower_content.find(lower_search)
                print(f"\n  Case-insensitive match at position {idx}:")
                print(f"  {repr(content[max(0,idx-50):idx+len(search)+50])}")
            else:
                # Show last bit of file for orientation
                print(f"\n  Last 200 chars of file:")
                print(f"  {repr(content[-200:])}")

if __name__ == "__main__":
    main()
