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
// application set (MuseScore 4, Shotcut, FreeCAD, Zotero, REAPER), and a
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
    // ---- VNC stack for upstream's --enable_vnc path (units disabled) -------
    'x11vnc',
    'novnc',
    'websockify',
    // ---- certutil for per-campaign CA trust in Chrome's NSS db -------------
    'libnss3-tools',
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
  // Shotcut and OpenBoard ship in Ubuntu 22.04's universe repo. MuseScore 4
  // is installed separately below as a pinned AppImage (jammy's apt MuseScore
  // package rejects task 067's score). FreeCAD is deliberately absent here: the
  // apt package blocks the KiCad 10 PPA install, so it ships as the 1.1.3
  // AppImage below (see the KiCad block).
  // Each immutable template build freezes whatever version apt installed;
  // apt-mark hold keeps the guest from drifting (same pattern as Chrome).
  .runCmd([
    'apt-get install -y shotcut openboard',
    'apt-mark hold shotcut openboard',
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
  // ---- MuseScore 4 (tasks 067/071 invoke `musescore`) ---------------------
  // Task 067's score was written by MuseScore Studio 4.6.5, which the jammy
  // apt MuseScore 3 package rejects at export. Pinned AppImage from the
  // GitHub release; sha256 from the release's checksums.sha256.txt. Extracted
  // at build time because the guest has no FUSE.
  .runCmd([
    'apt-get install -y libjack-jackd2-0',
    'curl -fsSL -o /tmp/musescore.AppImage "https://github.com/musescore/MuseScore/releases/download/v4.6.5/MuseScore-Studio-4.6.5.253511702-x86_64.AppImage"',
    'echo "193daa0ea18bcfa90a47145a842275b8069b7b2b8d153e562b15fab5fe50fcaf  /tmp/musescore.AppImage" | sha256sum -c -',
    'chmod +x /tmp/musescore.AppImage',
    'mkdir -p /opt/musescore && cd /opt/musescore && /tmp/musescore.AppImage --appimage-extract >/dev/null',
    'rm -f /tmp/musescore.AppImage',
    "printf '[Desktop Entry]\\nName=MuseScore 4\\nExec=/usr/local/bin/musescore %%F\\nType=Application\\nStartupWMClass=MuseScore4\\nCategories=AudioVideo;Audio;\\nMimeType=application/x-musescore;application/vnd.recordare.musicxml+xml;\\n' > /usr/share/applications/musescore4.desktop",
  ])
  .copy('musescore-launcher.sh', '/usr/local/bin/musescore', { mode: 0o755 })
  // ---- WPS Office (tasks 049/060/077/079/087/090/096 invoke `wpp`; 063/066/
  // 076/080/091 invoke `wps`) -------------------------------------------------
  // Kingsoft ships only the current build (older build numbers return 403), so
  // the .deb is sha256-pinned at the recorded build and apt-mark held. The
  // sha256 was computed from the download itself, twice: once inside a scratch
  // sandbox and once from the build host, both 318,892,996 bytes. libtiff5 is
  // required by the bundled PDF engine (libpdfmain.so links libtiff.so.5).
  // --no-install-recommends keeps ttf-mscorefonts-installer out (it fetches
  // from SourceForge at install time behind an interactive EULA); Carlito and
  // Caladea are the metric-compatible open fonts. Proprietary: the customer
  // accepts the Kingsoft EULA at https://www.wps.com/eula/ .
  .runCmd([
    'curl -fsSL -o /tmp/wps-office.deb "https://wdl1.pcfg.cache.wpscdn.com/wpsdl/wpsoffice/download/linux/11723/wps-office_11.1.0.11723.XA_amd64.deb"',
    'echo "fe6326210f69d94efdbf2728914d293036be391b93a614f58cd0e1ff1d4923b3  /tmp/wps-office.deb" | sha256sum -c -',
    'apt-get install -y libtiff5 fonts-crosextra-carlito fonts-crosextra-caladea',
    'apt-get install -y --no-install-recommends /tmp/wps-office.deb',
    'rm -f /tmp/wps-office.deb',
    'apt-mark hold wps-office',
    'test -x /usr/bin/wpp && test -x /usr/bin/wps && test -x /usr/bin/et',
  ])
  // ---- Blender 4.5 LTS (task 092 invokes `blender`) -----------------------
  // jammy's apt blender is 3.0.1; the reference desktop has a current
  // Blender. Vendor tarball, sha256 from the release's .sha256 file.
  .runCmd([
    'apt-get install -y libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libxkbcommon0 libsm6',
    'curl -fsSL -o /tmp/blender.tar.xz "https://download.blender.org/release/Blender4.5/blender-4.5.14-linux-x64.tar.xz"',
    'echo "9ba871ff2ecd36526b77432745980b7e6664ecd0c7ca11c48849073dcfe06da3  /tmp/blender.tar.xz" | sha256sum -c -',
    'mkdir -p /opt/blender',
    'tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1',
    'rm -f /tmp/blender.tar.xz',
    'ln -sf /opt/blender/blender /usr/local/bin/blender',
    "printf '[Desktop Entry]\\nName=Blender\\nExec=/usr/local/bin/blender %%f\\nType=Application\\nStartupWMClass=Blender\\nCategories=Graphics;3DGraphics;\\nMimeType=application/x-blender;\\n' > /usr/share/applications/blender.desktop",
  ])
  // ---- KiCad 10 (tasks 107/108 invoke `kicad`) ----------------------------
  // Task 107's setup adds ppa:kicad/kicad-10.0-releases and installs kicad at
  // runtime unless `command -v kicad` already succeeds. Preinstalling from the
  // same PPA short-circuits that install, which is what makes the task work at
  // all here: the PPA's OpenCASCADE 7.6 declares
  //   Package: libocct-foundation-7.6
  //   Breaks: libocct-foundation-7.4, libocct-foundation-7.5
  // and jammy's apt FreeCAD 0.19 pulls the 7.5 set, so on a guest carrying apt
  // FreeCAD the task's own `apt-get install -y kicad` dies with APT exit 100:
  //   freecad : Depends: freecad-python3 but it is not going to be installed
  //   E: Error, pkgProblemResolver::Resolve generated breaks, this may be
  //      caused by held packages.
  // (probe: out/osworld-v2-raw/template-probe/kicad-apt-conflict.txt, run on
  // build 0d796343 where `freecad` was apt-installed and held). With apt
  // FreeCAD gone the same install succeeds and `command -v kicad` resolves, so
  // FreeCAD ships as the AppImage below instead of from apt.
  // PPAs are mutable, so the installed version is apt-mark held and the
  // immutable template build pins it across sandboxes (same as Chrome).
  .runCmd([
    'apt-get install -y --no-install-recommends software-properties-common',
    'add-apt-repository -y ppa:kicad/kicad-10.0-releases',
    'apt-get update',
    'apt-get install -y kicad',
    'apt-mark hold kicad',
    'test -x /usr/bin/kicad',
  ])
  // ---- FreeCAD 1.1.3 AppImage (tasks 103/104 invoke `freecad`) ------------
  // The AppImage bundles conda-forge OCCT 7.8.1 under its own prefix, so it
  // has no apt OpenCASCADE dependency and cannot collide with the KiCad PPA's
  // libocct 7.6 above. sha256 from the release's own
  // FreeCAD_1.1.3-Linux-x86_64-py311.AppImage-SHA256.txt, re-checked against
  // the downloaded file. Extracted at build time because the guest has no
  // FUSE (same as MuseScore 4).
  .runCmd([
    'curl -fsSL -o /tmp/freecad.AppImage "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-Linux-x86_64-py311.AppImage"',
    'echo "3a853eb69ee595f779f2255dbf80a765926981d8ff68903cefee4dfb03a8f5ef  /tmp/freecad.AppImage" | sha256sum -c -',
    'chmod +x /tmp/freecad.AppImage',
    'mkdir -p /opt/freecad && cd /opt/freecad && /tmp/freecad.AppImage --appimage-extract >/dev/null',
    'rm -f /tmp/freecad.AppImage',
    // Tasks 103/104 grade with an extractor that imports numpy, and their
    // setups apt-install python3-numpy for the system interpreter that apt
    // FreeCAD used. The AppImage runs its own bundled py311 instead, which
    // cannot see system site-packages, and the extractor swallows a missing
    // numpy into {"error": "numpy_unavailable"} rather than failing. The
    // bundle does ship it (numpy 1.26.4, scipy 1.16.3, conda-forge py311.14 -
    // probe: out/osworld-v2-raw/template-probe/freecad-appimage-numpy.txt);
    // assert it at build time so a future re-pin that drops it fails loudly.
    "/opt/freecad/squashfs-root/AppRun freecadcmd -c 'import numpy; print(\"NUMPY_OK\", numpy.__version__)' 2>&1 | grep -q NUMPY_OK",
    "printf '[Desktop Entry]\\nName=FreeCAD\\nExec=/usr/local/bin/freecad %%F\\nType=Application\\nStartupWMClass=FreeCAD\\nCategories=Graphics;Science;Engineering;\\nMimeType=application/x-extension-fcstd;\\n' > /usr/share/applications/freecad.desktop",
  ])
  .copy('freecad-launcher.sh', '/usr/local/bin/freecad', { mode: 0o755 })
  // Tasks 103/104 grade by running `freecadcmd` (task_103.py:281,
  // task_104.py:214), which jammy's apt FreeCAD used to supply. The AppImage's
  // AppRun execs usr/bin/$1 when that name exists there, and it ships
  // usr/bin/freecadcmd, so the launcher forwards its own invoked name and one
  // file serves both commands.
  .runCmd('ln -sfn /usr/local/bin/freecad /usr/local/bin/freecadcmd')
  // ---- create OSWorld's uid-1000 `user` account ---------------------------
  .runCmd([
    'id user >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash user',
    'usermod -aG sudo,audio,video user || true',
    'echo "user ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-user',
    'echo "user:osworld-public-evaluation" | chpasswd',
    'mkdir -p /home/user/.local/share/keyrings /home/user/.config/vlc',
    'touch /home/user/.local/share/keyrings/login.keyring /home/user/.Xauthority',
    // Chrome on Linux trusts extra CAs only through the user's NSS db. Create it
    // empty at build so the bridge can `certutil -A` the campaign CA at start.
    'mkdir -p /home/user/.pki/nssdb',
    'certutil -N -d sql:/home/user/.pki/nssdb --empty-password',
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
  // ---- MuseScore 4 first-run suppression (baked config) -------------------
  // Fresh MuseScore 4 opens a first-launch setup wizard (language, playback,
  // cloud) and a tours prompt. `hasCompletedFirstLaunchSetup=true` skips the
  // wizard. This MuseScore4.ini is an initial, hand-written starter config,
  // NOT yet captured from a running MuseScore 4 (unlike the genuinely
  // guest-captured LibreOffice/VLC/REAPER configs below). The
  // application-launcher smoke on a fresh guest, later in this plan, is the
  // gate that forces a real capture-and-replace if any first-run dialog still
  // appears.
  .makeDir('/home/user/.config/MuseScore')
  .copy('MuseScore4.ini', '/home/user/.config/MuseScore/MuseScore4.ini')
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
  // ---- WPS Office first-run suppression (baked config) --------------------
  // Observed on a scratch sandbox of the current immutable guest with the .deb
  // above installed: a fresh `wpp` opens exactly two things before the
  // application is usable. First a modal titled "Kingsoft Office Software
  // License Agreement and Privacy Policy" whose "I Confirm" button stays
  // disabled until the "Have read and agreed to ..." checkbox is ticked.
  // Dismissing it once reveals a second window titled "System Check":
  // "Some formula symbols might not be displayed correctly due to missing
  // fonts Symbol, Wingdings...", with a "Do not report again" checkbox.
  // wps-office.conf is the Office.conf WPS itself wrote after both were
  // dismissed once and WPS was quit cleanly, copied verbatim. The decisive
  // keys are `common\AcceptedEULA=true` (EULA modal) and
  // `common\system_check\no_necessary_symbol_fonts=false` (System Check);
  // `[kdcsdk] NotFirstOpen=true` marks the suite as already started once.
  // Verified on that same guest: with only this file present, `wpp`, `wps` and
  // `et` each open a single application window and no dialog, and `wpp` on
  // task 049's own googlenet_intro.pptx opens straight into the editor.
  // Honest caveat: this suppresses the missing-font *warning*, it does not
  // supply Symbol/Wingdings - those fonts are still absent, so documents that
  // use them fall back to a substitute face.
  .makeDir('/home/user/.config/Kingsoft')
  .copy('wps-office.conf', '/home/user/.config/Kingsoft/Office.conf')
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
  // VNC units upstream's setup controller expects to be present (`systemctl
  // --user stop novnc.service x11vnc.service || true` in
  // OSWorld-V2/desktop_env/controllers/setup.py). The human-in-the-loop VNC
  // path is deferred, so the units are installed but never enabled.
  .copy('x11vnc.service', '/etc/systemd/user/x11vnc.service')
  .copy('novnc.service', '/etc/systemd/user/novnc.service')
  .runCmd('python3 -m pip install --no-cache-dir -r /opt/osworld-server/requirements.txt')
  .runCmd([
    'ln -sf /usr/bin/python3 /usr/bin/python || true',
    'chown -R user:user /opt/osworld-server /home/user',
    'systemctl enable --now osworld-cdp.service',
  ])
  // Boot GNOME + server inside a logind session (start.sh runs as root).
  .setStartCmd('bash /opt/osworld-server/start.sh', waitForPort(5000))
