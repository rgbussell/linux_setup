#!/usr/bin/env bash
# SSH auth triage for a git host. Read-only: touches nothing, changes nothing.
#
#   ./ssh_triage.sh                 # defaults to bitbucket.org
#   ./ssh_triage.sh github.com
#
# Prints what SSH actually did, then a verdict naming the fix.

HOST="${1:-bitbucket.org}"

echo "=== keys present in ~/.ssh ==="
ls -1 ~/.ssh/*.pub 2>/dev/null || echo "  (none — you have not generated a key)"
echo
echo "=== ~/.ssh/config ==="
if [ -f ~/.ssh/config ]; then
  MODE=$(stat -c '%a' ~/.ssh/config)
  echo "  mode ${MODE}$([ "$MODE" = 600 ] || echo '  <-- should be 600; ssh ignores loose configs')"
  if grep -qiE "^[[:space:]]*Host[[:space:]].*(^|[[:space:]])${HOST}([[:space:]]|$)" ~/.ssh/config; then
    echo "  has a Host block for ${HOST}: yes"
  else
    echo "  has a Host block for ${HOST}: NO"
  fi
else
  echo "  (no ~/.ssh/config)"
fi
echo
echo "=== ssh-agent ==="
ssh-add -l 2>&1 | sed 's/^/  /'
echo
echo "=== connecting to ${HOST} ==="
OUT=$(timeout 25 ssh -vT -o BatchMode=yes -o StrictHostKeyChecking=accept-new "git@${HOST}" 2>&1)
echo "$OUT" | grep -E 'Connection established|Offering public key|Authentications that can continue|Permission denied|authenticated|successfully authenticated|Server host key|Bad owner' | sed 's/^/  /'
echo
echo "=== VERDICT ==="
if echo "$OUT" | grep -qiE 'authenticated via ssh key|successfully authenticated|logged in as'; then
  echo "  SSH AUTH WORKS."
  echo "  (GitHub exits non-zero here even on success — it provides no shell. Judge by the message.)"
  echo "  If git still fails, it is authorization (repo permissions) or a wrong remote URL."
elif ! echo "$OUT" | grep -q 'Connection established'; then
  echo "  NEVER REACHED THE SERVER — port 22 egress is probably blocked."
  echo "  Fix: use the host's 443 endpoint. For Bitbucket, add to ~/.ssh/config:"
  echo "      Host ${HOST}"
  echo "          Hostname altssh.bitbucket.org"
  echo "          Port 443"
  echo "          User git"
  echo "          IdentityFile ~/.ssh/<your-key>"
  echo "          IdentitiesOnly yes"
  echo "  GitHub's equivalent host is ssh.github.com on port 443."
elif ! echo "$OUT" | grep -q 'Offering public key'; then
  echo "  SSH NEVER OFFERED A KEY — client-side; the server never saw you."
  echo "  Cause: your key has a non-default filename and nothing points ssh at it."
  echo "  Fix (permanent): add a Host block naming it with IdentityFile, then chmod 600 ~/.ssh/config"
  echo "  Fix (this shell): ssh-add ~/.ssh/<your-key>"
else
  echo "  KEY WAS OFFERED AND THE SERVER REJECTED IT — the key is not on the account."
  echo "  Keys ssh actually offered:"
  echo "$OUT" | grep 'Offering public key' | sed 's/^/    /'
  echo "  Register the matching .pub in the host's account settings (personal SSH keys,"
  echo "  not a repository access key), then confirm the fingerprint shown matches:"
  for k in ~/.ssh/*.pub; do [ -f "$k" ] && ssh-keygen -lf "$k" 2>/dev/null | sed 's/^/    /'; done
fi
