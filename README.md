# Forge Agent 0.5.0

Android coding workspace with universal provider APIs, an on-device runner, GitHub fetching, and an AI skill builder.

## Install from your phone

1. In `adeebkjan11-ctrl/Forgeagent`, replace `.github/workflows/build-apk.yml` with the complete supplied **Build-Forge-APK-fixed.yml**. Commit it. The push starts the APK build; you can also run **Build Forge APK** manually in Actions.
2. Once the APK workflow succeeds, download **Forge-Agent-APK** from that run's Artifacts section, extract the ZIP, and install `app-debug.apk`.
3. Open the app. The **on-device runner** starts automatically — your phone is the computer. No RDP, cloud machine, or extra setup needed.
4. Open the app and tap the model dropdown in the chat box to set up your provider: pick a preset, paste your API key, discover models, and save.

The source ZIP contains the application and the workflow generator; it is not an APK. The supplied YAML file includes the source it needs and can build from an otherwise empty repository.

If Android says the update has an incompatible signature, the older and newer CI builds used different temporary debug signing certificates. Save your settings and project work before uninstalling the older app. Uninstalling removes its encrypted keys. A signing keystore retained in repository secrets is required for consistent signing across future builds; this update does not add or rotate signing credentials.

## What changed

### 0.5.0 — clean chat UI, Railway runner
- The interface is now just **Chat, Files, Cloud**. No provider screens: the chat box has a model dropdown and a file-attach button, nothing else.
- **Separate chats**: a ☰ drawer lists your conversations, each with its own history. Switch freely; runs finish in the background and land in the right chat.
- Model setup is a one-time dialog (preset, URL, key, discover models, test, save). Your old provider settings migrate automatically.
- **Railway cloud runner**: deploy the included `Dockerfile` on Railway, set `FORGE_TOKEN`, add a volume at `/data`, then connect from Cloud with your Railway URL. The runner reads `$PORT` automatically.
- **GitHub built in**: connect a personal access token from the Cloud page and the agent can read your repos and open pull requests (`github_read_file`, `github_create_pr` tools, plus an "Open PR" skill).
- **Built-in skills**: Code Review, Write Tests, Explain Code, Fix Bug, and Open PR ship with the runner — pick them with the ✦ button in the chat box.
- **Key backup**: every provider key you save in the app is backed up to `api.json` on the runner (your Railway volume), so a reinstall restores it automatically.
- The Providers, Skills, Connectors, and legacy Windows RDP cloud pages were removed for clarity; the runner APIs remain.
- **Attachments**: up to 5 MB per file, multiple files at once, any type. Small text files are pasted into your message; larger or binary files (zip, pdf, images…) are uploaded to `uploads/` in the workspace so the agent can read them with its file tools.
- The Providers, Skills, and Connectors pages were removed from the UI for clarity; the runner APIs remain.

### 0.4.0

- Custom API prefixes and versions, such as `/api`, `/gateway/v2`, `/openai/v1`, and `/v1beta`, are preserved instead of having `/v1` added again. Bare server origins still default to `/v1`. Full inference URLs are accepted and their final route is stripped when choosing other endpoints.
- Surrounding whitespace and a pasted `Bearer ` prefix are removed from API keys. Invalid characters get a local, readable error.
- Discovery and API chat share credential/header handling. Codex CLI now honors Bearer, `x-api-key`, `api-key`, and unauthenticated modes instead of silently using Bearer for every custom gateway. Claude Code uses the selected supported credential mode.
- Model discovery is explicitly labeled **chat untested**. It no longer presents a public model list as proof that inference authentication works.
- **Test chat access** sends one short inference request to the selected model, with a tool schema matching the selected API format. It never executes tools. This can consume a small amount of provider credit. When a runner is connected, the test runs there; otherwise Android tests from the phone. A phone test does not establish runner connectivity, and an API test does not validate the separately installed CLI.
- HTTP 401, 403, 404/405, 429, redirects, HTML pages, empty responses, invalid JSON, UTF-8 BOMs, and invalid tool argument JSON receive appropriate handling. Provider error bodies and credentials are not included in diagnostics.
- Runner pairing failures identify the runner token, separately from provider API-key errors. Polling stops on an expired pairing or missing run instead of retrying forever.
- Custom provider presets default to Chat Completions. Claude and OpenAI presets default to their direct APIs. Existing saved profiles retain their chosen API/CLI mode; check older custom profiles that used the old Messages default.
- The GUI exposes authentication beside the URL and key, previews the chat endpoint, adds key visibility and model search, preserves inline errors, and allows removing saved providers. Model filtering does not silently change the selected model.
- The chat area scrolls inside a bounded layout while the composer and navigation remain visible. Smaller viewports get compact controls. Follow-up tasks keep earlier messages, and New restores the welcome screen.
- The APK source is restored from a script literal instead of one very large environment variable.

## Choose the correct API format

Use the **API base URL from your provider's documentation**, which may differ from its website.

| Provider API | App format | Typical chat route | Authentication |
| --- | --- | --- | --- |
| OpenAI-compatible gateway | OpenAI Chat Completions | `/v1/chat/completions` | Usually Authorization: Bearer |
| Claude-compatible gateway | Claude-compatible Messages | `/v1/messages` | Usually x-api-key; some gateways use Bearer |
| OpenAI Responses service | OpenAI Responses API | `/v1/responses` | Usually Authorization: Bearer |
| Google Gemini | Google Gemini API | `/v1beta/models/{model}:generateContent` | x-goog-api-key |
| Azure OpenAI | Azure OpenAI | `/openai/deployments/{deployment}/chat/completions` | api-key |
| Local Ollama | Ollama API | `/api/chat` | Usually no authentication |
| DeepSeek / Groq / Together / xAI / OpenRouter / Mistral / Cohere | OpenAI Chat Completions (preset) | provider default | Usually Authorization: Bearer |
| Custom gateway with its own header | Any API format + Custom header | provider default | Your header name |
| Installed Claude Code | Claude Code CLI | Managed by the CLI | Bearer or x-api-key |
| Installed Codex | Codex CLI | Responses API via the CLI | Selected header is preserved |

For Azure, the base URL is your resource origin (`https://YOUR-RESOURCE.openai.azure.com`) and the selected model is the deployment name. For Gemini, model IDs look like `gemini-2.0-flash`. The **Custom header** authentication mode sends your key under any header name a gateway requires.

AgentRouter and TabiToken presets remain editable conveniences. Their model-list endpoint does not certify access to Claude Code, every listed model, or tool calling. For CLI-only services, use the CLI mode documented by the provider; the direct API test may not be supported.

Claude Code appends `/v1/messages` itself. For a nonstandard path that does not end in `/v1`, select the direct Claude-compatible API agent. For Codex, custom gateways must support the Responses API. This version does not add Azure-specific query parameters, arbitrary custom headers, OAuth, or browser-challenge handling.

On Android, API keys are encrypted with Android Keystore. Browser keys remain in memory and need to be re-entered after a reload. A missing key is shown in the provider status. A blank key supplied by the app is not silently replaced with a different account's environment key.

**A genuine HTTP 401 still requires a valid key and the correct authentication mode.** The fix prevents several client-side causes and makes the failure identifiable; it cannot grant access denied by your provider. Model listing can be public even when chat requires paid access or model-specific permissions. GitHub tokens, runner pairing tokens, and provider API keys are separate credentials.

## On-device, cloud, and local runners

The app now runs its coding tools **on the phone itself**. On launch it starts an embedded Python runner (Chaquopy) on `127.0.0.1:8787` with a private workspace and pairs automatically — Python snippets (`run_python`), shell commands, file tools, and GitHub fetching all execute on-device. No RDP session, cloud machine, or computer is required. The runner lives while the app is open; stop it anytime from the Cloud tab's on-device card.

## Railway cloud runner

Railway is the built-in cloud option. Deploy once from the app's Cloud page (**Deploy on Railway**), or manually:

1. Push this source to a repo and create a Railway service from it. The included `Dockerfile` (+ `railway.toml`) starts the runner; Railway's `$PORT` is picked up automatically.
2. Add a variable `FORGE_TOKEN` with a secret value of at least 24 characters. This is your pairing token — the provider API keys you enter in the app are separate.
3. Add a Railway volume mounted at `/data` so your workspace and saved keys survive restarts.
4. In the app, open **Cloud → Connect** and enter your Railway URL (`https://…`) plus the `FORGE_TOKEN`. The app reconnects automatically on launch. Traffic is HTTPS.

Your Railway service is reachable at its public `https://….up.railway.app` URL: the app's API uses it, and you can also open it in a browser (it serves this same interface — pair with your token). The workspace lives on your volume; `api.json` at the workspace root holds the provider keys the app backs up there.

Optional variables: `GITHUB_TOKEN` keeps GitHub connected across restarts (otherwise connect from the app's Cloud page each time the runner starts).

## One shared backend for every install (silent mode)

Ship the app to other people and have every install silently use **your** Railway as its backend — no setup for them, no Railway access for them:

1. In this source, put your Railway URL in `web/shared.js` (`window.FORGE_SHARED_URL = 'https://your-app.up.railway.app';`).
2. Deploy on Railway with `FORGE_SHARED=1` (plus `FORGE_TOKEN` and a volume at `/data` as above).
3. Build the APK and distribute it.

How it works: on first launch each phone generates a random device id and calls `POST /api/provision` — the backend mints a per-device token and gives that device its own isolated workspace folder (`/data/devices/<id>/`). The app then connects with that token automatically. Users never see Railway, URLs, or tokens; the Cloud page's runner cards are hidden.

Provider API keys are stored **only on the backend**: the app sends the key once at model setup (saved to the device's `api.json`), then wipes every local copy. Every run sends the profile without a key and the runner injects it server-side.

Guardrails (set as Railway variables): `FORGE_MAX_DEVICES` (default 500) caps how many devices can provision; `FORGE_MAX_RUNS` (default 4) caps concurrent agent runs across all devices. Set a Railway spend alert — you pay the compute for every user.

The runner is stdlib-only Python — no pip packages needed. Local equivalent:

```sh
FORGE_TOKEN=<24+ chars> python -m runner.server --workspace /absolute/path/to/project --host 0.0.0.0
```

## Cloud and local runners (advanced)

For a local runner, use Python 3.11+:

```sh
python -m runner.server --workspace /absolute/path/to/project
```

Pair using the printed token. `FORGE_TOKEN` can retain a pairing token across restarts. Use HTTPS with `--cert` and `--key`, or trusted LAN/Tailscale HTTP. The Android app requires explicit trusted-network opt-in for an HTTP runner.

API agents request approval for edits, commands, and connector calls. CLI agents request approval for an entire CLI run, then use the installed CLI's permissions. CLI mode requires `FORGE_ENABLE_CLI=1` and an installed CLI. The fix preserves these approval boundaries.

## Verification

Completed locally for this release:

- 68 Python tests, including local HTTP discovery → chat probes → actual provider turns across six protocols and six authentication modes, Gemini tool-call round trips, Azure deployments, `run_python`, GitHub archive fetching (including zip-slip protection), and the AI skill builder; public models with chat 401; malformed/HTML/empty responses; CLI credential mapping; existing tool, approval, and encrypted-pairing tests.
- 10 DOM interface tests against the actual JavaScript: discovery versus chat status, key cleanup and persistence, model selection/filtering, stale discovery results, readable error messages, browser key loss after reload, and follow-up chat history.
- JavaScript syntax checks and Python compilation checks.

Android unit tests, APK compilation, and Android lint are configured as mandatory steps in the APK workflow. The source includes five Android unit tests for native URL, header, and response handling. Local Android build and actual-device visual checks are not claimed here. Live calls to your provider accounts require your own keys; no such credentials were included in the attachments.

Run the checks:

```sh
python -m pip install 'cryptography>=43,<47'
python -m unittest discover -s tests -v
npm ci --ignore-scripts
npm run test:ui
gradle -p android --no-daemon testDebugUnitTest assembleDebug lintDebug
python packaging/build_bundles.py --output /absolute/path/outside-this-source-tree
```

Build requirements: Java 17, Gradle 8.9, Android Gradle Plugin 8.7.3, Android SDK 35; minimum Android API 26. The build downloads Chaquopy's embedded Python from `https://chaquo.com/maven` (network required); if the pinned Chaquopy plugin version fails to resolve, update it to the latest at chaquo.com. Chaquopy is free for open-source apps and needs a license for proprietary distribution — check their terms for your use. The WebView loads bundled assets and routes network requests through its native bridge.

## References

- [Codex provider authentication configuration](https://developers.openai.com/codex/config-reference/)
- [Claude Code gateways](https://code.claude.com/docs/en/llm-gateway)
- [Ollama model listing](https://docs.ollama.com/api/tags)
