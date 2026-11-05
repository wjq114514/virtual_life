// @ts-check
/**
 * Host half of the Virtual Life Studio bundle.
 *
 * Responsibilities, and nothing else:
 *  1. Register one read-only same-origin route under `config.route` (default
 *     `/virtual-life-studio`) with the Harness webserver.
 *  2. Serve `GET <route>/config` with the non-secret view defaults the Client
 *     panel needs (companion id, refresh cadence, whether a token is set).
 *  3. Proxy `GET <route>/api/<path>?<query>` to `${baseUrl}/v1/<path>?<query>`
 *     with the configured Bearer token attached, so the browser never holds
 *     the token and never issues a cross-origin request.
 *
 * The route is deliberately narrow: GET/HEAD only, same-origin only, and the
 * upstream status/body are passed through unchanged so the panel can show real
 * runtime errors instead of guessing.
 */

export const name = 'virtual-life-studio';

export const inject = ['webServer'];

/**
 * @typedef {Object} StudioConfig
 * @property {string} baseUrl
 * @property {string} token
 * @property {string} companionId
 * @property {number} refreshMs
 * @property {number} logLimit
 * @property {string} route
 */

/** @param {any} raw @returns {StudioConfig} */
function normalizeConfig(raw) {
  const source = raw || {};
  const route = String(source.route || '/virtual-life-studio');
  return {
    baseUrl: String(source.baseUrl || 'http://127.0.0.1:8038').replace(/\/+$/, ''),
    token: String(source.token || ''),
    companionId: String(source.companionId || ''),
    refreshMs: Math.max(1000, Number(source.refreshMs) || 5000),
    logLimit: Math.min(500, Math.max(50, Number(source.logLimit) || 300)),
    route: route.startsWith('/') ? route.replace(/\/+$/, '') : `/${route}`,
  };
}

/**
 * Same-origin guard. The Harness webserver has no server-wide origin policy, so
 * the route enforces its own: a browser request carrying an `Origin` header must
 * come from this very origin. Requests without `Origin` (curl, health checks)
 * are allowed through — they still cannot read anything without being on the
 * loopback interface the webserver already binds.
 *
 * @param {import('node:http').IncomingMessage} req
 * @param {string} origin
 */
function isSameOrigin(req, origin) {
  const value = req.headers.origin;
  if (!value) return true;
  try {
    return new URL(String(value)).origin === origin;
  } catch {
    return false;
  }
}

/**
 * @param {import('node:http').ServerResponse} res
 * @param {number} status
 * @param {unknown} body
 */
function sendJson(res, status, body) {
  const text = JSON.stringify(body);
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Length', String(Buffer.byteLength(text)));
  res.end(text);
}

/**
 * @param {import('node:http').IncomingMessage} req
 * @param {import('node:http').ServerResponse} res
 * @param {StudioConfig} cfg
 * @param {string} origin
 */
async function handle(req, res, cfg, origin) {
  const method = (req.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    res.setHeader('Allow', 'GET, HEAD');
    sendJson(res, 405, { detail: 'read-only route' });
    return;
  }
  if (!isSameOrigin(req, origin)) {
    sendJson(res, 403, { detail: 'cross-origin request refused' });
    return;
  }

  const url = new URL(req.url || '/', 'http://localhost');
  const rest = url.pathname.slice(cfg.route.length) || '/';

  if (rest === '/' || rest === '/config' || rest === '/config.json') {
    sendJson(res, 200, {
      companionId: cfg.companionId,
      refreshMs: cfg.refreshMs,
      logLimit: cfg.logLimit,
      baseUrl: cfg.baseUrl,
      hasToken: Boolean(cfg.token),
      origin,
    });
    return;
  }

  if (!rest.startsWith('/api/')) {
    sendJson(res, 404, { detail: `unknown route ${rest}` });
    return;
  }

  const suffix = rest.slice('/api'.length); // keeps the leading slash of the API path
  const upstream = `${cfg.baseUrl}/v1${suffix}${url.search}`;
  const headers = { Accept: 'application/json' };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;

  /** @type {Response} */
  let response;
  try {
    response = await fetch(upstream, { method: 'GET', headers });
  } catch (error) {
    sendJson(res, 502, {
      detail: `runtime unreachable at ${cfg.baseUrl}`,
      error: error instanceof Error ? error.message : String(error),
    });
    return;
  }

  const text = await response.text();
  res.statusCode = response.status;
  res.setHeader('Content-Type', response.headers.get('content-type') || 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Length', String(Buffer.byteLength(text)));
  res.end(method === 'HEAD' ? undefined : text);
}

/**
 * @param {any} ctx Harness plugin context.
 * @param {any} rawConfig Bundle config from the patch row.
 */
export function apply(ctx, rawConfig) {
  const cfg = normalizeConfig(rawConfig);
  const origin = ctx.webServer.getOrigin();
  const dispose = ctx.webServer.register({
    kind: 'prefix',
    path: cfg.route,
    handler: (req, res) => handle(req, res, cfg, origin),
  });
  return () => dispose();
}