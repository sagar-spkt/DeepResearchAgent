import {cookies} from 'next/headers';
import {configuration, sessionHeaders, sameOriginMutation, hasDuplicateSessionCookies, SESSION_COOKIE} from '../../../lib/auth.mjs';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
async function proxy(request, {params}) {
  try {
    const config = configuration();
    if (hasDuplicateSessionCookies(request.headers.get('cookie') || '')) return Response.json({detail: 'Conflicting sessions. Sign out and sign in again.'}, {status: 401});
    if (!sameOriginMutation(request, config)) return Response.json({detail: 'Invalid request origin'}, {status: 403});
    const auth = sessionHeaders((await cookies()).get(SESSION_COOKIE)?.value, config);
    if (!auth) return Response.json({detail: 'Sign in to continue'}, {status: 401});
    const {path} = await params;
    if (!path?.length || path.some(part => !/^[A-Za-z0-9_-]+$/.test(part))) return Response.json({detail: 'Invalid API path'}, {status: 400});
    const target = new URL(`/api/${path.join('/')}`, process.env.API_INTERNAL_URL || 'http://localhost:8000');
    target.search = new URL(request.url).search;
    const headers = new Headers(auth);
    for (const name of ['content-type', 'last-event-id']) if (request.headers.has(name)) headers.set(name, request.headers.get(name));
    const options = {method: request.method, headers, cache: 'no-store', signal: request.signal};
    if (!['GET', 'HEAD'].includes(request.method)) { options.body = request.body; options.duplex = 'half'; }
    const upstream = await fetch(target, options);
    return new Response(upstream.body, {status: upstream.status, headers: {'Content-Type': upstream.headers.get('content-type') || 'application/json', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}});
  } catch {
    return Response.json({detail: 'The research service is unavailable. Please try again.'}, {status: 503});
  }
}
export {proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE};
