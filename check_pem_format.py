"""
Local-only diagnostic for the KALSHI_PRIVATE_KEY escaping discrepancy.
Never sends the key anywhere. Prints only shape/format info, never the key itself.

Written with chr() escapes throughout so copy/paste through tools that mangle
backslash sequences cannot corrupt it (the same failure once broke risk.py).
"""

import os

from cryptography.hazmat.primitives import serialization

NEWLINE = chr(10)
BACKSLASH_N = chr(92) + "n"

raw = os.getenv("KALSHI_PRIVATE_KEY", "")

if not raw:
    print("KALSHI_PRIVATE_KEY is not set in this environment.")
    raise SystemExit(1)

print(f"Raw length: {len(raw)} characters")
print(f"Contains literal backslash-n sequence: {BACKSLASH_N in raw}")
print(f"Contains real newline characters: {NEWLINE in raw}")
print(f"Starts with '-----BEGIN': {raw.strip().startswith('-----BEGIN')}")

# Step 1: what kalshi_client.py's _load_private_key does today
converted = raw.replace(BACKSLASH_N, NEWLINE).strip()
print(NEWLINE + "After replacing backslash-n with real newlines:")
print(f"  still has literal backslash-n: {BACKSLASH_N in converted}")

# Step 2: try to actually parse it, the same way kalshi_client.py does
try:
    key = serialization.load_pem_private_key(converted.encode("utf-8"), password=None)
except Exception as error:
    print(NEWLINE + "RESULT: PEM FAILED TO PARSE. This confirms the discrepancy.")
    print(f"Error: {error}")
    print(NEWLINE + "Likely cause: the key is stored in a format other than")
    print("PEM-with-escaped-newlines, e.g. base64-encoded, or a single-line")
    print("PEM missing internal newlines entirely.")
    raise SystemExit(1)

print(NEWLINE + "RESULT: PEM parsed successfully. Your key format is compatible")
print("with kalshi_client.py as written.")
print(f"Key type: {type(key).__name__}")
