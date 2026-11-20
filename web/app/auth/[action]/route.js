import {NextResponse} from 'next/server';
import {cookies} from 'next/headers';
import {configuration, logoutUrl, cookieAttributes, sessionCookieHeaders, pkce, seal, unseal, stateMatches, FLOW_COOKIE} from '../../../lib/auth.mjs';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export async function GET(request, {params}) {
  try {
    const config = configuration();
    const {action} = await params;
    const store = await cookies();
    if (action === 'logout') {
      const response = NextResponse.redirect(logoutUrl(config));
      response.cookies.set(FLOW_COOKIE, '', {...cookieAttributes(config), maxAge: 0});
      for (const header of sessionCookieHeaders('', config, 0)) response.headers.append('Set-Cookie', header);
      return response;
    }
    if (config.local) return NextResponse.redirect(config.base);
    if (action === 'login' || action === 'signup') {
      const flow = pkce();
      const url = new URL(action === 'signup' ? '/signup' : '/oauth2/authorize', config.domain);
      url.search = new URLSearchParams({client_id: config.clientId, response_type: 'code', scope: 'openid email profile', redirect_uri: new URL('/auth/callback', config.base).href, state: flow.state, code_challenge: flow.challenge, code_challenge_method: 'S256'}).toString();
      const response = NextResponse.redirect(url);
      response.cookies.set(FLOW_COOKIE, seal(flow, config.secret), {...cookieAttributes(config), maxAge: 600});
      return response;
    }
    if (action !== 'callback') return NextResponse.json({detail: 'Unknown authentication action'}, {status: 404});
    const flow = unseal(store.get(FLOW_COOKIE)?.value, config.secret);
    const query = new URL(request.url).searchParams;
    if (!flow || Date.now() - flow.createdAt > 600000 || !stateMatches(flow.state, query.get('state')) || !query.get('code')) {
      return NextResponse.json({detail: 'Authentication flow expired or state mismatch. Please sign in again.'}, {status: 400});
    }
    const tokenResponse = await fetch(new URL('/oauth2/token', config.domain), {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: new URLSearchParams({grant_type: 'authorization_code', client_id: config.clientId, redirect_uri: new URL('/auth/callback', config.base).href, code: query.get('code'), code_verifier: flow.verifier}), cache: 'no-store'});
    if (!tokenResponse.ok) return NextResponse.json({detail: 'Unable to exchange sign-in code. Please sign in again.'}, {status: 401});
    const token = await tokenResponse.json();
    if (!token.access_token || !Number.isFinite(token.expires_in) || token.expires_in <= 0) throw new Error('Invalid token response');
    if (!token.id_token) throw new Error('Verified profile token missing');
    const profileResponse = await fetch(new URL('/api/me/identity', process.env.API_INTERNAL_URL || 'http://localhost:8000'), {method: 'POST', headers: {'Content-Type': 'application/json', Authorization: `Bearer ${token.access_token}`}, body: JSON.stringify({id_token: token.id_token}), cache: 'no-store', signal: AbortSignal.timeout(10000)});
    if (!profileResponse.ok) throw new Error('Verified profile binding unavailable');
    const response = NextResponse.redirect(config.base);
    response.cookies.set(FLOW_COOKIE, '', {...cookieAttributes(config), maxAge: 0});
    const encrypted = seal({accessToken: token.access_token, expiresAt: Date.now() + token.expires_in * 1000}, config.secret);
    for (const header of sessionCookieHeaders(encrypted, config, token.expires_in)) response.headers.append('Set-Cookie', header);
    return response;
  } catch {
    return NextResponse.json({detail: 'Authentication is not configured or is temporarily unavailable.'}, {status: 503});
  }
}
