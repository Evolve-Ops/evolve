#!/usr/bin/env bash
# repro-acl-mask-clobber.sh — fast on-box reproduction of the W10-G round-6 bug.
#
# THE BUG (Linux only): a `chmod 600` (or any chmod that zeroes a file's group
# bits) on a file that carries a POSIX ACL ZEROES the ACL **mask**, which caps
# every named-user ACE to `#effective:---`. So after the secret-config hardening
# (`chmod_secret_config` → chmod 600) runs on `auth-profiles.json`, evolve's
# inherited read ACE is masked out and admin daemons hit EACCES — exactly the
# `pod_perms_drift_monitor: Permission denied: .../auth-profiles.json` round-6
# failure. The FIX is to re-run `setfacl -m u:evolve:r <file>` after the chmod
# (setfacl recomputes the mask). This script demonstrates the mechanism AND the
# fix against the live kernel + the real `evolve` user, in <1s and with no user
# creation — the fast inner loop that root-caused the bug.
#
# This is a MECHANISM repro, not the product test. The product code path
# (`deploy.set_evolve_read_acl` → `secret_config_perms.chmod_secret_config` +
# the `verify_evolve_access` backstop) is covered by:
#   - packages/admin/tests/test_w10g_daemon_layer.py (unit, seam-injected)
#   - packages/admin/tests/e2e_linux/test_ubuntu_e2e.py::test_step4g (live kernel)
#
# Usage:  sudo tools/repro-acl-mask-clobber.sh [evolve-user]   (default: evolve)
set -euo pipefail

EVOLVE_USER="${1:-evolve}"
SETFACL=/usr/bin/setfacl
GETFACL=/usr/bin/getfacl

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "SKIP: the ACL-mask sharp edge is Linux-only (macOS ACLs have no mask)." >&2
  exit 0
fi
if [[ "$(id -u)" != "0" ]]; then
  echo "ERROR: run as root (needs setfacl + read-as-another-user)." >&2
  exit 2
fi
if ! id "$EVOLVE_USER" >/dev/null 2>&1; then
  echo "ERROR: user '$EVOLVE_USER' does not exist on this host." >&2
  exit 2
fi

# /tmp is world-traversable, so this isolates the FILE mask (not parent traverse).
work="$(mktemp -d /tmp/acl-mask-repro.XXXXXX)"
chmod 711 "$work"
secret="$work/auth-profiles.json"
printf '{"profiles":{}}\n' > "$secret"
trap 'rm -rf "$work"' EXIT

mask_of() { "$GETFACL" -p "$1" 2>/dev/null | sed -n 's/^mask::\(.*\)$/\1/p'; }
reads_ok() { sudo -u "$EVOLVE_USER" cat "$1" >/dev/null 2>&1; }

fail=0

# 1) Grant evolve the inherited read ACE (what set_evolve_read_acl does).
"$SETFACL" -m "u:${EVOLVE_USER}:r" "$secret"
echo "[1] after grant:        mask=$(mask_of "$secret")   evolve-read=$(reads_ok "$secret" && echo OK || echo FAIL)"
reads_ok "$secret" || { echo "    UNEXPECTED: evolve cannot read right after the grant"; fail=1; }

# 2) The hardening chmod 600 — clobbers the mask (THE BUG).
chmod 600 "$secret"
m="$(mask_of "$secret")"
echo "[2] after chmod 600:    mask=$m   evolve-read=$(reads_ok "$secret" && echo OK || echo FAIL)"
[[ "$m" == "---" ]] || { echo "    UNEXPECTED: chmod 600 did not zero the mask (got '$m')"; fail=1; }

# 3) The fix — re-setfacl recomputes the mask (what chmod_secret_config does).
"$SETFACL" -m "u:${EVOLVE_USER}:r" "$secret"
m="$(mask_of "$secret")"
echo "[3] after re-setfacl:   mask=$m   evolve-read=$(reads_ok "$secret" && echo OK || echo FAIL)"
{ [[ "$m" == *r* ]] && reads_ok "$secret"; } || { echo "    UNEXPECTED: the re-grant did not restore evolve read"; fail=1; }

echo
if [[ "$fail" == 0 ]]; then
  echo "PASS: reproduced the mask clobber on chmod 600 AND the setfacl re-grant fix."
else
  echo "FAIL: the kernel did not behave as the round-6 root cause predicts (see above)."
fi
exit "$fail"
