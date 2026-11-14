# Research Desk frontend

Next.js App Router + assistant-ui external-store runtime. User state and access rules are owned by FastAPI; no Assistant Cloud dependency.

## Run

Copy `.env.example` to `.env.local`, install dependencies, then `npm run dev`. Local real model calls belong to the backend. The frontend defaults to server-side injection of `DEV_USER=demo@example.test` only when both `APP_ENV=local` and `AUTH_MODE=local` are explicitly configured. Use the same identity configured by the backend.

For AWS, use `AUTH_MODE=cognito`, `APP_ENV=staging` or `production`, HTTPS `APP_BASE_URL`, `COGNITO_DOMAIN`, `COGNITO_CLIENT_ID`, and a random `AUTH_SESSION_SECRET` of at least 32 characters. Configure a public Cognito client without client secret, OAuth authorization-code flow and openid/email/profile scopes, callback `/auth/callback`. Hosted UI provides signup, verification and password recovery. Token exchange is server-side PKCE with encrypted state cookie; the access token is encrypted in an HttpOnly SameSite=Lax cookie. Sessions expire with the access token; sign in again on expiry. Backend JWT validation remains mandatory. Signing out clears the app session; the Cognito hosted session can remain valid.

`API_INTERNAL_URL` is a server-only constant origin for FastAPI (e.g. `http://api:8000`). Every `/api/*` request is proxied server-side. Browser-supplied authorization and development identity headers are discarded. Mutation requests require Origin equal to configured APP_BASE_URL. This is not a replacement for backend authorization. No Cognito client secret or AWS credentials are placed in browser bundles.

Chat renders through assistant-ui. Reports use react-markdown with raw HTML disabled. Original documents and memory CRUD are server-owned. Progress is authenticated SSE with bounded reconnect and replay cursor; conversation detail supplies active_run/latest_run for reload recovery. Documents uploaded while viewing a conversation attach to it; library reuse requires explicit Attach.

## Verification and limitations

`npm test` executes offline Node behavior tests for local-auth rejection, encrypted sessions, expiration, state, CSRF, upload limits and chunked SSE. `npm run check` syntax-checks standalone library modules. `npm run build` requires dependency installation; browser/API integration and Cognito live flow require a running environment.

Direct dependencies are pinned in package.json. A complete lockfile could not be resolved in the restricted development environment: registry DNS is unavailable and assistant-ui is absent from npm cache. Resolve and commit package-lock.json in a network-enabled environment, use npm ci, and pin Docker base image digests before production release. Dockerfile currently uses npm install and is a development build recipe, not a reproducible release artifact. No claim of successful full build or browser testing is made until those checks run.
