import {test} from 'node:test';
import assert from 'node:assert/strict';
import {configuration, seal, unseal, sessionHeaders, stateMatches, pkce, sameOriginMutation, cookieAttributes, hasDuplicateSessionCookies, sessionCookieHeaders} from '../lib/auth.mjs';
import {eventParser} from '../lib/sse.mjs';
import {validateUpload, terminal, safeConsoleUrl} from '../lib/api.mjs';
const secret = 'a'.repeat(64);
test('local auth cannot be enabled in AWS environments', () => {
  for (const APP_ENV of ['staging','production']) assert.throws(() => configuration({APP_ENV,AUTH_MODE:'local'}));
  assert.equal(configuration({APP_ENV:'local',AUTH_MODE:'local'}).devUser,'demo@example.test');
});
test('cloud requires configuration, encryption and unexpired server session', () => {
  assert.throws(() => configuration({APP_ENV:'production'}));
  const config = {local:false,secret};
  const cookie = seal({accessToken:'synthetic',expiresAt:100},secret);
  assert.deepEqual(sessionHeaders(cookie,config,99),{Authorization:'Bearer synthetic'});
  assert.equal(sessionHeaders(cookie,config,100),null);
  assert.equal(unseal(cookie,'wrong'),null);
  const bytes = Buffer.from(cookie,'base64url'); bytes[30] ^= 1;
  assert.equal(unseal(bytes.toString('base64url'),secret),null);
  assert.throws(() => seal({},'short'));
});
test('PKCE state has independent randomness and rejects missing/mismatched state', () => {
  const flow = pkce(); const next = pkce();
  assert.notEqual(flow.verifier,next.verifier); assert.notEqual(flow.state,flow.verifier);
  assert.equal(flow.challenge.length,43); assert.equal(stateMatches(flow.state,flow.state),true);
  assert.equal(stateMatches(flow.state,null),false); assert.equal(stateMatches(flow.state,next.state),false);
});
test('mutation requires trusted origin while reads do not', () => {
  const config={base:new URL('https://research.example')};
  assert.equal(sameOriginMutation(new Request('https://research.example/api/me'),config),true);
  assert.equal(sameOriginMutation(new Request('https://research.example/api/memories',{method:'POST',headers:{origin:'https://attacker.example'}}),config),false);
  assert.equal(sameOriginMutation(new Request('https://research.example/api/memories',{method:'POST',headers:{origin:'https://research.example'}}),config),true);
});
test('SSE parser preserves split frames, ids and JSON including CRLF', () => {
  const events=[]; const parse=eventParser(e=>events.push(e));
  parse(': keepalive\r\n\r\nid: 17\r\nevent: token\r\ndata: {"token":"A"}\r');
  parse('\n\r\nid: 18\nevent: completed\ndata: {"result":"Done"}\n\n');
  assert.deepEqual(events,[{id:'17',event:'token',data:{token:'A'}},{id:'18',event:'completed',data:{result:'Done'}}]);
});
test('upload limits and terminal statuses are explicit', () => {
  assert.equal(validateUpload({name:'notes.md',size:30}),null);
  assert.ok(validateUpload({name:'notes.exe',size:30}));
  assert.ok(validateUpload({name:'notes.pdf',size:21*1024*1024}));
  assert.equal(terminal('completed'),true); assert.equal(terminal('running'),false);
});

const cloudEnv={APP_ENV:'staging', AUTH_MODE:'cognito', AUTH_SESSION_SECRET:secret, APP_BASE_URL:'https://research.example.test', COGNITO_DOMAIN:'https://identity.auth.us-east-1.amazoncognito.com', COGNITO_CLIENT_ID:'client'};
test('only an optional exact cloud child host enables a derived session cookie domain', () => {
  const normal=configuration(cloudEnv);
  assert.equal(cookieAttributes(normal,{sharedSession:true}).domain,undefined);
  const shared=configuration({...cloudEnv,OBSERVABILITY_HOST:'observability.research.example.test', COOKIE_DOMAIN:'.example.test'});
  assert.equal(cookieAttributes(shared,{sharedSession:true}).domain,'research.example.test');
  assert.deepEqual(cookieAttributes(shared),{httpOnly:true,sameSite:'lax',secure:true,path:'/'});
  assert.equal(cookieAttributes(shared,{sharedSession:true}).secure,true);
});
test('shared session configuration rejects broad, foreign, non-HTTPS and local hosts', () => {
  for(const OBSERVABILITY_HOST of ['example.test','observability.example.test','observability.research.example.test.evil.test','https://observability.research.example.test','observability.research.example.test:443']) {
    assert.throws(()=>configuration({...cloudEnv,OBSERVABILITY_HOST}));
  }
  assert.throws(()=>configuration({...cloudEnv,APP_BASE_URL:'http://research.example.test',OBSERVABILITY_HOST:'observability.research.example.test'}));
  assert.throws(()=>configuration({...cloudEnv,APP_ENV:'local',AUTH_MODE:'local',OBSERVABILITY_HOST:'observability.research.example.test'}));
  const local=configuration({APP_ENV:'local',AUTH_MODE:'local'});
  assert.equal(cookieAttributes(local,{sharedSession:true}).domain,undefined);
  assert.equal(cookieAttributes(local).domain,undefined);
});
test('console links permit only the configured application HTTPS child at root', () => {
  assert.equal(safeConsoleUrl('https://observability.research.example.test/','https://research.example.test/'),'https://observability.research.example.test/');
  for (const url of ['http://observability.research.example.test/','https://evil.test/','https://observability.example.test/','javascript:alert(1)','https://observability.research.example.test/login','https://user@observability.research.example.test/']) assert.equal(safeConsoleUrl(url,'https://research.example.test/'),null);
  assert.equal(safeConsoleUrl('https://observability.localhost/','http://localhost:3000/'),null);
});
test('duplicate application cookies are rejected before session selection', () => {
  assert.equal(hasDuplicateSessionCookies('research_session=old; research_session=new; native=x'),true);
  assert.equal(hasDuplicateSessionCookies('native=x; research_session=old; other=y; research_session=new'),true);
  assert.equal(hasDuplicateSessionCookies('native=x; research_session=new'),false);
  assert.equal(hasDuplicateSessionCookies('not_research_session=x; native=y'),false);
});
test('shared session migration clears legacy cookie before issuing the new cookie', () => {
  const config=configuration({...cloudEnv,OBSERVABILITY_HOST:'observability.research.example.test'});
  const headers=sessionCookieHeaders('sealed_token',config,3600);
  assert.equal(headers.length,2);
  assert.ok(headers[0].includes('Max-Age=0'));
  assert.ok(!headers[0].includes('Domain='));
  assert.ok(headers[1].includes('research_session=sealed_token'));
  assert.ok(headers[1].includes('Domain=research.example.test'));
  assert.ok(headers.every(value=>value.includes('HttpOnly')&&value.includes('Secure')));
  const logout=sessionCookieHeaders('',config,0);
  assert.equal(logout.length,2);
  assert.ok(logout.every(value=>value.includes('Max-Age=0')));
  assert.equal(sessionCookieHeaders('sealed_token',configuration(cloudEnv),3600).length,1);
  assert.throws(()=>sessionCookieHeaders('injected\r\nheader',config,1));
});
