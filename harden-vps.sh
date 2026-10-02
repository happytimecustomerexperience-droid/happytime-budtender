#!/usr/bin/env bash
# Happy Time — VPS hardening. Run by the OWNER on the VPS as root:   bash harden-vps.sh
# Idempotent. Undo: rm /etc/ssh/sshd_config.d/00-hardening.conf && systemctl reload ssh
#                   apt-get remove -y fail2ban
# It refuses to switch off password logins unless root already has an SSH key, so it cannot lock
# you out (Hostinger's browser terminal in hPanel keeps working either way).
set -euo pipefail

echo "==> 1/2 SSH: keys only"
[ -s /root/.ssh/authorized_keys ] || { echo "   No key in /root/.ssh/authorized_keys. Add one first; nothing changed."; exit 1; }
# sshd takes the FIRST value it reads, and 50-cloud-init.conf says "PasswordAuthentication yes",
# so this file is named 00- to be read before it.
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
EOF
sshd -t
systemctl reload ssh
sshd -T | grep -E "^(passwordauthentication|permitrootlogin) "

echo "==> 2/2 fail2ban: ban an IP after repeated failed SSH logins"
command -v fail2ban-client >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban; }
systemctl enable --now fail2ban
sleep 2
fail2ban-client status sshd

echo
echo "Done. Before closing this window, open a NEW terminal and confirm you can still log in:"
echo "  ssh -i ~/.ssh/hostinger_audit_packet_ed25519 root@2.24.116.142"
