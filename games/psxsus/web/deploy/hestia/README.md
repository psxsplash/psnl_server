# Putting the spectator behind HestiaCP

Two templates, `psxsus.tpl` (HTTP) and `psxsus.stpl` (HTTPS). They make nginx a
reverse proxy in front of the spectator and do nothing else — no PHP, no
document root to speak of.

## Install

```sh
# On the Hestia box, as root:
cp psxsus.tpl psxsus.stpl /usr/local/hestia/data/templates/web/nginx/php-fpm/
chmod 644 /usr/local/hestia/data/templates/web/nginx/php-fpm/psxsus.*
```

Then, for the domain: **WEB → *domain* → Advanced Options → Web Template →
`psxsus`**. Leave *Proxy Support* off — this template is the proxy. Apply, and
Hestia rewrites `/home/<user>/conf/web/<domain>/nginx.conf`.

From the shell instead:

```sh
v-change-web-domain-tpl <user> <domain> psxsus
v-add-letsencrypt-domain <user> <domain>     # if it has no certificate yet
```

Copy **both** files even if you only serve HTTP. Hestia looks for the `.stpl`
sibling the moment SSL is enabled and refuses the template if it is missing.

## Point it at the right backend

The upstream is hardcoded as `127.0.0.1:8089` — Hestia templates take no custom
variables. If the spectator listens elsewhere, edit `proxy_pass` in **both**
files and rebuild:

```sh
v-rebuild-web-domains <user>
```

Keep the spectator bound to loopback so nobody can skip the proxy. In
`docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:8089:8089"
```

## Two settings that are not optional

**1. Tell the spectator to trust the proxy.**

The fair-play banner works by comparing the browser's address against the
addresses consoles are connected from. Behind a proxy every request arrives from
the proxy, so without this the comparison can never match and the warning
silently stops appearing — a safeguard that fails quietly is worse than one that
was never there.

```yaml
environment:
  PSXSUS_WEB_TRUST_PROXY: 127.0.0.1
```

It is deliberately not automatic. `X-Forwarded-For` is a request header like any
other, so believing it from anyone would let a spectator decide for themselves
whether to be warned. Naming the proxy means the header is only honoured when it
came through our own nginx, which overwrites whatever the browser sent.

**2. Do not let Hestia's global caching template near it.**

The live feeds are Server-Sent Events. A cache or a buffer in front of them
holds the response until it has enough to be worth forwarding, and the page
shows nothing for several seconds and then jumps — indistinguishable from a
frozen server. The templates set `proxy_buffering off`, `gzip off` and
`proxy_cache off` on `/api/stream/`, and the application also sends
`X-Accel-Buffering: no`; do not add a `caching` proxy template on top.

## Checking it

```sh
curl -sI  https://<domain>/healthz                      # 200, application/json
curl -sN  https://<domain>/api/stream/rooms | head -c 200   # frames within ~1s
curl -s   https://<domain>/api/fairplay
```

If `/api/stream/rooms` pauses and then dumps everything at once, something is
still buffering — check for a caching template or an extra proxy in front of
nginx (Cloudflare buffers SSE on some plans).

If `/api/fairplay` reports `"warn": false` while somebody on your network is
demonstrably in a room, `PSXSUS_WEB_TRUST_PROXY` is unset or does not name the
proxy's address.

## What you are exposing

Everything. Roles, positions, vents, who voted for whom. That is the feature,
but it means the reverse proxy is not a security boundary — it is a convenience.
Only public rooms are ever listed, join codes are never sent, and a room made
private is invisible here; beyond that, anyone with the URL sees the impostors.

If that is not what you want on a public hostname, put Hestia's own HTTP basic
auth in front of it (**WEB → *domain* → Advanced Options → Password protection**,
or `v-add-web-domain-httpauth <user> <domain> <login> <password>`). It composes
with these templates without changes.
