import { NextRequest } from 'next/server';
import { allowed, forwardedFor, resolveMode, truthy, type Mode } from '@/lib/proxyPolicy';

/**
 * OSIRIS → PYTHIA engine proxy.
 * Same-origin passthrough so the browser avoids CORS and SSE works cleanly.
 * /api/engine/<path>  ->  ${PYTHIA_ENGINE_URL}/<path>
 *
 * Security (v0.3.1):
 *  - The engine token lives ONLY on the server (PYTHIA_ENGINE_TOKEN) and is injected
 *    here, so it never reaches the browser.
 *  - Secure by default: PYTHIA_PROXY_MODE decides what a browser may do through here.
 *      read      (default)  GET/HEAD + chat/what-if only. Right for any dashboard that
 *                           someone else on the LAN/Tailscale can open.
 *      operator             + day-to-day controls (predict, loop, model, watchlist,
 *                           alert rules, brief schedule). For a dashboard bound to
 *                           127.0.0.1 or behind your own login (run-all.sh sets this
 *                           when it binds the UI to loopback).
 *      admin                + webhooks, human review overrides, the audit trail.
 *  - X-Forwarded-For is forwarded ONLY with PYTHIA_PROXY_TRUST_XFF=true (i.e. when a
 *    reverse proxy you control sets it), and then only its right-most hop — the address
 *    that proxy actually saw. A browser-supplied header is otherwise dropped, so nobody
 *    can pick their own identity for the engine's rate limits and audit trail.
 *  - Paths are normalised; traversal and absolute URLs are refused. Requests time out
 *    and bodies are size-capped.
 */
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const ENGINE = (process.env.PYTHIA_ENGINE_URL || 'http://localhost:8088').replace(/\/$/, '');
const TOKEN = (process.env.PYTHIA_ENGINE_TOKEN || '').trim();
const MODE: Mode = resolveMode(process.env);
const TRUST_XFF = truthy(process.env.PYTHIA_PROXY_TRUST_XFF);
const TIMEOUT_MS = Number(process.env.PYTHIA_PROXY_TIMEOUT_MS || 240_000);   // local LLM calls are slow
const MAX_BODY = 256 * 1024;
const MAX_RESPONSE = 25 * 1024 * 1024;

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

async function proxy(req: NextRequest, path: string[]): Promise<Response> {
  const segs = (path || []).map((p) => decodeURIComponent(p));
  if (segs.some((p) => p === '..' || p === '.' || p.includes('/') || p.includes('\\') || p.includes(':'))) {
    return json(400, { error: 'bad path' });
  }
  const sub = '/' + segs.map(encodeURIComponent).join('/');
  if (!allowed(req.method, sub, MODE)) {
    return json(403, { error: `not allowed through the dashboard proxy (mode: ${MODE}) — ` +
      'set PYTHIA_PROXY_MODE=operator/admin, or call the engine API directly with a token' });
  }
  const url = ENGINE + sub + (req.nextUrl.search || '');
  const isSSE = sub.endsWith('/stream');

  const headers: Record<string, string> = { 'Content-Type': req.headers.get('content-type') || 'application/json' };
  if (TOKEN) headers['Authorization'] = `Bearer ${TOKEN}`;
  // tell the engine who the end user is — only when a reverse proxy we trust set the
  // header, and only the right-most hop (the one that proxy appended itself)
  const who = forwardedFor(req.headers.get('x-forwarded-for'), TRUST_XFF);
  if (who) headers['X-Forwarded-For'] = who;
  const init: RequestInit = {
    method: req.method,
    headers,
    // SSE streams stay open by design; everything else gets a deadline
    signal: isSSE ? req.signal : AbortSignal.any([req.signal, AbortSignal.timeout(TIMEOUT_MS)]),
    // @ts-expect-error - duplex required by undici for streaming request bodies
    duplex: 'half',
  };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    if (Number(req.headers.get('content-length') || 0) > MAX_BODY) return json(413, { error: 'request body too large' });
    const body = await req.text();
    if (body.length > MAX_BODY) return json(413, { error: 'request body too large' });
    init.body = body;
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
    if (Number(upstream.headers.get('content-length') || 0) > MAX_RESPONSE) {
      return json(502, { error: 'engine response too large' });
    }
    const body = await upstream.text();
    const outHeaders: Record<string, string> = { 'Content-Type': upstream.headers.get('content-type') || 'application/json' };
    const ai = upstream.headers.get('x-ai-generated');
    if (ai) outHeaders['X-AI-Generated'] = ai;
    return new Response(body, { status: upstream.status, headers: outHeaders });
  } catch (e) {
    if (e instanceof Error && e.name === 'TimeoutError') return json(504, { error: 'PYTHIA engine timed out' });
    console.error('[pythia proxy] engine unreachable:', e);
    return json(502, { error: 'PYTHIA engine unreachable' });
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
