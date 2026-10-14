import { NextRequest } from 'next/server';

/**
 * OSIRIS → PYTHIA engine proxy.
 * Same-origin passthrough so the browser avoids CORS and SSE works cleanly.
 * /api/engine/<path>  ->  ${PYTHIA_ENGINE_URL}/<path>
 *
 * v0.3 security:
 *  - The engine token lives ONLY on the server (PYTHIA_ENGINE_TOKEN) and is injected
 *    here, so it never reaches the browser.
 *  - Admin-only surfaces (webhook registration, human review overrides, the audit
 *    trail) are blocked from the browser unless PYTHIA_PROXY_ALLOW_ADMIN=true — the
 *    dashboard is often exposed over Tailscale/LAN, and those calls belong to operators
 *    using the engine API directly.
 *  - Paths are normalised; traversal and absolute URLs are refused.
 */
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const ENGINE = (process.env.PYTHIA_ENGINE_URL || 'http://localhost:8088').replace(/\/$/, '');
const TOKEN = (process.env.PYTHIA_ENGINE_TOKEN || '').trim();
const ALLOW_ADMIN = /^(1|true|yes|on)$/i.test(process.env.PYTHIA_PROXY_ALLOW_ADMIN || '');
// Shared/LAN dashboards: refuse every state-changing call except chat/what-if.
const READ_ONLY = /^(1|true|yes|on)$/i.test(process.env.PYTHIA_PROXY_READ_ONLY || '');
const READ_SAFE_POSTS = new Set(['/chat', '/whatif']);

// [method or '*', path prefix] pairs the browser may not reach through this proxy
const ADMIN_ONLY: Array<[string, string]> = [
  ['POST', '/webhooks'], ['DELETE', '/webhooks'],
  ['POST', '/governance/review'], ['*', '/governance/audit'],
];

function blocked(method: string, sub: string): boolean {
  if (READ_ONLY && method !== 'GET' && method !== 'HEAD' && !READ_SAFE_POSTS.has(sub)) return true;
  if (ALLOW_ADMIN) return false;
  return ADMIN_ONLY.some(([m, p]) => (m === '*' || m === method) && (sub === p || sub.startsWith(p + '/')));
}

async function proxy(req: NextRequest, path: string[]): Promise<Response> {
  const segs = (path || []).map((p) => decodeURIComponent(p));
  if (segs.some((p) => p === '..' || p === '.' || p.includes('/') || p.includes('\\') || p.includes(':'))) {
    return new Response(JSON.stringify({ error: 'bad path' }), { status: 400, headers: { 'Content-Type': 'application/json' } });
  }
  const sub = '/' + segs.map(encodeURIComponent).join('/');
  if (blocked(req.method, sub)) {
    return new Response(JSON.stringify({ error: 'admin-only endpoint — call the engine API directly with an admin token' }),
      { status: 403, headers: { 'Content-Type': 'application/json' } });
  }
  const url = ENGINE + sub + (req.nextUrl.search || '');
  const isSSE = sub.endsWith('/stream');

  const headers: Record<string, string> = { 'Content-Type': req.headers.get('content-type') || 'application/json' };
  if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
  // tell the engine who the end user is, so rate limits and the audit trail are per person
  const fwd = (req.headers.get('x-forwarded-for') || req.headers.get('x-real-ip') || '').split(',')[0].trim();
  if (fwd) headers['X-Forwarded-For'] = fwd;
  const init: RequestInit = {
    method: req.method,
    headers,
    // @ts-expect-error - duplex required by undici for streaming request bodies
    duplex: 'half',
  };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    init.body = await req.text();
  }

  try {
    const upstream = await fetch(url, init);
    if (isSSE) {
      // Explicitly pump chunks so the dev server doesn't buffer the stream.
      const stream = new ReadableStream({
        async start(controller) {
          const reader = upstream.body!.getReader();
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              controller.enqueue(value);
            }
          } catch { /* upstream closed */ } finally {
            try { controller.close(); } catch { /* already closed */ }
          }
        },
        cancel() { try { upstream.body?.cancel(); } catch { /* noop */ } },
      });
      return new Response(stream, {
        status: upstream.status,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache, no-transform',
          Connection: 'keep-alive',
          'X-Accel-Buffering': 'no',
        },
      });
    }
    const body = await upstream.text();
    const outHeaders: Record<string, string> = { 'Content-Type': upstream.headers.get('content-type') || 'application/json' };
    const ai = upstream.headers.get('x-ai-generated');
    if (ai) outHeaders['X-AI-Generated'] = ai;
    return new Response(body, { status: upstream.status, headers: outHeaders });
  } catch (e) {
    console.error('[pythia proxy] engine unreachable:', e);
    return new Response(JSON.stringify({ error: 'PYTHIA engine unreachable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, (await ctx.params).path);
}
export async function POST(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, (await ctx.params).path);
}
// DELETE was missing before v0.3, so removing a watchlist symbol or alert rule from the
// dashboard silently 405'd.
export async function DELETE(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(req, (await ctx.params).path);
}
