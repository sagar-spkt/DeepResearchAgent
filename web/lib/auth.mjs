import {isIP} from 'node:net';
import {createHash, randomBytes, createCipheriv, createDecipheriv, timingSafeEqual} from 'node:crypto';

export const SESSION_COOKIE = 'research_session';
export const FLOW_COOKIE = 'research_auth_flow';
export function configuration(env = process.env) {
  const environment = env.APP_ENV || 'local';
  const local = env.AUTH_MODE === 'local';
  if (local && environment !== 'local') throw new Error('Local authentication is allowed only in local environment');
  if (!local && !env.AUTH_SESSION_SECRET) throw new Error('AUTH_SESSION_SECRET is required');
  const base = new URL(env.APP_BASE_URL || 'http://localhost:3000');
  if (!local && environment !== 'local' && base.protocol !== 'https:') throw new Error('HTTPS APP_BASE_URL is required');
  if (!local && (!env.COGNITO_DOMAIN || !env.COGNITO_CLIENT_ID)) throw new Error('Cognito domain and client ID are required');
  const domain = env.COGNITO_DOMAIN ? new URL(env.COGNITO_DOMAIN) : null;
  if (domain && domain.protocol !== 'https:') throw new Error('Cognito domain must use HTTPS');
  const observabilityHost = env.OBSERVABILITY_HOST || null;
  if (observabilityHost && (local || environment === 'local' || base.protocol !== 'https:' || isIP(base.hostname) || observabilityHost !== `observability.${base.hostname}`)) {
    throw new Error('OBSERVABILITY_HOST must be the exact HTTPS cloud child of APP_BASE_URL');
  }
  return {local, environment, base, domain, observabilityHost, clientId: env.COGNITO_CLIENT_ID, secret: env.AUTH_SESSION_SECRET, devUser: env.DEV_USER || env.NEXT_PUBLIC_DEV_USER || 'demo@example.test'};
}
export function cookieAttributes(config, {sharedSession = false} = {}) {
  const attributes = {httpOnly: true, sameSite: 'lax', secure: config.base.protocol === 'https:', path: '/'};
  // Derive the sole permitted cookie domain; no operator-supplied root domain is accepted.
  if (sharedSession && config.observabilityHost && !config.local && config.environment !== 'local') {
    if (config.base.protocol !== 'https:' || config.observabilityHost !== `observability.${config.base.hostname}` || isIP(config.base.hostname)) throw new Error('Invalid shared session host');
    attributes.domain = config.base.hostname;
  }
  return attributes;
}
export function hasDuplicateSessionCookies(raw = '') {
  return raw.split(';').filter(part => part.trim().startsWith(`${SESSION_COOKIE}=`)).length > 1;
}
export function sessionCookieHeaders(value, config, maxAge) {
  if (!Number.isInteger(maxAge) || maxAge < 0 || maxAge > 86400 || (maxAge > 0 && !/^[A-Za-z0-9_-]+$/.test(value))) throw new Error('Invalid session cookie');
  const host = cookieAttributes(config);
  const shared = cookieAttributes(config, {sharedSession: true});
  const serialize = (token, age, attributes) => `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${age}${attributes.secure ? '; Secure' : ''}${attributes.domain ? `; Domain=${attributes.domain}` : ''}`;
  if (!shared.domain) return [serialize(maxAge ? value : '', maxAge, host)];
  // Cookie identity can differ across browser implementations during migration.
  // Clear the legacy host cookie BEFORE issuing the shared session, never after.
  if (maxAge) return [serialize('', 0, host), serialize(value, maxAge, shared)];
  return [serialize('', 0, shared), serialize('', 0, host)];
}
export function pkce() {
  const verifier = randomBytes(32).toString('base64url');
  return {verifier, state: randomBytes(32).toString('base64url'), challenge: createHash('sha256').update(verifier).digest('base64url'), createdAt: Date.now()};
}
export function stateMatches(expected, actual) {
  if (typeof expected !== 'string' || typeof actual !== 'string') return false;
  const a = Buffer.from(expected); const b = Buffer.from(actual);
  return a.length === b.length && timingSafeEqual(a, b);
}
export function seal(value, secret) {
  if (!secret || secret.length < 32) throw new Error('Session secret must contain at least 32 characters');
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', createHash('sha256').update(secret).digest(), iv);
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(value)), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), ciphertext]).toString('base64url');
}
export function unseal(token, secret) {
  try {
    if (!secret) return null;
    const bytes = Buffer.from(token || '', 'base64url');
    const decipher = createDecipheriv('aes-256-gcm', createHash('sha256').update(secret).digest(), bytes.subarray(0, 12));
    decipher.setAuthTag(bytes.subarray(12, 28));
    return JSON.parse(Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]).toString());
  } catch { return null; }
}
export function sessionHeaders(cookie, config, now = Date.now()) {
  if (config.local) return {'X-Dev-User': config.devUser};
  const session = unseal(cookie, config.secret);
  if (!session?.accessToken || !session.expiresAt || session.expiresAt <= now) return null;
  return {Authorization: `Bearer ${session.accessToken}`};
}
export function sameOriginMutation(request, config) {
  if (['GET', 'HEAD', 'OPTIONS'].includes(request.method)) return true;
  return request.headers.get('origin') === config.base.origin;
}
