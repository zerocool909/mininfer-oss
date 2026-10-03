#!/usr/bin/env bash
# Build a CA bundle from certifi plus the OS trust stores.
#
# Needed only when a TLS-intercepting proxy's root CA lives in the OS store and
# not in certifi — the case where `curl` works and Python does not. **A normal
# network does not need this**: certifi already trusts every public CA, so a
# fresh checkout talks to providers with no configuration.
#
# The output is machine-specific by nature (it contains *your* network's
# interception root), which is why `.certs/` is gitignored. Do not commit it, and
# do not copy it to another machine — it would make Python there trust a root
# that machine never agreed to. Do not work around a failure by disabling
# verification.
#
# Usage:  ./scripts/make_ca_bundle.sh [output-path]
set -euo pipefail

out="${1:-.certs/bundle.pem}"
mkdir -p "$(dirname "$out")"

# Certifi first: portable, and the fallback if every OS store is empty.
{
  python3 -c 'import certifi; print(open(certifi.where()).read())' 2>/dev/null || true

  # Whatever the caller already trusts, if they pointed at a bundle.
  for var in SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE; do
    f="${!var:-}"
    [ -n "$f" ] && [ -f "$f" ] && cat "$f"
  done

  case "$(uname -s)" in
    Darwin)
      # macOS keeps roots — including a corporate/MITM root — in the keychains.
      for kc in /Library/Keychains/System.keychain \
                "$HOME/Library/Keychains/login.keychain-db"; do
        security find-certificate -a -p "$kc" 2>/dev/null || true
      done
      cat /etc/ssl/cert.pem 2>/dev/null || true
      ;;
    Linux)
      # Distro-dependent paths; whichever exists contributes its roots.
      for f in /etc/ssl/certs/ca-certificates.crt \
               /etc/pki/tls/certs/ca-bundle.crt \
               /etc/ssl/ca-bundle.pem \
               /etc/ssl/cert.pem; do
        [ -f "$f" ] && cat "$f"
      done
      ;;
    *)
      # BSD/other: try the common locations and let an empty result fall through
      # to certifi alone rather than failing the build.
      for f in /etc/ssl/cert.pem /usr/local/etc/ssl/cert.pem; do
        [ -f "$f" ] && cat "$f"
      done
      ;;
  esac
} > "$out"

count=$(grep -c 'BEGIN CERT' "$out" || true)
if [ "${count:-0}" -eq 0 ]; then
  echo "error: $out has no certificates (is python3 + certifi installed?)" >&2
  exit 1
fi

# Resolve to an absolute path so the suggestion is correct whether $out was given
# as a relative path or an absolute one.
abs="$(cd "$(dirname "$out")" && pwd)/$(basename "$out")"

echo "wrote $out ($count certificates)"
echo
echo "Point MinInfer at it — or export SSL_CERT_FILE, which httpx honours too:"
echo "  export MI_CA_BUNDLE=$abs"
