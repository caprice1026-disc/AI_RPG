# AI_RPG

[日本語](README.md) · [Development guide (Japanese)](docs/development.md) · [Playtest guide (Japanese)](docs/playtest-guide.md)

AI_RPG is an MVP for a tabletop-style RPG played through free text and suggested actions.
The LLM interprets player intent and narrates confirmed outcomes. The Game Engine calculates checks, dice rolls, HP, and inventory changes; the application persists the results in PostgreSQL.

## What you can do

- Choose a scenario and a preset adventurer in the browser, then start the short adventure “The Holy Seal of the Ruined Chapel.”
- Explore, negotiate, sneak, fight, heal, and face enemy counterattacks, reaching success, success at a cost, retreat, or defeat.
- View HP, inventory, objectives, discovered information, and enemy HP from saved game state.
- Resume a saved adventure as the same player, or revisit previous inputs, narration, and endings.
- Run playtests for users whose OIDC accounts are registered in the application in advance, and optionally save feedback to a text file.

The game UI and scenarios are in Japanese; this English README does not add an English UI.
Fake LLM mode lets you try the suggested actions without an API key. Use a real model such as Gemini for open-ended conversation and varied wording.

This is a short-adventure MVP. The target duration of 20–30 minutes, conversation quality, and enjoyment still require human playtesting.
[Latest verification record (Japanese)](docs/verification-20260926.md) distinguishes automated tests from agent-operated browser checks.

## Quickstart: play locally with Fake LLM

These instructions use Windows PowerShell. Install Git, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/), and start Docker Desktop first.
To use an existing PostgreSQL server, create a separate, empty development database and substitute its connection URL below.

The repository includes the built Vue client, so you do not need Node.js just to play.
This setup is for your own computer only. Do not expose the fixed development principal (player ID) or the HTTP development environment to external users.

### 1. Clone and install dependencies

If you already have a checkout, open its root directory and start at `uv sync --frozen`.

```powershell
uv --version
git clone https://github.com/caprice1026-disc/AI_RPG.git
Set-Location AI_RPG
uv sync --frozen
if (-not (Test-Path -LiteralPath .env)) {
  Copy-Item -LiteralPath .env.example -Destination .env
}
```

`uv sync --frozen` creates the local `.venv`. You do not need to create or activate a virtual environment first.
The conditional copy preserves any existing `.env`. Fake mode does not require an API key.

### 2. Prepare the development database

Run this once for a new development environment. Adventures are stored in the named volume `ai-rpg-dev-data`.
If a container or volume already uses these names, check what it contains before reusing it, or choose different names.

```powershell
docker run --name ai-rpg-dev-postgres `
  -e POSTGRES_USER=airpg `
  -e POSTGRES_PASSWORD=airpg `
  -e POSTGRES_DB=airpg `
  -v ai-rpg-dev-data:/var/lib/postgresql/data `
  -p 127.0.0.1:5432:5432 -d postgres:16
docker exec ai-rpg-dev-postgres pg_isready -U airpg -d airpg
```

Wait for `pg_isready` to report `accepting connections`, then apply migrations from the repository root.
If the readiness check fails immediately after startup, wait briefly and run `pg_isready` again.

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen alembic upgrade head
```

If port 5432 is in use, change the Docker host port and use that same port in every connection URL below.
Alembic does not load `.env` directly, so set `AIRPG_DATABASE_URL` explicitly for migrations too.
Never point the test variable `AIRPG_TEST_DATABASE_URL` at this development database.

### 3. Start the API and both workers

Open three PowerShell terminals and **change to the same repository root in each one**.
Terminal environment variables are independent, so set the same database URL in all three.

Terminal 1 — API:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg api --dev-principal
```

Terminal 2 — action resolution:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg resolution-worker --fake
```

Terminal 3 — result narration:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg narration-worker --fake
```

`--dev-principal` is a loopback-only authentication mode that treats all connections as the same development player.
It is never enabled implicitly for a normal API launch. Use `--dev-principal <UUID>` to test a different development player.

### 4. Play, stop, and resume

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/), choose a scenario, adventurer, and name, then select 「冒険を始める」 (Start adventure).
You do not need Campaign or Actor UUIDs or the `seed-dev` command. Start with the suggested actions and wait for narration to finish before the next input.
Fake mode recognizes only limited text patterns; during combat, 「回復ポーションを飲む」 means “drink a healing potion.”

Stop the API and workers with `Ctrl+C` in each terminal. Stop and restart the database with:

```powershell
docker stop ai-rpg-dev-postgres
# When resuming
docker start ai-rpg-dev-postgres
```

To resume, wait for the database to be ready and restart all three processes from step 3 with the same database and principal.
Select a saved adventure in the browser. Completed adventures and their history remain available.
After updating the code, rerun the migration from step 2 before starting the API and workers.

If a submission has an unknown outcome, use 「同じ要求を再送」 (Resend the same request).
For an accepted turn, resume retrieving its result. See [recovery behavior (Japanese)](docs/development.md#failure-recovery) for details.

## Switch to Gemini

The application uses Pydantic AI and defaults to `google:gemini-3.5-flash`. Real model calls incur provider charges.
Stop both Fake workers with `Ctrl+C`, then edit the following entries in the existing `.env` at the repository root.
Replace the API key placeholder locally; do not overwrite the entire file with this example.

```dotenv
AIRPG_LLM_MODEL=google:gemini-3.5-flash
GEMINI_API_KEY=YOUR_API_KEY
```

Keep keys in the server's `.env` or secret configuration and out of Git.
Do not enter them in the browser or copy them into frontend `VITE_` variables.

Restart terminals 2 and 3 from the same root with the same database settings, removing `--fake` from both commands.
The API can keep running in local development mode.

Terminal 2:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg resolution-worker
```

Terminal 3:

```powershell
$env:AIRPG_DATABASE_URL = "postgresql+psycopg://airpg:airpg@localhost:5432/airpg"
uv run --frozen ai-rpg narration-worker
```

`AIRPG_LLM_MODEL` sets the common model. Set a tier override only when you want a different model for that role.

| Setting | Role |
| --- | --- |
| `AIRPG_FAST_MODEL` | Intent extraction and ordinary conversation |
| `AIRPG_QUALITY_MODEL` | Narration of confirmed results |
| `AIRPG_BACKGROUND_MODEL` | Reserved for future background work; its configured provider still needs a key at startup |

Unset tiers inherit the common model. Existing tier overrides take precedence, so check them when switching models.
Environment variables take precedence over `.env`; restart the workers after changing settings.
The [provider settings guide (Japanese)](docs/development.md#provider-settings) covers OpenAI Responses, key precedence, and call budgets.

## OIDC login for playtest participants

Before giving participants access, register their IdP identities in the application and configure HTTPS.
The [playtest guide (Japanese)](docs/playtest-guide.md) covers browser clients, redirect URIs, registration, disabling identities, logging, and local Keycloak setup.
Do not publish the development principal as participant authentication.

The browser client ID and Bearer API audience are separate settings. The API and both workers must use the same database.
Sessions expire after eight hours and are also invalidated by logout or identity disabling.
Feedback downloads are optional and are not automatically sent to the server.

## Development and tests

Run the database-independent tests and static checks from the repository root:

```powershell
uv run --frozen pytest tests/unit tests/contract -q -p no:cacheprovider
uv run --frozen ruff check .
uv run --frozen mypy src
uv build
```

For the full suite, follow the [dedicated test database instructions (Japanese)](docs/development.md#test-database).
A skipped PostgreSQL test caused by a missing database URL is not evidence of a passing integration test.

Frontend changes require Node.js 22.14 or later:

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
Set-Location ..
```

Commit frontend source changes together with the generated `src/ai_rpg/api/static/vue/` assets.
Verify login, cookies, and SSE using the same-origin client served by FastAPI. See the [frontend README](frontend/README.md) for details.

## Documentation

Most project documentation is in Japanese.

- [Development guide](docs/development.md): architecture, test databases, HTTP/SSE, scenario compatibility, providers, and recovery.
- [Architecture](docs/ai-trpg-architecture-v0.1.md) / [Contracts](docs/ai-trpg-contracts-v0.2.md) / [ADR index](docs/adr/README.md): design and adopted decisions.
- [MVP rules](docs/adr/0007-mvp-ruleset.md) / [Historical Fake LLM implementation record](docs/ai-trpg-fake-llm.md): rules and implementation history.
- [Playtest guide](docs/playtest-guide.md) / [Record template](docs/playtest-record-template.md): separate human playtests from agent verification.
- [Vue UI design](docs/vue-design.md) / [Latest verification](docs/verification-20260926.md) / [September 22–23, 2026 record](docs/verification-stage5-20260922.md): UI design, checks, and limitations.

## Contributing

Check [Issues](https://github.com/caprice1026-disc/AI_RPG/issues) for existing discussions and share the purpose and scope of larger changes before implementing them.
Look for [good first issue](https://github.com/caprice1026-disc/AI_RPG/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) for small tasks, `help wanted` for requests for assistance, and `documentation` for documentation improvements.
Follow [.AGENTS.md](.AGENTS.md) and describe the reason for your change, checks performed, and anything not verified in your pull request.
