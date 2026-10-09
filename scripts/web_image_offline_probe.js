#!/usr/bin/env node
/**
 * Runs inside a web container started with `--network none`.
 *
 * Negative controls: the container has only a loopback interface, an outbound
 * TCP connection has no route, and DNS resolution fails.
 * Positive control: the server answers /auth/login on loopback with the
 * security headers.
 *
 * Usage: docker exec -i <container> node - < scripts/web_image_offline_probe.js
 */
'use strict'

const dns = require('node:dns')
const net = require('node:net')
const os = require('node:os')

const LOGIN_URL = 'http://127.0.0.1:3000/auth/login'
const READY_ATTEMPTS = 30
const READY_INTERVAL_MS = 1000
const OUTBOUND_TIMEOUT_MS = 3000
// 192.0.2.0/24 is reserved for documentation (RFC 5737) and is never routed.
const OUTBOUND_TARGET = { host: '192.0.2.1', port: 443 }
const DNS_TARGET = 'registry.npmjs.org'
const NO_ROUTE_CODES = ['ENETUNREACH', 'EHOSTUNREACH']
const DNS_FAILURE_CODES = ['EAI_AGAIN', 'ENOTFOUND']
const REQUIRED_HEADERS = {
  'x-content-type-options': /^nosniff$/,
  'x-frame-options': /^DENY$/,
  'referrer-policy': /^no-referrer$/,
  'content-security-policy': /default-src 'self'/,
}

function fail(message) {
  console.error(`FAIL: ${message}`)
  process.exit(1)
}

function outboundResult() {
  return new Promise((resolve) => {
    const socket = net.connect({ ...OUTBOUND_TARGET, timeout: OUTBOUND_TIMEOUT_MS })
    socket.on('connect', () => { socket.destroy(); resolve('connected') })
    socket.on('timeout', () => { socket.destroy(); resolve('timeout') })
    socket.on('error', (error) => resolve(error.code))
  })
}

function dnsResult() {
  return new Promise((resolve) => {
    dns.lookup(DNS_TARGET, (error, address) => resolve(error ? error.code : `resolved ${address}`))
  })
}

async function loginResponse() {
  let lastError = 'no attempt made'
  for (let attempt = 1; attempt <= READY_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(LOGIN_URL, { redirect: 'manual' })
      if (response.status === 200) return response
      lastError = `HTTP ${response.status}`
    } catch (error) {
      lastError = String(error.cause?.code || error.message)
    }
    await new Promise((resolve) => setTimeout(resolve, READY_INTERVAL_MS))
  }
  return fail(`${LOGIN_URL} did not return 200 (last: ${lastError})`)
}

async function main() {
  const external = Object.entries(os.networkInterfaces())
    .flatMap(([name, addresses]) => (addresses || []).filter((address) => !address.internal).map(() => name))
  if (external.length > 0) fail(`non-loopback interface present: ${[...new Set(external)].join(', ')}`)
  console.log('interfaces: loopback only')

  const outbound = await outboundResult()
  if (!NO_ROUTE_CODES.includes(outbound)) fail(`outbound connection was not refused for lack of a route: ${outbound}`)
  console.log(`outbound ${OUTBOUND_TARGET.host}:${OUTBOUND_TARGET.port}: ${outbound}`)

  const lookup = await dnsResult()
  if (!DNS_FAILURE_CODES.includes(lookup)) fail(`DNS lookup did not fail: ${lookup}`)
  console.log(`dns ${DNS_TARGET}: ${lookup}`)

  const response = await loginResponse()
  console.log(`${LOGIN_URL}: ${response.status}`)
  for (const [name, pattern] of Object.entries(REQUIRED_HEADERS)) {
    const value = response.headers.get(name)
    if (value === null || !pattern.test(value)) fail(`header ${name} missing or unexpected: ${value}`)
    console.log(`${name}: ${value.length > 80 ? `${value.slice(0, 80)}...` : value}`)
  }
  console.log('OK: egress denied, login served on loopback with security headers')
}

main().catch((error) => fail(error.stack || String(error)))
