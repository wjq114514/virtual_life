# dsh-virtual-life-studio

DSH (DeepSeek Harness) sidebar **workspace** for the Virtual Life companion
runtime: companion state, live logs, memories, the distilled knowledge graph and
inner-life activity, in one panel next to the conversation.

The plugin has two halves.

- **Host half** (`index.js`) registers one read-only same-origin proxy route
  (`/virtual-life-studio`) on the Harness Web server. It adds the runtime Bearer
  token server-side, so the browser never receives it and no CORS is involved.
- **Client half** (`client.js`) registers a `sidebar.panellist` entry and the
  matching `main` panel — click the graph icon in the sidebar rail to open it.

## Panel views

| Tab | Source | Content |
| --- | --- | --- |
| 总览 | `/companions/{id}`, `/inner-life`, `/emotion`, `/memories/stats`, `/v1/graph` | companion card, mood metrics, inner needs bars, current inner activity, recent activity timeline |
| 日志 | `/flow/logs?after_sequence=` | live log tail (1.2 s), level chips, search, follow, expandable rows with the full formatted line |
| 记忆 | `/memories`, `/memories/stats` | memory cards with kind/layer/retention/importance/confidence, paging, statistics sidebar |
| 知识图谱 | `/memories/knowledge-graph` | deterministic force layout, 1/2/3 hops, entity search, click for aliases, relations and evidence memories |
| 内心活动 | `/inner-life`, `/inner-life/activities` | state chips, five-need radar, current activity with its `thinking`, 25-item timeline |

Every view follows the companion selector in the panel header, and the panel
honours the Host theme tokens (`--dsw-alias-*`) in both light and dark, plus the
active locale (zh / en) through the Client locale service.

## Config

```yaml
- insert:
    - id: virtual-life-studio
      name: '@virtuallife/dsh-virtual-life-studio'
      config:
        baseUrl: http://127.0.0.1:8038
        token: local-dev-token
        companionId: proact-demo
        refreshMs: 5000
        logLimit: 300
        route: /virtual-life-studio
```

`baseUrl` is the runtime the Host half proxies to, `token` is the runtime API
token (kept server-side only), `companionId` is the initially selected
companion, `refreshMs` is the panel poll interval (minimum 1000), `logLimit` is
the log batch size (clamped to 50–500) and `route` is the proxy path prefix.

## Host routes

| Route | Result |
| --- | --- |
| `GET <route>/` or `<route>/config` | `{companionId, refreshMs, logLimit, baseUrl, hasToken, origin}` — no secret values |
| `GET <route>/api/<path>` | proxied to `<baseUrl>/v1/<path>` with `Authorization: Bearer …`, upstream status and content type preserved |
| other path | `404 unknown route` |
| non-`GET`/`HEAD` | `405` |

The route is read-only and refuses any request whose `Origin` header is present
and differs from the Harness origin (`403`), because the Harness Web server has
no server-wide origin policy of its own. Requests without an `Origin` header
(non-browser clients) pass.

## Install

The package is a DSH bundle; install the directory, not a hand-written profile
entry:

```bash
# from the Harness side, with the workspace profile active
plugin_manager install_bundle target=/path/to/virtual_life/dsh/virtual-life-studio
```

Restarting is not required: the route and the panel registration are applied to
the live profile. Re-run the install after editing `client.js` (the client
bundle is composed from the package at load time).

## Requirements

Node 18+ and a Harness profile with a Web server. The runtime must be reachable
from the **Host** process (same machine or a routable address) and its token must
be the one given in `config.token`. The panel only reads; it never mutates
runtime state, so it needs no `command_id` handling.