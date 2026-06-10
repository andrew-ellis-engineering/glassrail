# Spec: Security baseline

Status: Proposed
Priority: P0/P1 split — items 1, 2, and 5 land before the 0.1.0 tag; items 3
and 4 land in the release window, before broad marketing
(`docs/release/grassroots-marketing.md` positions the project as "auditable
tool use", so the gap between claim and posture must close fast).
Depends on: nothing. Items are independently mergeable.

## Purpose

Current posture, as audited June 2026:

- `file_read` (`src/glassrail/harness/builtin.py`) reads **any** path the
  process can read — an LLM-planned `file_read("/etc/passwd")` succeeds.
- `web_fetch` (`src/glassrail/harness/integrations/web.py`) fetches arbitrary
  URLs with redirects on and no size cap — SSRF against localhost/cloud
  metadata endpoints is possible.
- `image_generate` (`src/glassrail/harness/integrations/image.py`) writes to
  any user-resolvable `output_path`.
- The REST gateway has **no authentication** and no CORS config.
- The tool `risk` field (`read`/`network`/`write`/`execute`) is **decorative**:
  `Executor._approve_tool_call` passes it through for display but never
  consults it; the registry docstring claims write/execute tools "require
  explicit user approval", which is currently false (acknowledged in the
  roadmap's file-editing entry).

The Phase 2 file-editing spec (vault) depends on items 1 and 2 anyway; this
pulls the substrate forward.

## Item 1 — `fs_roots` path confinement (P0)

- **Setting:** `fs_roots: list[Path] | None = None` on the tools settings
  model in `src/glassrail/config/settings.py` (the parent model that holds
  `tools.web` / `tools.image` — locate it; expose as `[tools] fs_roots = [...]`,
  env `GLASSRAIL_TOOLS__FS_ROOTS`).
- **Guard:** new module `src/glassrail/harness/pathguard.py`:

  ```python
  def ensure_within_roots(path: str, roots: Sequence[Path] | None) -> Path
  ```

  Behaviour: `expanduser()` then `resolve()` (symlinks resolved). If `roots`
  is `None`/empty: log a **one-time** warning ("file tools are unconfined; set
  tools.fs_roots to restrict them") and return the resolved path — current
  behaviour preserved, so existing flows and the eval fixtures under
  `/tmp/glassrail-eval/` keep working. Otherwise the resolved path must
  satisfy `resolved.is_relative_to(root.expanduser().resolve())` for at least
  one root, else raise `ToolExecutionError("path '<p>' is outside the
  configured tools.fs_roots")`.
- **Wire into:** `file_read` (builtin), `image_generate`'s `output_path`
  resolution, and every future file tool (the Phase 2 file-editing spec
  builds on this helper).
- **Tests:** traversal (`../../etc/passwd` from inside a root → denied),
  symlink escape (symlink inside a tmp root targeting outside → denied
  because resolution happens before the check), allowed path inside a root,
  unconfined-mode warning emitted exactly once.
- **Docs:** README Tools section gains the `fs_roots` row and a sentence on
  the default; note that a future minor release will flip the default to
  confined.

## Item 2 — Honor `risk` in tool approval (P0)

**File:** `Executor._approve_tool_call` in `src/glassrail/executor/executor.py`.

Effective-policy resolution becomes:

1. An explicit per-tool override in `settings.tool_approval.overrides` wins.
2. Otherwise, if `harness.risk_for(tool)` is `"write"` or `"execute"` →
   effective policy is `ASK`.
3. Otherwise → `settings.tool_approval.default`.

Everything downstream is unchanged: `mode = "auto"` still treats `ASK` as
allow (headless runs keep working — this is deliberate; auto mode is the
operator saying "I accept unattended execution"), explicit `DENY` always
denies, interactive mode with a broker prompts, interactive mode without a
broker logs and denies.

- **Tests:** write-risk tool, no override, interactive mode with broker →
  approval requested; same in auto mode → runs; explicit `allow` override on a
  write-risk tool → runs without asking; read-risk tool unaffected.
- **Docs:** README Tool Approval section documents the risk-derived default;
  fix the `ToolRisk` docstring in `src/glassrail/harness/registry.py` so the
  claim matches reality (it now does).

## Item 3 — `web_fetch` hardening (release window)

**File:** `src/glassrail/harness/integrations/web.py`.

- **Scheme allowlist:** only `http`/`https`; anything else →
  `ToolExecutionError`.
- **Private-address rejection:** resolve the hostname
  (`socket.getaddrinfo`, run via `asyncio.to_thread`); if **any** resolved
  address has `ipaddress.ip_address(addr)` with `is_private`, `is_loopback`,
  `is_link_local`, `is_reserved`, `is_multicast`, or `is_unspecified` →
  reject, unless the new setting `tools.web.allow_private_hosts: bool = False`
  is true (needed for self-hosted SearXNG users fetching internal pages —
  note: `web_search`'s configured `searxng_url` is operator-supplied config,
  not model-controlled input, so it is exempt from this check).
  Document the residual DNS-rebinding race (resolve-then-connect) as a known
  v1 limitation in the module docstring.
- **Size cap:** `tools.web.max_fetch_bytes: int = 5_000_000`; switch the GET
  to `client.stream(...)` and accumulate `aiter_bytes()` chunks, aborting with
  `ToolExecutionError` when the cap is exceeded (do not trust
  `Content-Length`).
- **Redirects:** set `max_redirects=5` explicitly.
- **Tests** (extend `tests/unit/test_web_tools.py`, `httpx.MockTransport`):
  `ftp://` rejected; `http://127.0.0.1/x` and `http://169.254.169.254/meta`
  rejected; allowed when `allow_private_hosts=true`; body larger than a small
  test cap rejected mid-stream.
- **Docs:** README web-tools block gains the two new keys.

## Item 4 — REST bearer auth (release window)

- **Setting:** `api_key: str | None = None` on `Settings`
  (`GLASSRAIL_API_KEY`). `None` (default) = no auth, current behaviour.
- **HTTP:** when set, an `@app.middleware("http")` in
  `src/glassrail/gateways/rest/app.py` requires
  `Authorization: Bearer <key>` (compare with `secrets.compare_digest`) on
  every route **except `/health`**; failure → 401 JSON body.
- **WebSocket:** middleware does not cover WS — check the header (or an
  `api_key` query parameter as a browser-client fallback) in the WS endpoint
  *before* `accept()`; reject with close code 1008.
- **Tests** (extend `tests/integration/test_rest_gateway.py` /
  `test_rest_ws_events.py`): with `api_key` set — 401 without header, 200 with
  correct bearer, `/health` open, WS closes 1008 without key; with `api_key`
  unset — everything open (regression).
- **Docs:** README Security notes + `docs/deployment.md` env example gain
  `GLASSRAIL_API_KEY`.

## Item 5 — Keep the README security notes truthful (P0, ongoing)

The README gained a "Security notes" section in the June 2026 audit change
stating: gateway is unauthenticated (bind to localhost), `file_read` is
unconfined until `fs_roots` is set, web tools fetch model-chosen URLs, and
`risk` is informational. **Each item in this spec, when it lands, must update
that section in the same PR** so it never overstates or understates the
posture. When items 1–4 are all done, the section shrinks to: how to set
`fs_roots`, `api_key`, and approval policies.

## Non-goals

- Sandboxing/subprocess isolation of tools (Phase 4).
- CORS policy (no browser client exists yet; add alongside any web UI).
- Multi-user authz/rate limiting.
- Outbound egress proxying.

## Acceptance criteria

- Full check sweep green per item.
- Item 1: the three pathguard tests; eval suites still green (default
  unconfined).
- Item 2: harness-mechanics tool tasks still green (their tools are read/
  network risk); the four approval tests.
- Item 3: SSRF/size tests green; a normal `web_fetch` against MockTransport
  unchanged.
- Item 4: auth test matrix green; `curl` without a key gets 401 when
  `GLASSRAIL_API_KEY` is set.
- README/deployment docs updated in the same PRs; CHANGELOG entries per item.
