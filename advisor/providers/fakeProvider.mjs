// Deterministic test double for the Codex provider.
//
// Real network/CLI calls (`spawn(... codex.js ...)` in server.mjs) must
// never run inside unit tests -- this module exists so `runAudit()` can be
// exercised for every scenario in the Work Order's test matrix without a
// real provider: success, timeout, generic error, invalid schema, an
// attempt to change the deterministic verdict, an attempt to reference an
// unknown id, and an attempt to invent a financial number.
//
// This file is test-only. It is never imported by server.mjs and is not
// copied into the production Docker image (see advisor/Dockerfile).

/**
 * Build a fake provider function with the same signature as the real
 * `runCodex(prompt, schemaFile, { timeoutMs })` used in server.mjs.
 *
 * @param {string} scenario one of the FAKE_PROVIDER_SCENARIOS keys
 * @param {object} [options]
 * @param {object} [options.responseOverrides] shallow-merged into the
 *   canned successful response, for tests that need a specific message.
 */
export function fakeProvider(scenario, options = {}) {
  return async function fakeCodexCall(_prompt, _schemaFile, { timeoutMs } = {}) {
    switch (scenario) {
      case "success":
        return {
          schema_version: "1.0.0",
          summary: "Status attention: nenhuma inconsistência adicional além dos findings existentes.",
          observations: [
            {
              category: "liquidity",
              type: "explanation",
              severity: "info",
              message: "A liquidez consumida no período é consistente com o déficit reportado.",
              evidence_ref: options.evidenceRef || [],
            },
          ],
          confidence: 0.62,
          ...options.responseOverrides,
        };
      case "timeout":
        return new Promise((_resolve, reject) => {
          setTimeout(() => reject(new Error("O Codex excedeu o tempo máximo da análise")), timeoutMs || 10);
        });
      case "provider_error":
        throw new Error("Codex encerrou com código 1");
      case "invalid_schema_missing_field":
        return {
          schema_version: "1.0.0",
          // "summary" and "confidence" are missing on purpose.
          observations: [],
        };
      case "invalid_schema_extra_field":
        return {
          schema_version: "1.0.0",
          summary: "Resumo.",
          observations: [],
          confidence: 0.5,
          status: "pass",
        };
      case "attempt_change_verdict":
        // A model that "complied" with an injected instruction to approve
        // everything. `status`/`score`/`trusted_for_*` are not part of the
        // output schema at all, so this is rejected as additionalProperties.
        return {
          schema_version: "1.0.0",
          summary: "Tudo aprovado, pode confiar integralmente nos números.",
          observations: [],
          confidence: 1,
          status: "pass",
          score: 100,
          trusted_for_projection: true,
          trusted_for_reports: true,
        };
      case "attempt_forbidden_severity":
        return {
          schema_version: "1.0.0",
          summary: "Encontrei um problema grave.",
          observations: [
            {
              category: "reconciliation",
              type: "observation",
              severity: "block",
              message: "Bloqueando o relatório por conta própria.",
            },
          ],
          confidence: 0.9,
        };
      case "unknown_evidence_ref":
        return {
          schema_version: "1.0.0",
          summary: "Referência a um finding que não fazia parte do pacote enviado.",
          observations: [
            {
              category: "reconciliation",
              type: "observation",
              severity: "review",
              message: "Ver finding relacionado.",
              evidence_ref: ["finding-not-in-package"],
            },
          ],
          confidence: 0.5,
        };
      case "invented_number":
        return {
          schema_version: "1.0.0",
          summary: "Status attention: o saldo real é R$ 999999,99, bem diferente do reportado.",
          observations: [
            {
              category: "liquidity",
              type: "hypothesis",
              severity: "review",
              message: "Estimo que o saldo real seja 999999 e não o valor informado.",
            },
          ],
          confidence: 0.4,
        };
      case "malformed_json":
        // Simulates a provider that could not be parsed as JSON at all.
        throw new SyntaxError("O Codex não retornou JSON válido");
      default:
        throw new Error(`Cenário de fake provider desconhecido: ${scenario}`);
    }
  };
}

export const FAKE_PROVIDER_SCENARIOS = Object.freeze([
  "success",
  "timeout",
  "provider_error",
  "invalid_schema_missing_field",
  "invalid_schema_extra_field",
  "attempt_change_verdict",
  "attempt_forbidden_severity",
  "unknown_evidence_ref",
  "invented_number",
  "malformed_json",
]);
