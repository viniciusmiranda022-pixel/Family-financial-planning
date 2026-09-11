# Work Order mínimo — ativação do slice Fase 4: MFA local TOTP

## Estado

Este documento **ativa** o terceiro item da Fase 4 de `docs/ROADMAP.md` após o merge do slice anterior (perfis administrador/consulta) em `main` no commit `d0a6b33e1bb24755f25d00665fbbc43adee1d046`.

A especificação normativa completa permanece em `docs/WORK_ORDER_LOCAL_MFA_TOTP.md`; este arquivo não a substitui nem reduz. Claude deve reler a `main` atual e confrontar implementação, segurança, schema, sessão e autorização com aquele documento antes de alterar código.

**Claude é o executor e não pode fazer merge.** Pode e deve contestar este Work Order ou a especificação principal quando houver evidência concreta em documentação, código, testes, segurança ou menor risco de regressão. Divergência não resolvida bloqueia merge.

## Objetivo

Implementar MFA local obrigatório por TOTP RFC 6238 para **todo usuário ativo**, administrador ou consulta, sem OAuth/Google API/SMS/e-mail OTP e sem criar provedor paralelo de identidade. Senha correta nunca pode produzir sessão financeira completa antes do segundo fator.

## Escopo obrigatório

- estender a autenticação atual com estado pendente separado e de TTL curto; `ffp_session` fica reservado à sessão completa;
- TOTP de 6 dígitos, 30 s, SHA-1, segredo aleatório >= 160 bits e janela máxima documentada de ±1 timestep;
- segredo TOTP criptografado em repouso com `MFA_ENCRYPTION_KEY` exclusiva, nunca `SECRET_KEY`/`FILE_ENCRYPTION_KEY`;
- recovery codes single-use persistidos somente como verifiers/hashes, plaintext exibido apenas na geração;
- anti-replay atômico do timestep aceito e rate limit server-authoritative/fail-closed;
- versionamento/epoch server-side de sessão para revogar cookies antigos após reset/reconfiguração;
- enrollment local com URI `otpauth://` e QR Code gerado localmente, sem serviço externo;
- reconfiguração forte sem desativar MFA permanentemente: senha atual + segundo fator atual, fator antigo preservado até confirmação do novo, revogação de sessões e recovery codes antigos após troca;
- break-glass somente por CLI local, sem endpoint web poderoso de reset;
- migration apenas aditiva e reversível, sem tocar tabelas/fatos financeiros e sem inventar TOTP para usuários existentes;
- manter integralmente household isolation e a fronteira admin/consulta mesclada no PR #56.

## Critérios de aceite

1. Usuário existente sem fator confirmado, após senha correta, entra em enrollment obrigatório e não recebe `ffp_session` completa.
2. Usuário já inscrito, após senha correta, recebe somente estado MFA pendente; TOTP/recovery válido é necessário para sessão completa.
3. Pending token/cookie não autentica nenhuma API financeira nem pode ser usado como sessão completa.
4. TOTP/recovery não aparecem em logs, audit events, exceptions ou respostas indevidas; segredo confirmado nunca volta a ser exposto.
5. Mesmo timestep TOTP não autentica duas vezes, inclusive em concorrência; recovery code usado não reutiliza.
6. Rate limit temporário funciona sem lockout permanente; sucesso normaliza estado conforme desenho documentado.
7. Reset/reconfiguração revoga sessões anteriores de forma server-side verificável; cookie legado sem versão exigida falha fechado após rollout.
8. Admin e consulta exigem MFA igualmente; role/household não podem ser forjados via frontend/request.
9. QR/URI são produzidos localmente; nenhuma rede externa é necessária para CI.
10. Banco vazio, upgrade da baseline suportada e downgrade da nova migration passam nos gates atuais.
11. Nenhuma regra financeira, snapshot, reconciliação, projeção ou invariant muda.
12. Os 12 gates do GitHub Actions permanecem verdes no **mesmo head final**.
13. Antes de merge/deploy final, o engenheiro responsável deve registrar teste manual real de enrollment/login com Google Authenticator; CI não depende de telefone/Google/rede externa.

## Invariantes e contratos que não podem regredir

- `docs/FINANCIAL_RULES.md` e `docs/FINANCIAL_INVARIANTS.md` permanecem semanticamente inalterados;
- Codex/Claude não decide autenticação, role, sessão, TOTP, recovery, score ou fato determinístico;
- `get_current_user()` continua sendo a fronteira de identidade **completa** e deve rejeitar sessão ausente, legada/inválida ou revogada;
- `_require_admin` continua protegendo todas as mutações administrativas após autenticação completa;
- MFA nunca concede acesso cruzado entre households nem transforma consulta em admin.

## Testes obrigatórios

Cobrir pelo menos os grupos definidos em `docs/WORK_ORDER_LOCAL_MFA_TOTP.md`: enrollment, login, pending/session version, logout, anti-replay e concorrência, janela de relógio, rate limit, recovery codes, regeneração, reconfiguração, break-glass CLI, users inativos, admin/consulta, household isolation, redaction de segredos, QR local, migration real PostgreSQL/Alembic e regressão financeira completa.

Além disso, executar `ruff check .`, suíte Python completa, testes do Advisor aplicáveis, `node --check app/static/app.js` e os 12 jobs nomeados do CI no head final.

## Riscos principais

- emitir sessão completa cedo demais;
- armazenar segredo/recovery em plaintext ou vazar em logs/auditoria;
- replay por corrida entre requisições;
- bypass de MFA por cookie legado/pending/role de consulta;
- lockout permanente por rate limit/reconfiguração mal ordenada;
- migration que invalide usuários/senhas existentes;
- reconfiguração que invalide o fator antigo antes de confirmar o novo;
- reutilizar chaves criptográficas de outras finalidades.

Esses riscos devem falhar fechados e ser cobertos por teste.

## Proibições

Não implementar OAuth/Login with Google, SMS/WhatsApp/e-mail OTP, passkeys/WebAuthn, SSO/IdP externo, device trust, Tailscale como substituto de MFA, exposição pública, backup externo, observabilidade ampla ou demais itens posteriores da Fase 4. Não alterar regra financeira para fazer teste passar, não reduzir cobertura, não usar auto-fix destrutivo, não alterar dados reais automaticamente, não criar migration destrutiva, não duplicar motor financeiro/autorização e não incluir nenhum segredo real no repositório/PR.
