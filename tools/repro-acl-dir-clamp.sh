#!/usr/bin/env bash
# repro-acl-dir-clamp.sh — fast on-box reproduction of the W10-G round-8 bugs.
#
# THE BUGS (Linux only), the two facets the round-7 fix missed because they hit
# AFTER the deploy-time grant, once the OC gateway runs:
#
#   (1) DIR-MODE CLAMP. A `chmod 700` on a DIRECTORY that carries a POSIX ACL
#       (e.g. the OC gateway re-hardening ~/.openclaw on startup) zeroes the
#       directory's ACL **mask**, capping evolve's inherited `rX` traverse ACE to
#       `#effective:---`. evolve can no longer traverse .openclaw → the
#       defer-runner / manifest-reflex-runner hit EACCES on workspace/evolve.
#       This is the dir-level cousin of the round-6 chmod-600-on-a-FILE clamp.
#
#   (2) DEFAULT-ACL INHERITANCE. A file the gateway creates UNDER .openclaw after
#       the grant inherits evolve's read ACE ONLY if .openclaw carries a *default*
#       ACL — and even then a 0700/0600 creation clamps the child's own mask. The
#       FIX for both: re-run `setfacl -R -m u:evolve:rX .openclaw` (+ `-R -d`) AFTER
#       the gateway has run — the recursive grant recomputes every clamped child
#       mask AND re-plants the default ACL. This is exactly what
#       `deploy.set_evolve_read_acl` does, now re-asserted post-gateway.
#
# This is a MECHANISM repro, not the product test. The product code path
# (set_evolve_read_acl → the ensure_pod_perms `check_evolve_access` drift check +
# the post-gateway re-assert + probe in setup_wizard) is covered by:
#   - packages/admin/tests/test_w10g_daemon_layer.py (unit, seam-injected)
#   - packages/admin/tests/e2e_linux/test_ubuntu_e2e.py::test_step4h (live kernel)
#
# Usage:  sudo tools/repro-acl-dir-clamp.sh [evolve-user]   (default: evolve)
set -euo pipefail

EVOLVE_USER="${1:-evolve}"
SETFACL=/usr/bin/setfacl
GETFACL=/usr/bin/getfacl

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "SKIP: the ACL-mask sharp edge is Linux-only (macOS ACLs have no mask)." >&2
  exit 0
fi
if [[ "$(id -u)" != "0" ]]; then
  echo "ERROR: run as root (needs setfacl + traverse-as-another-user)." >&2
  exit 2
fi
if ! id "$EVOLVE_USER" >/dev/null 2>&1; then
  echo "ERROR: user '$EVOLVE_USER' does not exist on this host." >&2
  exit 2
fi

work="$(mktemp -d /tmp/acl-dir-clamp.XXXXXX)"
chmod 711 "$work"            # /tmp is world-traversable; isolate the DIR mask
oc="$work/.openclaw"
mkdir -p "$oc"
trap 'rm -rf "$work"' EXIT

mask_of() { "$GETFACL" -p "$1" 2>/dev/null | sed -n 's/^mask::\(.*\)$/\1/p'; }
has_default() { "$GETFACL" -p "$1" 2>/dev/null | grep -q '^default:user:'"$EVOLVE_USER"':'; }
traverses() { sudo -u "$EVOLVE_USER" /bin/sh -c "cd '$1'" >/dev/null 2>&1; }

fail=0

# 1) Grant the recursive read contract (access ACE + default ACL) — set_evolve_read_acl.
"$SETFACL" -R -m "u:${EVOLVE_USER}:rX" "$oc"
"$SETFACL" -R -d -m "u:${EVOLVE_USER}:rX" "$oc"
echo "[1] after grant:       mask=$(mask_of "$oc")  default-ace=$(has_default "$oc" && echo yes || echo NO)  traverse=$(traverses "$oc" && echo OK || echo FAIL)"
traverses "$oc" || { echo "    UNEXPECTED: evolve cannot traverse right after the grant"; fail=1; }
has_default "$oc" || { echo "    UNEXPECTED: no default ACL after the -R -d grant"; fail=1; }

# 2) The gateway re-hardens .openclaw to 0700 — clamps the DIR mask (BUG 1).
chmod 700 "$oc"
m="$(mask_of "$oc")"
echo "[2] after chmod 700:   mask=$m  traverse=$(traverses "$oc" && echo OK || echo FAIL)"
[[ "$m" == "---" ]] || { echo "    UNEXPECTED: chmod 700 did not zero the dir mask (got '$m')"; fail=1; }

# 3) The gateway creates a fresh child AFTER the grant; the 0700 dir clamps it too.
child="$oc/auth-profiles.json"
( umask 077; printf '{}\n' > "$child" )
echo "[3] new child mask:    mask=$(mask_of "$child")  evolve-read=$(sudo -u "$EVOLVE_USER" cat "$child" >/dev/null 2>&1 && echo OK || echo FAIL)"

# 4) The fix — re-run the recursive grant (post-gateway): recomputes every mask.
"$SETFACL" -R -m "u:${EVOLVE_USER}:rX" "$oc"
"$SETFACL" -R -d -m "u:${EVOLVE_USER}:rX" "$oc"
m="$(mask_of "$oc")"
echo "[4] after re-assert:   mask=$m  traverse=$(traverses "$oc" && echo OK || echo FAIL)  child-read=$(sudo -u "$EVOLVE_USER" cat "$child" >/dev/null 2>&1 && echo OK || echo FAIL)"
{ [[ "$m" != "---" ]] && traverses "$oc" && sudo -u "$EVOLVE_USER" cat "$child" >/dev/null 2>&1; } \
  || { echo "    UNEXPECTED: the post-gateway re-assert did not restore evolve access"; fail=1; }

echo
if [[ "$fail" == 0 ]]; then
  echo "PASS: reproduced the dir-mode mask clamp + post-grant child, AND the recursive re-assert fix."
else
  echo "FAIL: the kernel did not behave as the round-8 root cause predicts (see above)."
fi
exit "$fail"
