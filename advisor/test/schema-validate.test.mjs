import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { validate } from "../schema-validate.mjs";

const root = dirname(fileURLToPath(import.meta.url));
const advisorRoot = join(root, "..");

async function loadJson(name) {
  return JSON.parse(await readFile(join(advisorRoot, name), "utf8"));
}

test("validator accepts a minimal well-formed object", () => {
  const schema = {
    type: "object",
    properties: { a: { type: "string" }, b: { type: "number" } },
    required: ["a"],
    additionalProperties: false,
  };
  const result = validate(schema, { a: "x", b: 1 });
  assert.equal(result.valid, true);
  assert.deepEqual(result.errors, []);
});

test("validator rejects a field outside the allowlist", () => {
  const schema = {
    type: "object",
    properties: { a: { type: "string" } },
    additionalProperties: false,
  };
  const result = validate(schema, { a: "x", secret: "leak" });
  assert.equal(result.valid, false);
  assert.match(result.errors.join("\n"), /secret/);
});

test("validator enforces enum membership", () => {
  const schema = { type: "string", enum: ["a", "b"] };
  assert.equal(validate(schema, "a").valid, true);
  assert.equal(validate(schema, "z").valid, false);
});

test("validator enforces const", () => {
  const schema = { const: "1.0.0" };
  assert.equal(validate(schema, "1.0.0").valid, true);
  assert.equal(validate(schema, "2.0.0").valid, false);
});

test("validator enforces array maxItems and nested item schema", () => {
  const schema = { type: "array", maxItems: 1, items: { type: "number" } };
  assert.equal(validate(schema, [1]).valid, true);
  assert.equal(validate(schema, [1, 2]).valid, false);
  assert.equal(validate(schema, ["x"]).valid, false);
});

test("the real audit-schema.json rejects an authority-shaped field", async () => {
  const schema = await loadJson("audit-schema.json");
  const attempt = {
    schema_version: "1.0.0",
    summary: "ok",
    observations: [],
    confidence: 0.5,
    status: "pass",
    trusted_for_projection: true,
  };
  const result = validate(schema, attempt);
  assert.equal(result.valid, false);
});

test("the real audit-schema.json rejects a forbidden severity", async () => {
  const schema = await loadJson("audit-schema.json");
  const attempt = {
    schema_version: "1.0.0",
    summary: "ok",
    confidence: 0.5,
    observations: [
      { category: "reconciliation", type: "observation", severity: "block", message: "x" },
    ],
  };
  const result = validate(schema, attempt);
  assert.equal(result.valid, false);
});

test("the real audit-schema.json accepts a well-formed advisory observation", async () => {
  const schema = await loadJson("audit-schema.json");
  const good = {
    schema_version: "1.0.0",
    summary: "Resumo consultivo.",
    confidence: 0.7,
    observations: [
      {
        category: "liquidity",
        type: "explanation",
        severity: "review",
        message: "Explica o déficit sem cobertura já calculado.",
        evidence_ref: ["finding-1"],
        recommendation: "Revisar manualmente.",
      },
    ],
  };
  const result = validate(schema, good);
  assert.equal(result.valid, true, result.errors.join("; "));
});

test("the real audit-input-schema.json rejects an unlisted field (e.g. a leaked secret)", async () => {
  const schema = await loadJson("audit-input-schema.json");
  const attempt = {
    schema_version: "1.0.0",
    audit_type: "period_review",
    integrity_snapshot: {
      status: "healthy",
      score: 100,
      trusted_for_projection: true,
      trusted_for_reports: true,
      open_findings: 0,
    },
    findings: [],
    database_url: "postgresql://leak",
  };
  const result = validate(schema, attempt);
  assert.equal(result.valid, false);
});

test("the real audit-input-schema.json accepts a well-formed sanitized package", async () => {
  const schema = await loadJson("audit-input-schema.json");
  const good = {
    schema_version: "1.0.0",
    audit_type: "period_review",
    period: "2026-08",
    trace_id: "11111111-1111-1111-1111-111111111111",
    integrity_snapshot: {
      status: "attention",
      score: 88.5,
      trusted_for_projection: true,
      trusted_for_reports: true,
      open_findings: 1,
      open_findings_by_severity: { warning: 1 },
    },
    findings: [
      {
        opaque_id: "finding-1",
        invariant_id: "INV-006",
        check_status: "warning",
        severity: "warning",
        scope: "period",
        period: "2026-08",
        message: "Déficit consumiu parte da liquidez disponível.",
      },
    ],
    category_breakdown: [{ opaque_id: "cat-1", category: "Mercado", amount: 1200.5 }],
    financial_metrics: { operating_result: -270014.03 },
  };
  const result = validate(schema, good);
  assert.equal(result.valid, true, result.errors.join("; "));
});
