# Security Policy

## Threat model

Docket is a **local-first** tool. By default it binds **loopback only**
(`HOST=127.0.0.1`, and under Docker the port is published only on `127.0.0.1`), and
it has **no authentication or TLS** — by design, because nothing off-machine can
connect. Running it exposed (`HOST=0.0.0.0`) is opt-in and is your responsibility to
protect (reverse proxy, auth, TLS); that configuration is outside the default
security posture.

The primary trust boundary is **LLM-driven tool arguments**. Docket's tools are
called by an AI agent, and the content it indexes can contain prompt injection. A
poisoned document could try to steer the agent into calling `index(url=...)` with a
hostile target. The hardening below is aimed squarely at that boundary.

## Hardening in place

- **SSRF guard** — URL fetches and git-clone hosts are resolved and refused if they
  point at internal addresses (private / loopback / link-local / reserved), with
  IPv4-mapped-IPv6 unwrapping and a per-redirect re-check. Toggle with
  `ALLOW_INTERNAL_IPS` (default `false`).
- **git clone hardening** — `--` argument terminator, leading-`-` rejection,
  `GIT_ALLOW_PROTOCOL=http:https:git:ssh` (blocks `ext::`/`file::` command
  execution), `GIT_TERMINAL_PROMPT=0`, a hard timeout, and temp-dir cleanup.
- **Resource limits** — per-response download cap (`MAX_RESPONSE_BYTES`, catches
  decompression bombs), total-content cap per source, redirect cap, and page cap on
  manifest crawls.
- **Container** — runs as a **non-root** user; minimal base image.
- **Data layer** — all SQL is parameterized; YAML is parsed with `safe_load`.
- **Local file ingest** can be restricted with `LOCAL_ROOT_ALLOWLIST` (see below).

## Known limitations / residual risks

- **DNS rebinding (TOCTOU).** The SSRF guard validates at DNS-resolution time; a
  determined attacker controlling a short-TTL record could return a public address
  during the check and an internal one at connect time. A full fix requires pinning
  the vetted IP for the connection. Accepted residual for a local, owner-driven
  tool; revisit before any networked deployment.
- **Local file reads are unrestricted by default.** `LOCAL_ROOT_ALLOWLIST` is empty
  by default, so `index()` can read any local path the process can access. This is
  intentional for a tool you drive yourself; set `LOCAL_ROOT_ALLOWLIST` to confine
  local ingestion if that matters in your setup.
- **No authentication/TLS** in the default local-only posture (see Threat model).

## Supported versions

The latest release and `main` receive security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security reports. Instead, use GitHub's
**private vulnerability reporting** (repository *Security* tab → *Report a
vulnerability*), or email **directedbits@gmail.com**. Include reproduction steps and
the affected version/commit. We aim to acknowledge within a few days.
