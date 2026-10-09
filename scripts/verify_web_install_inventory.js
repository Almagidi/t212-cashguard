#!/usr/bin/env node
/**
 * Checks that every package-lock.json entry this platform needs is installed
 * at its locked version.
 *
 * npm skips an optional package whose install fails and still exits 0, so an
 * "added N packages" summary does not prove the tree is complete.
 *
 * Usage: node verify_web_install_inventory.js [directory containing package-lock.json]
 */
'use strict'

const fs = require('node:fs')
const path = require('node:path')

const root = path.resolve(process.argv[2] || process.cwd())
const { packages } = JSON.parse(fs.readFileSync(path.join(root, 'package-lock.json'), 'utf8'))

function currentLibc() {
  if (process.platform !== 'linux') return null
  return process.report.getReport().header.glibcVersionRuntime ? 'glibc' : 'musl'
}

const platform = { os: process.platform, cpu: process.arch, libc: currentLibc() }

// Same rule npm applies to os/cpu/libc lists: any matching "!x" excludes, and
// if positive entries exist one of them must match.
function listAllows(field, value) {
  const list = typeof field === 'string' ? [field] : field
  if (!Array.isArray(list) || list.length === 0 || list.includes('any')) return true
  if (list.includes(`!${value}`)) return false
  const positives = list.filter((item) => !item.startsWith('!'))
  return positives.length === 0 || positives.includes(value)
}

function appliesHere(entry) {
  if (!listAllows(entry.os, platform.os) || !listAllows(entry.cpu, platform.cpu)) return false
  if (Array.isArray(entry.libc) && entry.libc.length > 0) {
    return platform.libc !== null && listAllows(entry.libc, platform.libc)
  }
  return true
}

// Node resolution: nearest node_modules first, then each ancestor's.
function resolveDependency(fromKey, name) {
  let scope = fromKey
  for (;;) {
    const candidate = scope === '' ? `node_modules/${name}` : `${scope}/node_modules/${name}`
    if (packages[candidate]) return candidate
    if (scope === '') return null
    const cut = scope.lastIndexOf('/node_modules/')
    scope = cut === -1 ? '' : scope.slice(0, cut)
  }
}

// Each dependency name with whether the lockfile must contain it. Optional and
// peer dependencies may legitimately be absent from the lockfile.
function dependencyEdges(key, entry) {
  const required = [entry.dependencies, key === '' ? entry.devDependencies : null]
  const optional = [entry.optionalDependencies, entry.peerDependencies]
  const optionalNames = new Set(optional.flatMap((group) => Object.keys(group || {})))
  return [
    ...required.flatMap((group) => Object.keys(group || {}))
      .filter((name) => !optionalNames.has(name))
      .map((name) => ({ name, mustResolve: true })),
    ...[...optionalNames].map((name) => ({ name, mustResolve: false })),
  ]
}

// Walk from the project root through packages that apply here. Dependencies
// reachable only through a package for another platform are not expected.
const expected = new Set()
const skippedForPlatform = new Set()
const problems = []
const queue = ['']
while (queue.length > 0) {
  const key = queue.pop()
  for (const { name, mustResolve } of dependencyEdges(key, packages[key])) {
    const target = resolveDependency(key, name)
    if (target === null && mustResolve) problems.push(`UNRESOLVED ${name} required by ${key || '(root)'}`)
    if (target === null || expected.has(target) || packages[target].link) continue
    if (!appliesHere(packages[target])) {
      skippedForPlatform.add(target)
      continue
    }
    expected.add(target)
    queue.push(target)
  }
}

for (const key of [...expected].sort()) {
  const manifest = path.join(root, key, 'package.json')
  const kind = packages[key].optional ? 'optional' : 'required'
  if (!fs.existsSync(manifest)) {
    problems.push(`MISSING (${kind}) ${key}@${packages[key].version}`)
    continue
  }
  const installed = JSON.parse(fs.readFileSync(manifest, 'utf8')).version
  if (installed !== packages[key].version) {
    problems.push(`VERSION ${key}: locked ${packages[key].version}, installed ${installed}`)
  }
}

console.log(`platform: ${platform.os}/${platform.cpu}${platform.libc ? `/${platform.libc}` : ''}`)
console.log(`lockfile entries: ${Object.keys(packages).length - 1}`)
console.log(`expected on this platform: ${expected.size}`)
console.log(`not applicable to this platform: ${skippedForPlatform.size}`)

if (expected.size === 0) {
  console.error('FAIL: no expected packages were derived from the lockfile')
  process.exit(1)
}
if (problems.length > 0) {
  console.error(`FAIL: ${problems.length} problem(s) between the lockfile and the installed tree`)
  for (const problem of problems) console.error(`  ${problem}`)
  process.exit(1)
}
console.log(`OK: all ${expected.size} expected packages are installed at their locked versions`)
