import '@testing-library/jest-dom'

declare namespace jest {
  interface Matchers<R, T = unknown> {
    toBeInTheDocument(): R
    toBeEmptyDOMElement(): R
    toHaveValue(value?: string | number | string[]): R
  }
}
