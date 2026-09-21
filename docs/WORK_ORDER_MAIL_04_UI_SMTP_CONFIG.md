# Work Order — MAIL-04: SMTP configurável pela interface

Issue: #110

## Objetivo
Permitir configuração completa do remetente SMTP na tela de Configurações, sem edição manual de .env e sem restart para aplicar mudanças.

## Escopo
1. Persistência de metadados SMTP e App Password criptografada.
2. Chave criptográfica dedicada de ambiente para este segredo.
3. API admin-only de leitura sanitizada e mutação.
4. UI em Configurações.
5. Resolver uma configuração SMTP efetiva compartilhada pelo teste e notification-worker.
6. Compatibilidade controlada com ALERT_* legado.
7. Auditoria sem segredo.
8. Documentação/runbook e testes.

## Critérios de aceite
- Usuário admin configura e testa Gmail SMTP apenas pela UI.
- Segredo nunca retorna pela API/UI e nunca aparece em log/audit.
- Edição com senha vazia preserva o segredo; substituição e remoção são explícitas.
- Worker usa configuração nova sem restart.
- Deploy existente sem a nova configuração continua funcional.
- Fallback/precedência entre DB e env é explícito, testado e documentado.
- Migration é aditiva e possui round-trip.
- Testes de auth, crypto/redaction, API/UI, worker e test-email passam.
- CI e lint verdes.

## Invariantes
Não alterar regras financeiras nem MAIL-02 de elegibilidade/outbox. Não criar segundo motor de envio. Não reutilizar SECRET_KEY, FILE_ENCRYPTION_KEY ou MFA_ENCRYPTION_KEY. Oracle/OCI #59–65 permanece fora do escopo.

## Segurança
Somente admin altera SMTP. App Password criptografada em repouso. Chave de criptografia nunca no banco. Nenhum endpoint retorna ciphertext ou plaintext. Exceptions e auditoria usam apenas códigos sanitizados.

## Compatibilidade
ALERT_* continua disponível como fallback para instalações existentes. A implementação deve definir uma precedência determinística e evitar misturar campos de duas fontes de forma que produza credenciais híbridas.

## Testes obrigatórios
Migration SQLite/PostgreSQL; admin/non-admin; criptografia; redaction; preserve/replace/remove secret; fallback/precedência; live reload worker; test-email; regressão notification scheduler/delivery/status; ruff.

## Proibições
Sem Oracle/OCI. Sem dependência paga recorrente. Sem segredo plaintext. Sem SQL/LLM adicional. Sem mudança lateral de semântica financeira.