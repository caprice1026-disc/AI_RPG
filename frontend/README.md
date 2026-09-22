# Vue adventure client

Run from this directory with Node 22.14 or later:

```powershell
npm.cmd ci
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
```

The build writes only `../src/ai_rpg/api/static/vue/`. Commit that generated directory together with the source for Python-only installations. The backend serves its `index.html` at `/`; all built assets use `/static/vue/`. The existing legacy static files are outside this client's ownership.

`npm.cmd run dev` starts Vite for frontend development. Gameplay and login use same-origin absolute URLs, so end-to-end testing uses the Python-served production build with the backend's configured authentication origin. This project does not configure another authentication origin or read application secrets.

`src/game.ts` owns the session, public DTOs, pending operation, request cancellation and turn tracking. `App.vue`, `TurnRecord.vue` and `Feedback.vue` render that state with native form controls. There is no router, store framework, remote font, CDN or analytics dependency.

Pending request bodies are saved before POST under the authenticated principal's namespace. Unknown POST outcomes need explicit replay with the original body. Accepted turns use GET/SSE. Campaign changes, logout and unmount stop tracking; session expiration hides private data but keeps the principal's pending request for reauthentication. Polling stops after at most 30 GET attempts per tracking attempt, and the UI offers read-only result recovery.

Tests run the actual Vue components, state module and browser storage. Only external HTTP, SSE, time and download boundaries are controlled. Recovery cases from the legacy browser tests are covered in `tests/game.test.ts` and `tests/recovery.test.ts`; `tests/app.test.ts` covers the rendered flows and display refinements.
