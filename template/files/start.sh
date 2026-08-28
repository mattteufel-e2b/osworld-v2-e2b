#!/usr/bin/env bash
# E2B start command (runs as root). GNOME Shell 42 hard-requires a
# systemd-logind graphical session (loginManager.getCurrentSessionProxy ->
# User.Display); without one it crashes in ScreenShield. E2B built from
# ubuntu:22.04 runs systemd as PID1 with a working logind, so we open a real
# PAM login session for `user` (pam_systemd registers it on seat0 as an x11
# session) and run the whole GNOME + OSWorld stack inside it.
set -u

LOG=/tmp/osworld
mkdir -p "$LOG"
chown -R user:user "$LOG" /home/user 2>/dev/null || true

# The V2 su/sudo credential for `user`. The build-time `chpasswd` in template.ts
# does not survive E2B's template finalize (verified: every other file in that
# same build layer persists, but /etc/shadow's password field comes back empty,
# so `passwd -S user` reports NP in a fresh sandbox). Set it here, as root, on
# every boot so each live sandbox actually authenticates the documented password
# `osworld-public-evaluation` (OSWorld 2.0's `echo <pw> | sudo -S ...` path).
echo 'user:osworld-public-evaluation' | chpasswd

# setsid + su - triggers pam_systemd; XDG_SEAT/XDG_SESSION_TYPE tell logind to
# mark this as the user's graphical (Display) session on seat0.
exec setsid su - user -c \
  'XDG_SEAT=seat0 XDG_VTNR=1 XDG_SESSION_TYPE=x11 exec bash /opt/osworld-server/session_inner.sh'
