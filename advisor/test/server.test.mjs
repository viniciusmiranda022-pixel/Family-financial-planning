// End-to-end smoke test of the actual HTTP server for the new routes,
// using the real fake-Codex-CLI-free path: since ADVISOR credentials are
// not present in CI, /v1/audit and /v1/classify legitimately answer 503
// ("Codex ainda não foi autenticado"), same as /v1/analyze already does.
// This test only proves auth/routing wiring -- the full behavioral matrix
// (success/timeout/invalid schema/injection/etc.) is covered directly
// against `runAudit()` in audit.test.mjs, which is the actual unit under
// test and does not require spawning the server or the Codex CLI.
import assert from "node:assert/strict";
import { once } from "node:events";
import { after, before, test } from "node:test";

process.env.ADVISOR_SHARED_SECRET = "test-shared-secret";
process.env.ADVISOR_PORT = "0";
process.env.CODEX_HOME = "/tmp/ffp-advisor-test-codex-home";
// The production default (`/sandbox`, root-owned in the container image) is
// not writable by an unprivileged CI runner user; point the test at a
// directory it can actually create, exactly like ADVISOR_SANDBOX_DIR is
// meant to be overridden for the native-Windows deployment mode.
process.env.ADVISOR_SANDBOX_DIR = "/tmp/ffp-advisor-test-sandbox";

let server;
let baseUrl;

before(async () => {
  ({ default: server } = await import("../server.mjs?test=server"));
  if (!server.listening) await once(server, "listening");
  const address = server.address();
  baseUrl = `http://127.0.0.1:${address.port}`;
});

after(() => {
  server.close();
});

test("GET /health does not require the shared secret", async () => {
  const response = await fetch(`${baseUrl}/health`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(typeof body.ready, "boolean");
});

test("POST /v1/audit without the shared secret is rejected with 401", async () => {
  const response = await fetch(`${baseUrl}/v1/audit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  assert.equal(response.status, 401);
});

test("GET /v1/audit/metrics with the shared secret returns numeric counters", async () => {
  const response = await fetch(`${baseUrl}/v1/audit/metrics`, {
    headers: { "X-Advisor-Token": "test-shared-secret" },
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(typeof body.calls_total, "number");
  assert.equal(typeof body.success_total, "number");
  assert.equal(typeof body.timeout_total, "number");
  assert.equal(typeof body.invalid_schema_total, "number");
  assert.equal(typeof body.latency_ms_sum, "number");
});

test("POST /v1/audit with the shared secret but no Codex auth answers 503, never a fabricated pass", async () => {
  const response = await fetch(`${baseUrl}/v1/audit`, {
    method: "POST",
    headers: { "X-Advisor-Token": "test-shared-secret", "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  // No auth.json in this sandbox -> the same fail-safe 503 every other
  // authenticated route already returns; the financial engine's caller
  // (app/services/codex_audit.py) treats this identically to any other
  // transport failure -- "provider_unreachable", never a verdict.
  assert.equal(response.status, 503);
});
