# AI_RPG

[日本語](README.md) · [Development guide (Japanese)](docs/development.md) · [Playtest guide (Japanese)](docs/playtest-guide.md)

AI_RPG is a tabletop-style RPG MVP for playing short adventures and authoring your own in the browser.
The LLM proposes actions and narrates confirmed results. The Game Engine calculates checks, dice, HP, and inventory changes; the application persists them in PostgreSQL.

## Start with Docker Compose

On Windows, use PowerShell, Docker Desktop, and Git. Python and Node.js are not required just to play.

```powershell
git clone https://github.com/caprice1026-disc/AI_RPG.git
Set-Location AI_RPG
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
```

Preserve any existing `.env` and set your `GEMINI_API_KEY` locally. The default model is `google:gemini-3.5-flash`; real model calls incur charges. Never put keys in the browser or Git.
`AIRPG_LLM_MODEL` sets the shared model. `AIRPG_FAST_MODEL`, `AIRPG_QUALITY_MODEL`, and `AIRPG_BACKGROUND_MODEL` override intent interpretation, narration, and story-authoring assistance respectively.

```powershell
docker compose up --build -d
docker compose ps
```

Compose runs PostgreSQL, migrations, the API, and three workers: resolution, narration, and authoring.
Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/). If that port is occupied, set a different `AIRPG_PORT`, such as `18769`, in `.env` and use that port after starting.

This configuration uses HTTP and one fixed development player, bound to this computer's loopback interface. Do not expose it to the internet. For key-free Fake LLM and manual startup, see the [development guide](docs/development.md#manual-local).

## Play and discover stories

Start either built-in adventure, “The Holy Seal of the Ruined Chapel” or “The Lighthouse Logbook,” or choose a story in 「公開作品を探す」 (Find public stories). Search titles, synopses, and tags, then review the description and content warnings. Public details are readable without login; starting an adventure requires login.

Choose an adventurer name, preset, ability allocation, and specialty. No UUID or database entry is needed. Suggested actions are shortcuts, not the only permitted approaches: try exploration, negotiation, and other plausible actions in free text. Failures can affect alert and elapsed actions; major risks require confirmation. Inspect saved HP, inventory, discoveries, and turn history, or resume as the same player.

The UI and built-in stories are in Japanese. Fake LLM recognizes only a few free-text examples. Waiting animations never show uncommitted dice; confirmed rolls can appear while narration is still pending.

## Create your own story

1. Open 「マイ作品」 (My stories), choose a template or 「空の原稿から作る」 (Start from a blank draft), and edit the world, places, characters, items, actions, conditions, and endings.
2. Changes autosave after two seconds. Check the saved status. Incomplete drafts are allowed; conflicts preserve your input for comparison with the saved revision. Revision restore and duplication are available.
3. Select 「原稿を検証」 (Validate draft) to check structure, references, and registered-action paths. Read errors, warnings, and coverage. This step does not call an LLM.
4. Start a playtest from the saved draft. Later edits do not change the definition used by that adventure.
5. Play through at least one ending yourself, acknowledge warnings, and confirm the content before publishing. Choose private, unlisted, or public visibility. Unlisted stories stay out of discovery. Withdrawing a story stops new starts; existing adventures keep their pinned version.

AI assistance runs only when explicitly requested: consistency checks, gap-filling proposals, or an outline from your instructions. Edit and approve an outline before requesting concretization. After reloading, use the history to return to running jobs and check their progress, or select previous results to review. Compare proposed changes and adopt only those you select. Autosave never calls AI, and AI neither applies edits nor publishes on its own. These jobs require `authoring-worker`.

Structural validation does not guarantee every freeform route, narrative meaning, secrecy, game balance, or enjoyment. The author's publication acknowledgement is a human decision, not something automated tests or agent playthroughs establish. See the [storage and publishing details](docs/development.md#story-authoring).

## Stop, resume, and operating limits

```powershell
docker compose down
docker compose up --build -d
docker compose logs --tail=80 api resolution-worker narration-worker authoring-worker
```

`down` preserves the named database volume. Do not use `down -v` if you want to retain adventures, drafts, and releases.

OIDC registration is disabled by default (`AIRPG_REGISTRATION_ENABLED=false`). Only when the operator enables it can users explicitly register through `/auth/login?join=true`. Ordinary login and Bearer API requests do not auto-register unknown identities. Participant access needs HTTPS and normal OIDC authentication, never the fixed development player.

Daily and concurrency limits are documented in [.env.example](.env.example) and the [development guide](docs/development.md#authoring-limits). Human authoring/playtest acceptance and the public HTTPS acceptance gate in [Issue #19](https://github.com/caprice1026-disc/AI_RPG/issues/19) remain unresolved. The intended 20–30-minute duration, conversation quality, and enjoyment also need human evaluation.

## Development and documentation

Run database-independent checks from the repository root:

```powershell
uv sync --frozen
uv run --frozen pytest tests/unit tests/contract -q -p no:cacheprovider
uv run --frozen ruff check .
uv run --frozen mypy src
uv build
```

Use a [dedicated empty test database](docs/development.md#test-database) for PostgreSQL tests. A missing-DB skip is not a pass. Frontend changes require Node.js 22.14 or later: run `npm.cmd ci`, `npm.cmd test`, `npm.cmd run typecheck`, and `npm.cmd run build` inside `frontend`, keeping source and generated assets together.

- [Development guide](docs/development.md) / [Frontend README](frontend/README.md): manual startup, configuration, tests, and recovery.
- [ADR index](docs/adr/README.md) / [User-authored stories ADR](docs/adr/0017-user-authored-stories.md): adopted design decisions.
- [Playtest guide](docs/playtest-guide.md) / [Record template](docs/playtest-record-template.md): distinguish human testing from automated and agent checks.
- [September 29, 2026 verification record](docs/verification-20260929.md): historical scope, not verification of current changes.

Check [Issues](https://github.com/caprice1026-disc/AI_RPG/issues) before contributing. Follow [.AGENTS.md](.AGENTS.md), and describe the reason for changes, checks performed, and anything unverified in your pull request.
