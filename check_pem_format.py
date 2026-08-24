"""
Local-only diagnostic for the KALSHI_PRIVATE_KEY escaping discrepancy.
Never sends the key anywhere. Prints only shape/format info, never the key itself.
"""

import os
from cryptography.hazmat.primitives import serialization

raw = os.getenv("KALSHI_PRIVATE_KEY", "")

if not raw:
    print("KALSHI_PRIVATE_KEY is not set in this environment.")
    raise SystemExit(1)

print(f"Raw length: {len(raw)} characters")
print(f"Contains literal backslash-n sequence: {'\\\
' in raw}")
print(f"Contains real newline characters: {chr(10) in raw}")
print(f"Starts with '-----BEGIN': {raw.strip().startswith('-----BEGIN')}")

# Step 1: what kalshi_client.py's _load_private_key does today
converted = raw.replace("\\\
", "
").strip()
print(f"\
After .replace('\\\\\\\
', newline): still has literal backslash-n: {'\\\
' in converted}")

# Step 2: try to actually parse it, the same way kalshi_client.py does
try:
    key = serialization.load_pem_private_key(converted.encode("utf-8"), password=None)
    print("\
RESULT: PEM parsed successfully. Your key format is compatible with kalshi_client.py as written.")
    print(f"Key type: {type(key).__name__}")
except Exception as error:
    print(f"\
RESULT: PEM FAILED TO PARSE. This confirms the discrepancy.")
    print(f"Error: {error}")
    print("\
Likely cause: your key is stored in a format other than PEM-with-escaped-newlines,")
    print("e.g. base64-encoded, or a single-line PEM missing internal newlines entirely.")