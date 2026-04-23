#!/bin/bash
# Unit tests for init-secrets.sh helpers. No AWS calls.
#
# Runs the validate_hmac_b64 function from init-secrets.sh against known
# good and bad inputs. Run with: ./scripts/test-init-secrets.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_SCRIPT="$SCRIPT_DIR/init-secrets.sh"

# Extract just the validate_hmac_b64 function from the real script so tests
# exercise the same code path as production.
if ! grep -q "^validate_hmac_b64()" "$INIT_SCRIPT"; then
    echo "FAIL: validate_hmac_b64 function not found in $INIT_SCRIPT" >&2
    exit 1
fi

# Source the function definition only (bash doesn't have a clean way to do
# this short of sed-ing out the function block).
eval "$(awk '/^validate_hmac_b64\(\)/,/^}$/' "$INIT_SCRIPT")"

PASS=0
FAIL=0

assert_valid() {
    local label="$1"
    local value="$2"
    if validate_hmac_b64 "$value"; then
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $label — expected valid, got rejected" >&2
        FAIL=$((FAIL + 1))
    fi
}

assert_invalid() {
    local label="$1"
    local value="$2"
    if validate_hmac_b64 "$value"; then
        echo "  FAIL: $label — expected rejected, got accepted" >&2
        FAIL=$((FAIL + 1))
    else
        echo "  PASS: $label"
        PASS=$((PASS + 1))
    fi
}

echo "=== validate_hmac_b64 ==="

# Generate a real 32-byte random key, base64-encoded — what Daily actually
# returns as an HMAC secret.
REAL_KEY=$(head -c 32 /dev/urandom | base64)
assert_valid  "real 32-byte base64 HMAC" "$REAL_KEY"

# 16 bytes is our minimum acceptable length.
MIN_KEY=$(head -c 16 /dev/urandom | base64)
assert_valid  "16-byte base64 HMAC (boundary)" "$MIN_KEY"

# 15 bytes should be rejected — too short.
SHORT_KEY=$(head -c 15 /dev/urandom | base64)
assert_invalid "15-byte base64 HMAC (too short)" "$SHORT_KEY"

# The bug we're fixing: literal string "null" from jq -r on a missing field.
assert_invalid "literal string 'null'"   "null"
assert_invalid "literal string 'None'"   "None"

# Empty / whitespace / non-base64.
assert_invalid "empty string"            ""
assert_invalid "not-base64 string"       "!!!not base64!!!"

# Common Daily-response example format (32-byte key, 44 chars base64 with =).
DAILY_STYLE_KEY="AxQbeGLAbN032si3wVL43MW/ice5LAQbQmIh9137WGQ="
assert_valid   "Daily-style 44-char base64" "$DAILY_STYLE_KEY"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
exit $FAIL
