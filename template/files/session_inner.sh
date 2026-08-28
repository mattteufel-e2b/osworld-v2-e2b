#!/usr/bin/env bash
# Runs as `user` INSIDE a PAM/systemd-logind graphical session (opened by the
# outer start script via `su - user` with XDG_SEAT/XDG_SESSION_TYPE). Owns the
# whole GNOME graphical session on Xvfb, then execs the OSWorld Flask server on
# :5000 as the foreground process (so the E2B waitForPort ready check + snapshot
# capture the live desktop).
set -u

export HOME=/home/user
export USER=user
export DISPLAY=:0
export XAUTHORITY=/home/user/.Xauthority
# This template requests no GPU, so mutter composites via llvmpipe.
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export XDG_SESSION_TYPE=x11
export GDK_BACKEND=x11
export CLUTTER_BACKEND=x11
export GTK_MODULES=gail:atk-bridge
export QT_ACCESSIBILITY=1
export NO_AT_BRIDGE=0
export OOO_FORCE_DESKTOP=gnome
export PYTHONUNBUFFERED=1

LOG=/tmp/osworld
mkdir -p "$LOG"

# systemd-logind creates /run/user/1000 (owned by user) for a real session.
# OSWorld's own agents hardcode unix:path=/run/user/1000/bus, so prefer it.
if [ -d /run/user/1000 ] && [ -w /run/user/1000 ]; then
    export XDG_RUNTIME_DIR=/run/user/1000
else
    export XDG_RUNTIME_DIR=/tmp/xdg-runtime-1000
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
fi
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" >"$LOG/session-env.log"
loginctl session-status >"$LOG/loginctl.log" 2>&1 || true

touch "$XAUTHORITY"

# ---- Xvfb :0 (1920x1080x24) --------------------------------------------
/usr/bin/Xvfb :0 -screen 0 1920x1080x24 -ac \
    +extension GLX +extension RANDR +extension XTEST +render -noreset \
    >"$LOG/xvfb.log" 2>&1 &
for _ in $(seq 1 30); do
    if xdpyinfo -display :0 >/dev/null 2>&1; then break; fi
    sleep 0.5
done

# Seed an Xauthority cookie. Xvfb -ac disables access control, but Xlib clients
# still emit "Xlib.xauth: warning, no xauthority details available" to stdout
# when the Xauthority file is empty - that noise pollutes OSWorld's
# get_vm_platform()/get_vm_machine() probes and breaks platform-gated getters.
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(mcookie 2>/dev/null || echo 00000000000000000000000000000000)" 2>/dev/null || true

# ---- session D-Bus at a fixed path -------------------------------------
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
if [ ! -S "$XDG_RUNTIME_DIR/bus" ]; then
    /usr/bin/dbus-daemon --session \
        --address="$DBUS_SESSION_BUS_ADDRESS" \
        --nofork --nopidfile --syslog-only >"$LOG/dbus.log" 2>&1 &
    for _ in $(seq 1 20); do
        [ -S "$XDG_RUNTIME_DIR/bus" ] && break
        sleep 0.2
    done
fi

# D-Bus-activated desktop services (notably xdg-desktop-portal-gtk) inherit
# their environment from the user service manager, not from this shell. Give
# them the live X11 session variables so application startup cannot stall for
# the D-Bus activation timeout while the portal fails with "cannot open
# display".
dbus-update-activation-environment --systemd \
    DISPLAY XAUTHORITY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS \
    XDG_SESSION_TYPE GDK_BACKEND 2>"$LOG/dbus-environment.log" || true

# ---- PulseAudio userspace sound + virtual sink ------------------------------
# The E2B guest kernel ships no ALSA device (no /proc/asound, snd-dummy absent),
# so start PulseAudio in the user session with a null sink as the default output.
# OSWorld 2.0's audio tasks (REAPER/MuseScore/Shotcut) render/export audio files
# offline and never query live sink state, so a virtual sink is sufficient for
# the apps to complete an export. Evidence: out/osworld-v2-evidence/spike-audio.json.
pulseaudio --start --exit-idle-time=-1 >"$LOG/pulseaudio.log" 2>&1 || true
for _ in $(seq 1 20); do
    pactl info >/dev/null 2>&1 && break
    sleep 0.2
done
pactl load-module module-null-sink sink_name=vsink >>"$LOG/pulseaudio.log" 2>&1 || true
pactl set-default-sink vsink >>"$LOG/pulseaudio.log" 2>&1 || true

# ---- AT-SPI accessibility bus + registry -------------------------------
if [ -x /usr/libexec/at-spi-bus-launcher ]; then
    /usr/libexec/at-spi-bus-launcher --launch-immediately >"$LOG/atspi-bus.log" 2>&1 &
    sleep 1
    [ -x /usr/libexec/at-spi2-registryd ] && \
        /usr/libexec/at-spi2-registryd >"$LOG/atspi-reg.log" 2>&1 &
fi

# ---- GNOME shell + the relevant gnome-settings-daemon suite -------------
# Headless gnome-session hangs waiting on systemd's graphical-session.target on
# Xvfb, so we bring up gnome-shell as the WM/compositor plus the available
# gsd-* daemons directly. This is a headless GNOME reconstruction, not an exact
# clone of every service in the reference qcow2 login session.
/usr/bin/gnome-shell --x11 --replace >"$LOG/gnome-shell.log" 2>&1 &

# The gnome-settings-daemon components installed in Ubuntu 22.04.
for gsd in gsd-xsettings gsd-a11y-settings gsd-keyboard gsd-media-keys \
           gsd-sound gsd-power gsd-color gsd-datetime gsd-housekeeping \
           gsd-print-notifications gsd-rfkill gsd-screensaver-proxy \
           gsd-sharing gsd-smartcard gsd-wacom; do
    [ -x "/usr/libexec/$gsd" ] && "/usr/libexec/$gsd" >"$LOG/$gsd.log" 2>&1 &
done

# nautilus-desktop paints desktop icons like the reference GNOME session.
nautilus-desktop >"$LOG/nautilus-desktop.log" 2>&1 &

# Wait for GNOME Shell to actually own the session instead of a fixed sleep:
# mutter publishes _NET_SUPPORTING_WM_CHECK once the compositor is up, which
# `wmctrl -m` reads. llvmpipe compositing under build-host load can exceed any
# fixed delay, and a half-ready shell captured at snapshot time would poison
# every sandbox from the build. Cap at 30s; on timeout continue with a logged
# warning so the server still comes up for debugging.
shell_ready=0
for _ in $(seq 1 60); do
    if wmctrl -m >/dev/null 2>&1; then shell_ready=1; break; fi
    sleep 0.5
done
[ "$shell_ready" -eq 1 ] || echo "WARNING: gnome-shell/WM not detected after 30s; continuing" >>"$LOG/gnome-shell.log"
# Brief settle for gsd-* + nautilus-desktop + top-bar paint after the WM is up.
sleep 2

# GNOME opens Activities Overview on a first session with no windows. Capture
# the template in the neutral desktop state expected at the start of a task.
xdotool key Escape 2>/dev/null || true
sleep 1

# Pin the PDF handler without opening any visible application. The template's
# captured initial state must be neutral and repeatable for every task.
xdg-mime default org.gnome.Evince.desktop application/pdf 2>/dev/null || true

# ---- OSWorld 2.0 guest server (foreground) -----------------------------
# V2's main.py imports src.server and runs its FastAPI/uvicorn ASGI app on
# :5000 (no reloader), so hark's V1 serve.py no-reloader shim is unnecessary.
# Running the script from its own dir puts /opt/osworld-server on sys.path[0]
# so `import src.server` resolves.
cd /opt/osworld-server
exec /usr/bin/python3 /opt/osworld-server/main.py >"$LOG/server.log" 2>&1
