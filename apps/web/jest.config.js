const nextJest = require('next/jest')

const createJestConfig = nextJest({ dir: './' })

const config = {
  coverageProvider: 'v8',
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/tests/unit/setup.ts'],
  moduleNameMapper: { '^@/(.*)$': '<rootDir>/$1' },
  testMatch: ['<rootDir>/tests/unit/**/*.test.{ts,tsx}'],
  passWithNoTests: true,
}

module.exports = createJestConfig(config)
