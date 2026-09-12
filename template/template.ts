import * as path from 'node:path'
import { fileURLToPath } from 'node:url'
import { Template, waitForPort } from 'e2b'

// OSWorld 2.0-oriented Ubuntu 22.04 GNOME guest for E2B.
//
// Ported from the proven OSWorld 1.0 reconstruction. This is a native
// reconstruction, not a bit-for-bit conversion of OSWorld's qcow2. It uses the
// same Ubuntu release, GNOME family, screen geometry, user, major applications,
// and OSWorld control interfaces. Relative to the V1 template it adds the V2
// guest server (FastAPI/uvicorn, xlang-ai/osworld-server), the V2 expanded
// application set (MuseScore 3, Shotcut, FreeCAD, Zotero, REAPER), and a
// userspace PulseAudio null sink for the V2 audio tasks. The V2 guest user
// password is `osworld-public-evaluation` (the V2 harness su/sudo credential).
// VS Code is pinned to the reference image's 1.91.1 via Microsoft's permanent
// versioned .deb URL.
// Chrome cannot be version-pinned the same way (Google's apt repo serves only
// the latest stable and does not archive old .debs), so it is apt-mark held:
// each immutable template build freezes whatever version it installed and the
// guest can never drift. Matching the qcow2's exact Chrome needs its version
// number plus an archived .deb source.

const filesDir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'files')
const UBUNTU_2204_IMAGE =
  'ubuntu:22.04@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc'

export const template = Template({ fileContextPath: filesDir })
  // Multi-platform OCI index pinned on 2026-08-27. E2B selects linux/amd64;
  // rebuilds cannot silently consume a newer ubuntu:22.04 publication.
  .fromImage(UBUNTU_2204_IMAGE)
  .setUser('root')
  .setWorkdir('/')
  .setEnvs({
    DEBIAN_FRONTEND: 'noninteractive',
    DEBIAN_PRIORITY: 'high',
    PIP_DISABLE_PIP_VERSION_CHECK: '1',
    PIP_NO_CACHE_DIR: '1',
    LANG: 'en_US.UTF-8',
    TZ: 'UTC',
  })
  .runCmd('apt-get update')
  // ---- desktop stack (GNOME) + software GL --------------------------------
  .aptInstall([
    'gnome-session',
    'gnome-shell',
    'gnome-terminal',
    'nautilus',
    'gnome-settings-daemon',
    'gnome-control-center',
    'gsettings-desktop-schemas',
    'dconf-cli',
    'accerciser',
    // themes + cursor (fixes mutter "No cursor theme available" + real rendering)
    'adwaita-icon-theme',
    'gnome-themes-extra',
    'dmz-cursor-theme',
    'libgl1-mesa-dri',
    'libglx-mesa0',
    'mesa-utils',
    'xvfb',
    'x11-utils',
    'x11-xserver-utils',
    // ---- session bus + accessibility --------------------------------------
    'dbus-x11',
    'dbus-user-session',
    'at-spi2-core',
    'python3-pyatspi',
    'python3-gi',
    'python3-tk',
    'python3-xlib',
    'python3-dev',
    'python3-pip',
    // ---- OSWorld evaluator/tooling binaries -------------------------------
    'gnome-screenshot',
    'scrot',
    'wmctrl',
    'xdotool',
    'xclip',
    'socat',
    'iproute2',
    'ffmpeg',
    // ---- audio: userspace PulseAudio + virtual sink -----------------------
    // The E2B guest kernel ships no ALSA device (no /proc/asound, snd-dummy
    // absent), so OSWorld 2.0's audio apps (REAPER/MuseScore/Shotcut) route
    // through a PulseAudio null sink started in the user session. The tasks
    // render/export audio files offline and never query live sink state.
    'pulseaudio',
    'pulseaudio-utils',
    // ---- office / productivity apps evaluators hard-reference -------------
    'libreoffice',
    'libreoffice-gnome',
    'gimp',
    'vlc',
    'thunderbird',
    // evince = the reference GNOME desktop's PDF handler. Without it xdg-open
    // resolves application/pdf to GIMP/LO Draw rather than a PDF viewer.
    'evince',
    // ---- fonts / locale ---------------------------------------------------
    'fonts-dejavu',
    'fonts-liberation',
    'fonts-noto',
    'fonts-noto-cjk',
    'locales',
    'tzdata',
    // ---- misc -------------------------------------------------------------
    'gnupg',
    'apt-transport-https',
    'curl',
    'ca-certificates',
    'sudo',
  ], { noInstallRecommends: false })
  // Locale
  .runCmd([
    'locale-gen en_US.UTF-8',
    'update-locale LANG=en_US.UTF-8',
    'ln -sf /usr/share/zoneinfo/UTC /etc/localtime',
  ])
  // ---- Chrome from Google's apt repo (evaluators assert `google-chrome`) ---
  .runCmd([
    'curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg',
    'echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list',
    'apt-get update',
    'apt-get install -y google-chrome-stable',
    // Freeze the installed version inside the guest; the immutable template
    // build pins it across sandboxes.
    'apt-mark hold google-chrome-stable',
  ])
  // ---- VSCode pinned to the OSWorld reference image's 1.91.1 --------------
  // Installed from Microsoft's permanent versioned .deb URL, not the rolling
  // apt repo: 1.91.1 predates the Copilot chat panel and sign-in surfaces
  // that measurably distracted agents in live runs on newer builds.
  // Installing a local .deb skips apt's repository signature check, so the
  // download is integrity-pinned by sha256 (recorded 2026-08-26 from the
  // canonical versioned URL; a mismatch means the artifact changed upstream
  // and must be re-reviewed, never silently accepted).
  .runCmd([
    'curl -fsSL -o /tmp/code_1.91.1.deb "https://update.code.visualstudio.com/1.91.1/linux-deb-x64/stable"',
    'echo "31a13c05295f3349d3dc168d9c67dda4fcf30823fe3c34f215f0324d197f5479  /tmp/code_1.91.1.deb" | sha256sum -c -',
    'apt-get install -y /tmp/code_1.91.1.deb',
    'rm -f /tmp/code_1.91.1.deb',
    'apt-mark hold code',
  ])
  // ---- OSWorld 2.0 expanded application set (apt) -------------------------
  // MuseScore 3, Shotcut, FreeCAD, and OpenBoard ship in Ubuntu 22.04's
  // universe repo.
  // Each immutable template build freezes whatever version apt installed;
  // apt-mark hold keeps the guest from drifting (same pattern as Chrome).
  .runCmd([
    'apt-get install -y musescore3 shotcut freecad openboard',
    'apt-mark hold musescore3 shotcut freecad openboard',
    // Task 093 hard-codes the upstream image's snap launcher and snap package
    // probe. Preserve that narrow observable contract without installing the
    // privileged snapd daemon. Executing through this lowercase symlink also
    // yields the lowercase process name that the task verifies with pgrep.
    'mkdir -p /snap/bin',
    'ln -sfn /usr/bin/OpenBoard /snap/bin/openboard',
  ])
  .copy('snap-openboard-compat.sh', '/usr/local/bin/snap', { mode: 0o755 })
  // ---- nested-Docker Compose compatibility -------------------------------
  // Task 082 installs Ubuntu's Docker engine at setup time. Jammy's fallback
  // docker-compose v1 is incompatible with the newer requests dependency used
  // by the V2 guest server (http+docker transport was removed). A checksum-
  // pinned Compose v2 plugin keeps the task on `docker compose` without adding
  // a mutable apt repository or installer script to the template build.
  .runCmd([
    'mkdir -p /usr/local/lib/docker/cli-plugins',
    'curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose "https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-x86_64"',
    'echo "dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372  /usr/local/lib/docker/cli-plugins/docker-compose" | sha256sum -c -',
    'chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose',
  ])
  // ---- Zotero (reference manager) ----------------------------------------
  // Zotero ships no official .deb; the versioned tarball from
  // download.zotero.org is the archived, pinnable source. Installed to
  // /opt/zotero with a launcher symlink + .desktop entry so OSWorld's zotero
  // tasks resolve it. Pinned 7.0.15.
  // sha256-pinned like the VSCode .deb (tarballs have no signature channel).
  .runCmd([
    'curl -fsSL -o /tmp/zotero.tar.bz2 "https://download.zotero.org/client/release/7.0.15/Zotero-7.0.15_linux-x86_64.tar.bz2"',
    'echo "d16a8aca23562c025e07e274524fbf7cc1225f67f6075b1958ec896eeb4523bf  /tmp/zotero.tar.bz2" | sha256sum -c -',
    'mkdir -p /opt/zotero',
    'tar -xjf /tmp/zotero.tar.bz2 -C /opt/zotero --strip-components=1',
    'rm -f /tmp/zotero.tar.bz2',
    'ln -sf /opt/zotero/zotero /usr/local/bin/zotero',
    "printf '[Desktop Entry]\\nName=Zotero\\nExec=/opt/zotero/zotero --url %%u\\nIcon=/opt/zotero/icons/icon128.png\\nType=Application\\nStartupWMClass=Zotero\\nCategories=Office;\\nMimeType=x-scheme-handler/zotero;text/x-moz-hlink;\\n' > /usr/share/applications/zotero.desktop",
  ])
  // ---- REAPER (DAW) ------------------------------------------------------
  // Versioned Linux tarball from reaper.fm into /opt/REAPER + launcher and
  // .desktop entry. REAPER's own install-reaper.sh is interactive; extract
  // the payload manually instead. Pinned 7.79.
  // sha256-pinned like the VSCode .deb (tarballs have no signature channel).
  .runCmd([
    'curl -fsSL -o /tmp/reaper.tar.xz "https://www.reaper.fm/files/7.x/reaper779_linux_x86_64.tar.xz"',
    'echo "a420b47fc5a1bef2ba6ecbf4937bb67b094333f0daa318dfe702d5fcdaa38846  /tmp/reaper.tar.xz" | sha256sum -c -',
    'mkdir -p /tmp/reaper-x',
    'tar -xJf /tmp/reaper.tar.xz -C /tmp/reaper-x',
    'mv /tmp/reaper-x/reaper_linux_x86_64/REAPER /opt/REAPER',
    'rm -rf /tmp/reaper.tar.xz /tmp/reaper-x',
    "printf '[Desktop Entry]\\nName=REAPER\\nExec=/usr/local/bin/reaper\\nType=Application\\nStartupWMClass=REAPER\\nCategories=AudioVideo;Audio;\\n' > /usr/share/applications/reaper.desktop",
  ])
  .copy('reaper-launcher.sh', '/usr/local/bin/reaper', { mode: 0o755 })
  // ---- create OSWorld's uid-1000 `user` account ---------------------------
  .runCmd([
    'id user >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash user',
    'usermod -aG sudo,audio,video user || true',
    'echo "user ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-user',
    'echo "user:osworld-public-evaluation" | chpasswd',
    'mkdir -p /home/user/.local/share/keyrings /home/user/.config/vlc',
    'touch /home/user/.local/share/keyrings/login.keyring /home/user/.Xauthority',
    // One Chrome profile, two views: the google-chrome shim launches with
    // --user-data-dir=google-chrome-cdp (Chrome >=136 disables the debug port
    // on the default path, even passed explicitly - probed on Chrome 150),
    // while OSWorld's profile getters hardcode ~/.config/google-chrome/... .
    // Symlinking the default path onto the CDP dir keeps both consistent.
    'mkdir -p /home/user/.config/google-chrome-cdp',
    'ln -sfn google-chrome-cdp /home/user/.config/google-chrome',
  ])
  // ---- Chrome: use the compatibility wrapper on every GUI launch path -----
  .runCmd([
    "sed -Ei '/^Exec=/ s#/usr/bin/google-chrome-stable#/usr/local/bin/google-chrome#g; /^Exec=/ s#google-chrome-stable#/usr/local/bin/google-chrome#g' /usr/share/applications/google-chrome.desktop || true",
  ])
  // ---- PATH shims: make task-config CDP launches work through ingress -----
  // Task configs run `google-chrome --remote-debugging-port=1337` + a socat
  // 9222->1337 forward. The chrome shim adds the flags Chrome >=136 needs to
  // actually open the debug port; the socat shim swaps the plain TCP forward
  // for cdp_hostfix.py (Host-normalizing proxy), since E2B's ingress rewrites
  // the Host header and Chrome's DevTools host check rejects it.
  .copy('google-chrome-shim.sh', '/usr/local/bin/google-chrome', { mode: 0o755 })
  // Some V2 task runtimes launch /usr/bin/google-chrome by absolute path, so a
  // PATH-only shim is insufficient. Keep google-chrome-stable as the wrapper's
  // real target and redirect only the unversioned absolute path through it.
  .runCmd('ln -sfn /usr/local/bin/google-chrome /usr/bin/google-chrome')
  .copy('socat-shim.sh', '/usr/local/bin/socat', { mode: 0o755 })
  .copy('iptables-owner-compat.sh', '/usr/local/sbin/iptables', { mode: 0o755 })
  // Task setup is unattended, and the E2B base image owns configuration such
  // as sshd_config. Package upgrades must retain that image-managed state
  // instead of waiting forever for a ucf prompt.
  .copy('apt-get-noninteractive.sh', '/usr/local/sbin/apt-get', { mode: 0o755 })
  // Keep the Host-normalizing CDP listener present even after GUI relaunches.
  .runCmd("printf '[Unit]\\nDescription=OSWorld CDP host-normalizing proxy\\nAfter=network.target\\n[Service]\\nUser=user\\nExecStart=/usr/bin/python3 /opt/osworld-server/cdp_hostfix.py\\nRestart=always\\n[Install]\\nWantedBy=multi-user.target\\n' > /etc/systemd/system/osworld-cdp.service")
  // ---- VLC Lua HTTP interface :8080 (baked; matches full install) ---------
  .runCmd(
    "printf '[core]\\nextraintf=http\\n[lua]\\nhttp-port=8080\\nhttp-host=0.0.0.0\\nhttp-password=password\\n[qt]\\nqt-privacy-ask=0\\nqt-updates-notif=0\\n' > /home/user/.config/vlc/vlcrc",
  )
  // Avoid first-run UI that intercepts benchmark actions. These values use
  // LibreOffice's own registry paths from the installed 7.3.7 schema.
  .makeDir('/home/user/.config/libreoffice/4/user')
  .copy(
    'libreoffice-registrymodifications.xcu',
    '/home/user/.config/libreoffice/4/user/registrymodifications.xcu',
  )
  // ---- MuseScore 3 first-run suppression (baked config) -------------------
  // Fresh MuseScore 3 opens a modal "Startup Wizard", then a "Start Center"
  // score picker, then a "Tour" popup - all agent-blocking. This ini was
  // produced by MuseScore itself after setting Program Start = "Start empty"
  // and unchecking show-start-center / show-tours / show-splash in
  // Preferences, then captured verbatim. The decisive key is
  // ui/.../sessionStart = EMPTY (@Variant), which is what actually stops the
  // Start Center; the boolean flags alone do not. Same doctrine as the baked
  // LibreOffice/VLC first-run configs. Path org/app = MuseScore/MuseScore3.
  .makeDir('/home/user/.config/MuseScore')
  .copy('MuseScore3.ini', '/home/user/.config/MuseScore/MuseScore3.ini')
  // ---- REAPER first-run suppression (baked config) ------------------------
  // Fresh (unregistered) REAPER opens three windows on launch: the main
  // window, an "About REAPER" evaluation/license nag, and an "Error opening
  // devices" dialog (its default audio system is JACK, and no JACK server
  // runs). This reaper.ini selects "Dummy Audio" (linux_audio_mode=2), which
  // removes the device error. REAPER binds its nag acknowledgement to host
  // identity, so it cannot be baked into an E2B template whose clones receive
  // new identities; reaper-launcher.sh closes that startup window once. The
  // title bar still reads "EVALUATION LICENSE" - that is the unregistered
  // product's own window title, not a modal.
  .makeDir('/home/user/.config/REAPER')
  .copy('reaper.ini', '/home/user/.config/REAPER/reaper.ini')
  // ---- system-wide dconf defaults: a11y + interface + DPI/scaling ---------
  // Matches the OSWorld reference GNOME session: toolkit-accessibility on (so
  // GTK/Qt apps expose AT-SPI trees), Adwaita cursor/theme, 1.0 text scaling
  // and 96 DPI to pair with the baked 1920x1080 framebuffer.
  .makeDir('/etc/dconf/db/site.d')
  .makeDir('/etc/dconf/profile')
  .copy('dconf-osworld', '/etc/dconf/db/site.d/00-osworld')
  .runCmd([
    "printf 'user-db:user\\nsystem-db:site\\n' > /etc/dconf/profile/user",
    'dconf update || true',
    // 96 DPI via Xresources so Xft-based apps size text consistently at 1080p.
    "printf 'Xft.dpi: 96\\n' > /home/user/.Xresources",
  ])
  // ---- OSWorld server payload + session scripts ---------------------------
  .makeDir('/opt/osworld-server')
  .copy('server', '/opt/osworld-server')
  .copy('session_inner.sh', '/opt/osworld-server/session_inner.sh', { mode: 0o755 })
  .copy('start.sh', '/opt/osworld-server/start.sh', { mode: 0o755 })
  .runCmd('python3 -m pip install --no-cache-dir -r /opt/osworld-server/requirements.txt')
  .runCmd([
    'ln -sf /usr/bin/python3 /usr/bin/python || true',
    'chown -R user:user /opt/osworld-server /home/user',
    'systemctl enable --now osworld-cdp.service',
  ])
  // Boot GNOME + server inside a logind session (start.sh runs as root).
  .setStartCmd('bash /opt/osworld-server/start.sh', waitForPort(5000))
