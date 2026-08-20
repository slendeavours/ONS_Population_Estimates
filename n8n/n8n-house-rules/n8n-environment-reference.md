# Environment: self-hosted n8n stack

Verified facts about the running environment. If anything here looks out of date (version numbers especially), confirm with the user rather than assuming.

## The stack

- Docker Desktop on Windows, WSL2 backend.
- Compose project: `C:\Users\slewi\self-hosted-ai-starter-kit` (run compose commands from this directory).
- Services on the `demo` compose network: n8n (localhost:5678), Postgres 16-alpine (n8n's own DB `n8ndb`, exposed on host port 5432), Ollama (port 11434, `OLLAMA_HOST=ollama:11434` inside the network), Qdrant.
- n8n data lives in Docker volumes and bind mount `./n8n_data:/home/node/.n8n`. Postgres data in `postgres_storage`, Ollama models in `ollama_storage`.
- Business/pipeline data lives in a separate database (`exempt_pipeline`), never in `n8ndb`.
- Image versions are pinned deliberately so upgrades are a conscious decision. Don't switch anything back to `latest`.

## Environment variables that matter

- `N8N_RESTRICT_FILE_ACCESS_TO` is **semicolon-separated, not comma-separated**. Verified in n8n 2.14.2 source (`file-system-helper-functions.js`). Correct form: `/root;/tmp;/data/shared;/tmp/n8n_outputs`. Comma-separated values fail silently: file-write nodes error with access denied even though the path looks allowed. If a Write File node fails despite correct volume mounts and permissions, check this first.
- `N8N_COMMUNITY_PACKAGES_ENABLED=true` and `N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE=true` are set. `N8N_CUSTOM_EXTENSIONS=n8n-nodes-retellai` is installed but its trigger node is not usable (see integrations.md); Retell integration uses standard webhooks.
- `NODE_OPTIONS=--dns-result-order=ipv4first` is set to avoid IPv6 resolution issues between containers.
- `N8N_DEFAULT_HTTP_REQUEST_TIMEOUT=600000` (10 minutes) for long-running calls.

## PowerShell traps (host is Windows)

- `2>/dev/null` does not work in PowerShell; it's interpreted as a file redirect. Run Linux-style commands inside the container instead: `docker exec <container> sh -c "command 2>/dev/null"`.
- `grep` doesn't exist in PowerShell; use `findstr`, or again run `sh -c` inside the container.
- To list local models: `docker exec ollama ollama list`.

## Docker safety and recovery

Data lives in volumes. These are safe:

- `docker compose down` (containers and networks removed, volumes preserved)
- `docker compose pull` then `docker compose up -d`
- `docker container prune` (stopped containers only)
- `docker image prune -a` (unused images; correct cleanup after version pinning leaves old `latest` tags around)

These destroy data. Never run them without an explicit, confirmed instruction:

- `docker compose down -v`
- `docker system prune --volumes`
- `docker volume prune`

Two tags pointing at the same image ID (e.g. `latest` and a pinned version) share layers; deleting the `latest` tag with `docker rmi` frees nothing but is safe and reduces confusion.

## When Docker Desktop won't start (WSL2)

Escalate in this order:

1. From an admin PowerShell: `wsl --shutdown` (allow up to 60 seconds; it responds silently), then `wsl --update`, then restart Docker Desktop from the tray.
2. If WSL hangs: `Restart-Service LxssManager` in admin PowerShell.
3. If still stuck (backend processes lingering in Task Manager): full Windows restart, wait for Docker Desktop to fully load, then `docker compose --profile cpu up -d` from the starter-kit directory.
4. Last resort, safe for volumes: `wsl --unregister docker-desktop` and `wsl --unregister docker-desktop-data`, then restart Docker Desktop to reinstall the WSL distros.

Do not "fix" a stuck Docker by deleting volumes or reinstalling Docker Desktop with data removal.
