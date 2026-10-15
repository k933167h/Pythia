/**
 * Access policy for the OSIRIS → PYTHIA engine proxy (routes/engine-proxy-route.ts).
 * Kept in its own module so it can be unit-tested and so the route file only exports
 * route handlers (Next.js rejects other exports from route.ts).
 */
export type Mode = 'read' | 'operator' | 'admin';

const truthy = (v?: string) => /^(1|true|yes|on)$/i.test(v || '');

export function resolveMode(env: Record<string, string | undefined>): Mode {
  const m = (env.PYTHIA_PROXY_MODE || '').trim().toLowerCase();
  if (m === 'operator' || m === 'admin' || m === 'read') return m;
  // legacy v0.3 switch: PYTHIA_PROXY_ALLOW_ADMIN=true meant "everything"
  return truthy(env.PYTHIA_PROXY_ALLOW_ADMIN) ? 'admin' : 'read';
}

// POSTs that only read engine state (they cost LLM time — the engine rate-limits them)
const READ_SAFE: Array<[string, string]> = [['POST', '/chat'], ['POST', '/whatif']];
// day-to-day controls, allowed from operator mode up
const OPERATOR: Array<[string, string]> = [
  ['POST', '/predict'], ['POST', '/loop'], ['POST', '/model'], ['POST', '/swarm/model'],
  ['POST', '/watchlist'], ['DELETE', '/watchlist'], ['POST', '/alerts'], ['DELETE', '/alerts'],
  ['POST', '/brief/run'], ['POST', '/brief/config'], ['POST', '/scorecard/resolve'],
];
// GETs only an admin-mode dashboard may make
const ADMIN_READS = ['/governance/audit'];

const under = (sub: string, p: string) => sub === p || sub.startsWith(p + '/');
const hit = (rules: Array<[string, string]>, method: string, sub: string) =>
  rules.some(([m, p]) => m === method && under(sub, p));

/** May a browser make this call through the proxy? Anything not listed is refused. */
export function allowed(method: string, sub: string, mode: Mode): boolean {
  if (mode === 'admin') return true;
  if (method === 'GET' || method === 'HEAD') return !ADMIN_READS.some((p) => under(sub, p));
  if (hit(READ_SAFE, method, sub)) return true;
  return mode === 'operator' && hit(OPERATOR, method, sub);
}

/** The end user's address to forward: only when a trusted reverse proxy set
 *  X-Forwarded-For, and only its right-most hop (the one that proxy appended). */
export function forwardedFor(header: string | null, trust: boolean): string | null {
  if (!trust || !header) return null;
  const hops = header.split(',').map((h) => h.trim()).filter(Boolean);
  const who = hops[hops.length - 1];
  return who && /^[0-9a-fA-F:.]{2,45}$/.test(who) ? who : null;
}

export { truthy };
