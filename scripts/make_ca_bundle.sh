#!/usr/bin/env bash
# Build a CA bundle from certifi + the macOS system/login keychains.
# Needed when a proxy root lives in the system keychain: curl trusts it,
# Python's certifi does not. Do not work around this by disabling verification.
set -euo pipefail
out="${1:-.certs/bundle.pem}"
mkdir -p "$(dirname "$out")"
{
  python3 -c 'import certifi; print(open(certifi.where()).read())'
  for kc in /Library/Keychains/System.keychain "$HOME/Library/Keychains/login.keychain-db"; do
    security find-certificate -a -p "$kc" 2>/dev/null || true
  done
  cat /etc/ssl/cert.pem 2>/dev/null || true
} > "$out"
echo "wrote $out ($(grep -c 'BEGIN CERT' "$out") certificates)"
echo "export MI_CA_BUNDLE=$(pwd)/$out"
echo "export MI_CA_BUNDLE=$(pwd)/$out"

