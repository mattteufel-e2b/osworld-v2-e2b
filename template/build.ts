import { Sandbox, Template, defaultBuildLogger } from 'e2b'
import { template } from './template.js'
import { mkdirSync, writeFileSync } from 'node:fs'

// OSWorld 2.0 guest template. Name/geometry match the V2 conversion plan:
// osworld-v2-gnome, 4 vCPU, 8 GB. E2B root capacity comes from the project's
// disk entitlement rather than a per-build SDK option, so the build is accepted
// only after a fresh restore proves at least the release's 100 GB maximum.
const TAG = process.env.GNOME_TAG || 'osworld-v2-gnome'

// The V2 osworld-server submodule commit vendored into files/server/.
const OSWORLD_SERVER_COMMIT = 'a3cc3f0c64e463f020d1a44780307e9b46cbcab1'
const LOCKFILE_RELEASE = 'osworld-v2-2026.08.08'
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
    minimumRootCapacityGB: MIN_ROOT_CAPACITY_GB,
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
