import { describe, expect, test } from '@jest/globals'
import fs from 'node:fs'
import path from 'node:path'

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..')
const DOC_PATH = path.join(REPO_ROOT, 'docs', 'OPERATOR_UI_SAFETY_INVARIANTS.md')

describe('operator UI safety invariants documentation', () => {
  test('records the frontend proof points for paper-only operator safety', () => {
    const doc = fs.readFileSync(DOC_PATH, 'utf8')

    expect(doc).toContain('Operator UI Safety Invariants')
    expect(doc).toContain('Read-only operator dashboard')
    expect(doc).toContain('No live-trading unlock/control')
    expect(doc).toContain('Paper order form boundary')
    expect(doc).toContain('No broker order sent')
    expect(doc).toContain('Scheduled automation visibility is read-only')
    expect(doc).toContain('Strategy-signals scheduler visibility is read-only')
    expect(doc).toContain('does not start, stop, or run strategies')
    expect(doc).toContain('Scheduler observation is not live-readiness')
    expect(doc).toContain('Signal/fill observation is backend evidence, not a UI control')
    expect(doc).toContain('scheduler OK is not paper-fill success')
    expect(doc).toContain('Market-regime validity is backend risk evidence, not scheduler health')
    expect(doc).toContain('Paper-fill success is backend execution evidence, not scheduler health')
    expect(doc).toContain('Operator UI may display backend-provided evidence read-only')
    expect(doc).toContain('must never force market regime')
    expect(doc).toContain('must never bypass RiskEngine')
    expect(doc).toContain('Live-readiness remains separate from mock and paper observations')
    expect(doc).toContain('UI must never trigger strategies or fills')
    expect(doc).toContain('npm audit --audit-level=moderate')
  })
})
