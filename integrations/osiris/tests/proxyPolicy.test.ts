// Run: node --experimental-strip-types --test integrations/osiris/tests/*.test.ts   (Node ≥ 22.6)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { allowed, forwardedFor, resolveMode } from '../lib/proxyPolicy.ts';

test('read mode (default): reads + chat/what-if only', () => {
  const m = resolveMode({});
  assert.equal(m, 'read');
  assert.ok(allowed('GET', '/predictions', m));
  assert.ok(allowed('GET', '/state/stream', m));
  assert.ok(allowed('POST', '/chat', m));
  assert.ok(allowed('POST', '/whatif', m));
  for (const [meth, p] of [['POST', '/model'], ['POST', '/loop'], ['POST', '/predict'], ['DELETE', '/watchlist/SPY'],
                           ['POST', '/alerts'], ['POST', '/webhooks'], ['POST', '/governance/review/x']]) {
    assert.equal(allowed(meth, p, m), false, `${meth} ${p}`);
  }
  assert.equal(allowed('GET', '/governance/audit', m), false);
  assert.equal(allowed('GET', '/governance/audit/verify', m), false);
  assert.ok(allowed('GET', '/governance/system-card', m));
});

test('operator mode: day-to-day controls, never admin surfaces', () => {
  const m = resolveMode({ PYTHIA_PROXY_MODE: 'operator' });
  assert.ok(allowed('POST', '/model', m));
  assert.ok(allowed('DELETE', '/watchlist/SPY', m));
  assert.ok(allowed('DELETE', '/alerts/r1', m));
  assert.equal(allowed('POST', '/webhooks', m), false);
  assert.equal(allowed('DELETE', '/webhooks', m), false);
  assert.equal(allowed('POST', '/governance/review/x', m), false);
  assert.equal(allowed('POST', '/modelx', m), false);          // prefix match is per path segment
  assert.equal(allowed('PUT', '/model', m), false);            // unknown method/path → refused
});

test('admin mode and legacy switch', () => {
  assert.equal(resolveMode({ PYTHIA_PROXY_ALLOW_ADMIN: 'true' }), 'admin');
  assert.equal(resolveMode({ PYTHIA_PROXY_MODE: 'bogus' }), 'read');
  assert.ok(allowed('POST', '/webhooks', 'admin'));
});

test('X-Forwarded-For: dropped unless trusted, then right-most hop only', () => {
  assert.equal(forwardedFor('1.2.3.4', false), null);                       // spoofed by a browser
  assert.equal(forwardedFor('6.6.6.6, 100.64.0.7', true), '100.64.0.7');    // client-forged left hop ignored
  assert.equal(forwardedFor('evil<script>', true), null);
  assert.equal(forwardedFor(null, true), null);
});
