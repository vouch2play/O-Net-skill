# O*NET MCP Server — hosted (Fly.io)

This is the hosted version of the local O*NET MCP server: same two tools
(`search_occupations`, `get_occupation_report`), served over HTTP with an
API-key gate, instead of running as a subprocess on one machine.

## Deploy (Fly.io)

Prereqs: a Fly.io account, `flyctl` installed and logged in (`fly auth login`).

```bash
cd hosted

# 1. Create the app (edit fly.toml's `app` name first — it must be globally unique)
fly launch --no-deploy --copy-config

# 2. Create a persistent volume for the database (1GB is plenty of headroom for 204MB)
fly volumes create onet_data --region ord --size 1

# 3. Upload onet.db onto the volume. Fly doesn't have a direct "scp to volume"
#    command, so the simplest path is a one-off machine with the volume attached:
fly machine run . --volume onet_data:/data --command "sleep 3600" --region ord
#    then, in another terminal, find that machine's ID with `fly machine list`
#    and copy the db onto it:
fly ssh sftp shell -a vetour-onet-mcp
#    (in the sftp shell) put /path/to/your/local/onet.db /data/onet.db
#    then stop the one-off machine: fly machine stop <machine-id>

# 4. Set your customers' API keys as a secret (comma-separated key:label pairs)
fly secrets set ONET_API_KEYS="k_live_abc123:AcmeCorp,k_live_def456:BetaLLC"

# 5. Deploy
fly deploy
```

Your endpoint will be `https://<app-name>.fly.dev/mcp`.

## Connecting a customer

Each customer gets one line for their MCP client config, pointing at your URL
with their API key as a Bearer token. In Claude Desktop's config:

```json
{
  "mcpServers": {
    "onet": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://vetour-onet-mcp.fly.dev/mcp",
        "--header", "Authorization: Bearer k_live_abc123"
      ]
    }
  }
}
```

(`mcp-remote` is a small npm proxy that bridges Claude Desktop's stdio-based
config to a remote HTTP MCP server with custom headers — needed because not
every Claude Desktop version supports headers on a remote connector directly.
If your customers connect through a Claude surface with native remote-MCP +
header support, they can skip `mcp-remote` and enter the URL and key there
directly.)

## Growing past v1

This ships with the minimum that makes "hosted and gated" true, not a full
billing system. Things intentionally left simple, and what to do when you
outgrow them:

- **API keys** live in one `ONET_API_KEYS` env var (comma-separated
  `key:label` pairs). Fine for a handful of early customers. Move to a real
  table (Postgres — Fly has a managed Postgres add-on) once you're issuing
  keys self-serve or need to revoke one without a redeploy. Only
  `_lookup_key()` in `onet_hosted_server.py` needs to change.
- **Usage tracking** is a flat SQLite log (`usage.db` on the volume) —
  one row per request, with customer label, path, and status. It's the raw
  material for metering, not metering itself. To bill on it: aggregate rows
  per customer per billing period and push counts to Stripe's usage-based
  billing (metered subscription items), or just eyeball it for a flat-fee
  beta.
- **Scaling to multiple machines/regions**: the Fly volume above is tied to
  one machine. `min_machines_running = 1` keeps a single machine up, which
  is fine for moderate load. If you need to scale out, either (a) create one
  volume + machine per region (Fly supports this, each gets its own copy of
  the db), or (b) stop mounting the db from a volume and instead load it
  from object storage (S3/Tigris) into local disk on container boot — more
  moving parts, but lets any machine in any region come up stateless.
- **Auth today is a static bearer token**, not real OAuth. That's normal for
  an API-key product and is what most paid MCP servers do. If you later want
  "Sign in with Google/Vetour account" style auth for end users (not just
  server-to-server keys), that's where `mcp`'s built-in `TokenVerifier` /
  `AuthSettings` OAuth support comes in — a bigger lift, worth doing once you
  have real signups to justify it.
- **HTTPS, health checks, and TLS** are already handled by Fly's proxy layer
  (`force_https = true` in fly.toml) — nothing to add there.

## Local test (before deploying)

```bash
pip install -r requirements.txt
ONET_DB_PATH=/path/to/references/onet.db \
ONET_API_KEYS="testkey:you" \
python3 onet_hosted_server.py
# then: curl http://localhost:8000/healthz
```
