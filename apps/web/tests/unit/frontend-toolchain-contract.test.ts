import { describe, expect, test } from '@jest/globals'
import fs from 'node:fs'
import path from 'node:path'

const WEB_ROOT = path.resolve(__dirname, '..', '..')
const REPO_ROOT = path.resolve(WEB_ROOT, '..', '..')

describe('supported frontend toolchain contract', () => {
  test('pins the verified Node 24 LTS and bundled npm release everywhere authoritative', () => {
    const packageJson = JSON.parse(fs.readFileSync(path.join(WEB_ROOT, 'package.json'), 'utf8'))
    const workflow = fs.readFileSync(path.join(REPO_ROOT, '.github', 'workflows', 'ci.yml'), 'utf8')
    const dockerfile = fs.readFileSync(path.join(WEB_ROOT, 'Dockerfile'), 'utf8')

    expect(packageJson.engines).toEqual({ node: '24.x', npm: '11.x' })
    expect(packageJson.packageManager).toBe('npm@11.19.0')
    expect(fs.readFileSync(path.join(REPO_ROOT, '.nvmrc'), 'utf8').trim()).toBe('24.21.0')
    expect(fs.readFileSync(path.join(REPO_ROOT, '.node-version'), 'utf8').trim()).toBe('24.21.0')
    expect(workflow.match(/node-version:\s*["']24\.21\.0["']/g)).toHaveLength(2)
    expect(dockerfile).toMatch(/^FROM node:24\.21\.0-alpine AS base/m)
  })

  test('uses supported Tailwind PostCSS v4 inputs without the affected v3 consumers', () => {
    const packageJson = JSON.parse(fs.readFileSync(path.join(WEB_ROOT, 'package.json'), 'utf8'))
    const postcss = fs.readFileSync(path.join(WEB_ROOT, 'postcss.config.js'), 'utf8')
    const globals = fs.readFileSync(path.join(WEB_ROOT, 'styles', 'globals.css'), 'utf8')

    expect(packageJson.devDependencies.tailwindcss).toBe('4.3.3')
    expect(packageJson.devDependencies['@tailwindcss/postcss']).toBe('4.3.3')
    expect(packageJson.devDependencies.autoprefixer).toBeUndefined()
    expect(postcss).toContain("'@tailwindcss/postcss': {}")
    expect(postcss).not.toMatch(/\b(?:tailwindcss|autoprefixer):\s*\{\}/)
    expect(globals).toMatch(/^@import ['"]tailwindcss['"];\n@config ['"]\.\.\/tailwind\.config\.js['"];/)
    expect(globals).not.toMatch(/@tailwind\s/)
  })

  test('preserves the complete Next lint rule family through the maintained Oxlint replacement', () => {
    const packageJson = JSON.parse(fs.readFileSync(path.join(WEB_ROOT, 'package.json'), 'utf8'))
    const oxlint = JSON.parse(fs.readFileSync(path.join(WEB_ROOT, '.oxlintrc.json'), 'utf8'))
    const configuredRules = {
      ...(oxlint.rules ?? {}),
      ...Object.assign({}, ...(oxlint.overrides ?? []).map((entry: { rules?: object }) => entry.rules ?? {})),
    }
    const nextRules = Object.keys(configuredRules).filter((name) => name.startsWith('nextjs/'))

    expect(packageJson.devDependencies.oxlint).toBe('1.87.0')
    expect(packageJson.devDependencies['eslint-config-next']).toBeUndefined()
    expect(packageJson.devDependencies.eslint).toBeUndefined()
    expect(fs.existsSync(path.join(WEB_ROOT, 'eslint.config.mjs'))).toBe(false)
    expect(nextRules).toHaveLength(21)
    expect(configuredRules['react/rules-of-hooks']).toBe('error')
    expect(configuredRules['react/exhaustive-deps']).toBe('warn')
    expect(configuredRules['react-compat/no-deprecated']).toBe('error')
    expect(oxlint.jsPlugins).toContainEqual({ name: 'react-compat', specifier: 'eslint-plugin-react' })
  })
})
