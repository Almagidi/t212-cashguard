/**
 * Static contract for the web image's dependency install (apps/web/Dockerfile).
 *
 * Retrieval and execution are separate: tarballs are downloaded with lifecycle
 * scripts disabled, and the install that runs lifecycle scripts has no network
 * and reads only from that cache. See docs/WEB_IMAGE_BUILD.md.
 */
import { describe, expect, test } from '@jest/globals'
import fs from 'node:fs'
import path from 'node:path'

const dockerfile = fs.readFileSync(path.resolve(__dirname, '..', '..', 'Dockerfile'), 'utf8')

// One string per instruction: continuation lines joined, comments dropped.
const instructions = dockerfile
  .replace(/\\\r?\n\s*/g, ' ')
  .split(/\r?\n/)
  .map((line) => line.trim())
  .filter((line) => line !== '' && !line.startsWith('#'))

function stageInstructions(name: string): string[] {
  const start = instructions.findIndex((line) => new RegExp(`^FROM\\s+\\S+\\s+AS\\s+${name}$`, 'i').test(line))
  if (start === -1) throw new Error(`stage ${name} not found in Dockerfile`)
  const rest = instructions.slice(start + 1)
  const end = rest.findIndex((line) => /^FROM\s/i.test(line))
  return [instructions[start], ...(end === -1 ? rest : rest.slice(0, end))]
}

// Any instruction that can run dependency install/lifecycle scripts.
const installInstructions = instructions.filter(
  (line) => /^RUN\s/.test(line) && /\bnpm\s+(ci|install|i|rebuild|install-test|it)\b/.test(line),
)
const disablesScripts = (line: string) => /--ignore-scripts(?![=\w-])/.test(line)
const isOfflineWithoutNetwork = (line: string) =>
  /^RUN\s+(--\S+\s+)*--network=none\s/.test(line) && /\bnpm\s+\S+\s+--offline(?![=\w-])/.test(line)

describe('web image dependency install isolation', () => {
  test('retrieval stage downloads without running lifecycle scripts', () => {
    const installs = stageInstructions('npm_fetch').filter((line) => /\bnpm\s+ci\b/.test(line))

    expect(installs).toHaveLength(1)
    expect(disablesScripts(installs[0])).toBe(true)
    expect(installs[0]).toContain('--cache /npm-cache')
  })

  test('cache stage carries only the downloaded cache', () => {
    const [from, ...body] = stageInstructions('npm_cache')

    expect(from).toMatch(/^FROM\s+scratch\s+AS\s+npm_cache$/)
    expect(body).toEqual(['COPY --from=npm_fetch /npm-cache/ /'])
  })

  test('install stage runs lifecycle scripts offline with networking disabled', () => {
    const installs = stageInstructions('deps').filter((line) => /\bnpm\s+ci\b/.test(line))

    expect(installs).toHaveLength(1)
    expect(isOfflineWithoutNetwork(installs[0])).toBe(true)
    expect(installs[0]).toContain('--mount=type=bind,from=npm_cache,target=/root/.npm,rw ')
    expect(installs[0]).not.toContain('--ignore-scripts')
  })

  test('no install instruction can run lifecycle scripts with network access', () => {
    const unsafe = installInstructions.filter((line) => !disablesScripts(line) && !isOfflineWithoutNetwork(line))

    expect(installInstructions.length).toBeGreaterThan(0)
    expect(unsafe).toEqual([])
  })

  test('application build uses the offline-installed dependencies', () => {
    expect(stageInstructions('builder')).toContain('COPY --from=deps /workspace/node_modules ./node_modules')
  })
})
