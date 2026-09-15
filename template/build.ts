import { Sandbox, Template, defaultBuildLogger } from 'e2b'
import { template } from './template.js'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'

// OSWorld 2.0 guest template. Name/geometry match the V2 conversion plan:
// osworld-v2-gnome, 4 vCPU, 8 GB. E2B root capacity comes from the project's
// disk entitlement rather than a per-build SDK option, so the build is accepted
// only after a fresh restore proves at least the release's 100 GB maximum.
const TAG = process.env.GNOME_TAG || 'osworld-v2-gnome'

type ReleaseLock = {
  release: string
  server_code: { commit: string }
}
const lock = JSON.parse(
  readFileSync(new URL('../examples/osworld-v2/upstream.lock.json', import.meta.url), 'utf8'),
) as ReleaseLock
if (!/^osworld-v2-[0-9]{4}\.[0-9]{2}\.[0-9]{2}$/.test(lock.release)) {
  throw new Error('invalid release in examples/osworld-v2/upstream.lock.json')
}
if (!/^[0-9a-f]{40}$/.test(lock.server_code.commit)) {
  throw new Error('invalid server commit in examples/osworld-v2/upstream.lock.json')
}
const OSWORLD_SERVER_COMMIT = lock.server_code.commit
const LOCKFILE_RELEASE = lock.release
const MIN_ROOT_CAPACITY_GB = 100
const PROTECTED_EGRESS_CIDRS = [
  '10.0.0.0/8',
  '100.64.0.0/10',
  '169.254.0.0/16',
  '172.16.0.0/12',
  '192.0.0.0/24',
  '192.168.0.0/16',
  '198.18.0.0/15',
  '224.0.0.0/4',
  '240.0.0.0/4',
  '::1/128',
  'fc00::/7',
  'fe80::/10',
  'ff00::/8',
]

async function main() {
  console.log(`Building template "${TAG}"...`)
  // 4 vCPU matches OSWorld's reference t3.xlarge. 8 GB (reference has 16) is
  // needed headroom: chrome_open_tabs configs load 3 heavy sites at once and
  // at 4 GB the guest thrashes, leaving CDP unresponsive for minutes.
  const cpuCount = parseInt(process.env.CPU_COUNT || '4', 10)
  const memoryMB = parseInt(process.env.MEM_MB || '8192', 10)
  const info = await Template.build(template, TAG, {
    cpuCount,
    memoryMB,
    skipCache: process.env.SKIP_CACHE === '1',
    onBuildLogs: defaultBuildLogger(),
  })
  const immutableRef = `${info.name}:${info.buildId}`
  const sandbox = await Sandbox.create(immutableRef, {
    timeoutMs: 5 * 60_000,
    secure: true,
    network: {
      allowPublicTraffic: false,
      denyOut: PROTECTED_EGRESS_CIDRS,
    },
    metadata: { workload: 'osworld-v2-template-build-smoke' },
  })
  let rootCapacityBytes = 0
  let rootUsedBytes = 0
  let applicationInventory: string[] = []
  let appliedNetworkPolicy: unknown = null
  try {
    const sandboxInfo = await sandbox.getInfo()
    appliedNetworkPolicy = sandboxInfo.network
    const expectedNetworkPolicy = {
      allowPublicTraffic: false,
      denyOut: PROTECTED_EGRESS_CIDRS,
    }
    const networkPolicy = appliedNetworkPolicy as {
      allowPublicTraffic?: boolean
      denyOut?: string[]
    }
    if (
      networkPolicy?.allowPublicTraffic !== expectedNetworkPolicy.allowPublicTraffic ||
      JSON.stringify(networkPolicy?.denyOut) !== JSON.stringify(expectedNetworkPolicy.denyOut)
    ) {
      throw new Error(
        `sandbox network policy mismatch: ${JSON.stringify(appliedNetworkPolicy)}`,
      )
    }
    const capacity = await sandbox.commands.run('df -B1 --output=size / | tail -n 1', {
      user: 'root',
      timeoutMs: 30_000,
    })
    rootCapacityBytes = Number.parseInt(capacity.stdout.trim(), 10)
    if (!Number.isSafeInteger(rootCapacityBytes)) {
      throw new Error(`invalid root-capacity output: ${JSON.stringify(capacity.stdout)}`)
    }
    const minimumBytes = MIN_ROOT_CAPACITY_GB * 1_000_000_000
    if (rootCapacityBytes < minimumBytes) {
      throw new Error(
        `template root is undersized: ${(rootCapacityBytes / 1_000_000_000).toFixed(2)} GB ` +
          `< ${MIN_ROOT_CAPACITY_GB} GB`,
      )
    }
    // Used bytes on / for a freshly booted guest of this build: the image
    // footprint before a task writes anything. docs/runtime.md quotes it.
    const used = await sandbox.commands.run('df -B1 --output=used / | tail -n 1', {
      user: 'root',
      timeoutMs: 30_000,
    })
    rootUsedBytes = Number.parseInt(used.stdout.trim(), 10)
    if (!Number.isSafeInteger(rootUsedBytes)) {
      throw new Error(`invalid root-used output: ${JSON.stringify(used.stdout)}`)
    }
    // Every command name an OSWorld 2.0 task or its evaluator invokes directly,
    // plus the VNC/NSS tools the bridge shells out to. The loop collects all
    // missing names and exits 0 so the names reach this process as stdout
    // rather than as a bare non-zero exit from the SDK.
    const launchers = [
      'musescore',
      'wpp',
      'wps',
      'blender',
      'kicad',
      'freecad',
      'freecadcmd',
      'certutil',
      'x11vnc',
      'websockify',
      'google-chrome',
    ]
    const which = await sandbox.commands.run(
      'missing=""; ' +
        `for c in ${launchers.join(' ')}; do ` +
        'command -v "$c" >/dev/null 2>&1 || missing="$missing $c"; done; ' +
        'if [ -n "$missing" ]; then echo "MISSING$missing"; else echo LAUNCHERS_OK; fi',
      { user: 'root', timeoutMs: 60_000 },
    )
    if (!which.stdout.includes('LAUNCHERS_OK')) {
      throw new Error(
        `launcher smoke failed: ${which.stdout.trim()} ${which.stderr.trim()}`.trim(),
      )
    }
    // Versions of the applications this build installs. `dpkg-query -W` fails
    // for the whole invocation if any one name is absent, so query one package
    // per iteration: an absent package then names itself instead of aborting
    // the list. Every name here MUST be installed, so a MISSING_ marker below
    // fails the build.
    const versions = await sandbox.commands.run(
      'for p in google-chrome-stable kicad wps-office x11vnc novnc websockify libnss3-tools; do ' +
        "dpkg-query -W -f='${Package} ${Version}\\n' \"$p\" 2>/dev/null || echo \"MISSING_PACKAGE $p\"; " +
        'done; ' +
        '/opt/blender/blender --version 2>/dev/null | head -1 || true; ' +
        'ls /opt/musescore/squashfs-root/bin/ 2>/dev/null | head -3; ' +
        'ls /opt/freecad/squashfs-root/usr/bin/ 2>/dev/null | grep -i freecad | head -3; ' +
        'if [ -f /etc/systemd/user/x11vnc.service ] && [ -f /etc/systemd/user/novnc.service ]; ' +
        'then echo VNC_UNITS_PRESENT; else echo MISSING_VNC_UNITS; fi; ' +
        'echo INVENTORY_DONE',
      { user: 'root', timeoutMs: 60_000 },
    )
    const inventoryLines = versions.stdout
      .trim()
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0)
    if (!inventoryLines.includes('INVENTORY_DONE')) {
      throw new Error(
        `application inventory smoke did not complete: ${versions.stdout.trim()} ` +
          versions.stderr.trim(),
      )
    }
    const inventoryFailures = inventoryLines.filter((line) => line.startsWith('MISSING_'))
    if (inventoryFailures.length > 0) {
      throw new Error(`application inventory smoke failed: ${inventoryFailures.join('; ')}`)
    }
    applicationInventory = inventoryLines.filter((line) => line !== 'INVENTORY_DONE')
  } finally {
    await sandbox.kill()
  }
  const receipt = {
    name: info.name,
    buildId: info.buildId,
    templateId: info.templateId,
    reference: immutableRef,
    cpuCount,
    memoryMB,
    diskSizeMB: Math.floor(rootCapacityBytes / 1_000_000),
    diskSizeMBRequested: MIN_ROOT_CAPACITY_GB * 1000,
    diskSizeMBNote:
      'E2B project disk entitlement supplies root capacity; verified from the exact immutable build.',
    rootCapacityBytes,
    rootUsedBytes,
    rootUsedNote:
      'Bytes used on / by a freshly booted sandbox of this build, before any task writes. ' +
      'This is the image footprint, not a live-run peak.',
    minimumRootCapacityGB: MIN_ROOT_CAPACITY_GB,
    applicationInventory,
    applicationInventoryNote:
      'Package versions and launcher payloads observed in the smoke sandbox of this exact ' +
      'build: dpkg versions, the Blender banner, the MuseScore 4 and FreeCAD AppImage ' +
      'binaries, and the presence of both VNC user units. Recorded only; a launcher ' +
      'resolving is not evidence that the application opens a document.',
    unpinnableVersionsNote:
      'google-chrome-stable and kicad have no source sha256 pin: they come from mutable apt ' +
      "sources (Google's Chrome repo and ppa:kicad/kicad-10.0-releases), which serve whatever " +
      'version is current at build time. Both are apt-mark hold inside the guest, and the ' +
      'versions in applicationInventory are frozen for every sandbox by this immutable build ' +
      'id, not by a pin in template/template.ts. Re-running the build can install newer ' +
      'versions; only the build id guarantees the ones recorded here.',
    smokeSandboxId: sandbox.sandboxId,
    networkPolicy: appliedNetworkPolicy,
    timestamp: new Date().toISOString(),
    lockfileRelease: LOCKFILE_RELEASE,
    osworldServerCommit: OSWORLD_SERVER_COMMIT,
  }
  // Local build-dir copy.
  mkdirSync('results', { recursive: true })
  writeFileSync('results/template-build.json', JSON.stringify(receipt, null, 2) + '\n')
  console.log('BUILD_DONE', JSON.stringify(info))
  console.log('RECEIPT', JSON.stringify(receipt))
  console.log(`Use this exact build for validation: export GUEST_TEMPLATE=${immutableRef}`)
}

main().catch((e) => {
  console.error('BUILD_ERROR', e?.message)
  console.error(e)
  process.exit(1)
})
