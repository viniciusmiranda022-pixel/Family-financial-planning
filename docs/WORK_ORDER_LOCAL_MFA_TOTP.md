# Work Order — Fase 4: autenticação multifator local (TOTP)

## Estado e sequência

Este documento prepara antecipadamente o terceiro item da Fase 4 de `docs/ROADMAP.md`: **autenticação multifator local**.

A presença deste Work Order em `main` **não autoriza antecipar a implementação**. O desenvolvimento deste slice só pode começar quando o item imediatamente anterior da Fase 4 — **perfis separados para administrador e consulta** — estiver revisado, com os gates exigidos verdes e mesclado em `main` pelo engenheiro responsável.

No início da implementação, Claude deve reler a `main` atual e confrontar este Work Order com o código efetivamente integrado. Se o merge do slice anterior alterar contratos de autenticação, papéis, sessão ou autorização, este documento deve ser adaptado à realidade encontrada sem enfraquecer os objetivos de segurança abaixo.

**Claude é o executor da implementação e não pode fazer merge.**

Branch prevista quando o slice for iniciado: `feat/local-mfa-totp`.

Draft PR previsto: `Fase 4 — autenticação multifator local`.

## Objetivo

Adicionar um segundo fator local baseado em **TOTP (Time-based One-Time Password)**, compatível com Google Authenticator, Microsoft Authenticator e outros aplicativos que implementem RFC 6238, sem depender de login Google, OAuth Google, API Google, SMS, e-mail OTP ou serviço externo pago.

O resultado deve transformar o fluxo atual de senha + cookie de sessão em um fluxo no qual **nenhum usuário ativo acessa dados financeiros somente com senha**. O segundo fator protege confidencialidade e operação; portanto a exigência vale tanto para administradores quanto para perfis de consulta. Um perfil somente leitura ainda enxerga dados financeiros sensíveis e não deve ser tratado como exceção de MFA.

A implementação deve permanecer local ao Family Finance: segredo TOTP armazenado de forma protegida no banco, QR Code produzido dentro da aplicação e validação feita no backend.

## Fonte de verdade

Ler antes de implementar e confrontar este Work Order com:

- `docs/ROADMAP.md`;
- `docs/SECURITY.md`;
- `docs/ARCHITECTURE.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `README.md`;
- `compose.yaml`;
- `.env.example`;
- `.github/workflows/ci.yml`;
- `app/security.py`;
- `app/config.py`;
- `app/models.py`;
- `app/api.py`;
- migrations Alembic atuais;
- testes de autenticação, household isolation e autorização existentes;
- o Work Order e o diff efetivamente mesclado do slice de perfis administrador/consulta.

Se houver conflito entre este Work Order e um contrato normativo mais novo de `main`, Claude deve abrir Technical Challenge com evidência concreta. Não substituir arquitetura por preferência de ferramenta.

## Arquitetura atual que deve ser preservada

Na `main` existente quando este documento foi escrito:

- senha é armazenada com `scrypt` e salt aleatório;
- a aplicação usa FastAPI + SQLAlchemy + Alembic;
- sessão autenticada usa cookie `ffp_session`, assinado por `itsdangerous`;
- o cookie de sessão é `HttpOnly`, `SameSite=Strict` e respeita `COOKIE_SECURE`;
- `User` possui `household_id`, `username`, `is_admin` e `active`;
- `get_current_user()` é a fronteira central que carrega a identidade autenticada;
- `cryptography` já é dependência do projeto;
- documentos usam uma chave própria de criptografia (`FILE_ENCRYPTION_KEY`).

O MFA deve **estender** essa arquitetura, não criar um provedor paralelo de identidade.

## Decisões normativas de segurança

1. **TOTP padrão RFC 6238.** Usar 6 dígitos, período de 30 segundos, SHA-1 para interoperabilidade e segredo aleatório de pelo menos 160 bits. Aceitar no máximo uma janela pequena de relógio (`±1` timestep), documentada e testada.
2. **Sem integração Google.** Google Authenticator é apenas um gerador TOTP. Não criar OAuth, Client ID, Client Secret, redirect URI ou chamada a API Google.
3. **MFA obrigatório para todo usuário ativo.** Administrador e consulta devem concluir o segundo fator antes de receber sessão completa.
4. **Nenhuma sessão completa depois da senha.** Senha correta produz somente um estado temporário e restrito para `enroll` ou `verify`; `ffp_session` completo só é criado após o segundo fator.
5. **Backend é a autoridade.** Esconder tela ou botão no frontend é somente UX. Todas as APIs protegidas continuam dependendo do backend.
6. **Segredo TOTP criptografado em repouso.** Não armazenar em plaintext e não armazenar apenas hash, porque o servidor precisa recuperar o segredo para validar TOTP.
7. **Separação de chaves.** Criar `MFA_ENCRYPTION_KEY` própria. Não reutilizar diretamente `SECRET_KEY` nem `FILE_ENCRYPTION_KEY` para criptografar o segredo MFA.
8. **Recovery codes são one-way.** Códigos de recuperação ficam somente como verifier/hash seguro, nunca recuperáveis em plaintext depois da exibição inicial.
9. **Anti-replay.** Um timestep TOTP já aceito para um usuário não pode autenticar de novo.
10. **Rate limit fail-closed para o segundo fator.** Tentativas inválidas repetidas devem produzir bloqueio temporário, sem bloqueio permanente provocado por atacante.
11. **Mudança de MFA revoga sessões.** Reconfiguração, reset de emergência ou qualquer troca do fator deve invalidar sessões autenticadas anteriores de maneira server-side verificável.
12. **Nenhum segredo em log, audit event, exception ou PR.** TOTP, segredo, URI `otpauth`, recovery code e `MFA_ENCRYPTION_KEY` são material sensível.
13. **QR Code local.** Nunca enviar a URI `otpauth://` ou o segredo para serviço público de QR Code.
14. **Sem mudança de regra financeira.** MFA não altera transações, categorias, snapshots, fechamento, reconciliação, projeções, compromissos ou invariantes.

## Modelo de autenticação desejado

O estado de autenticação deve ser explícito:

```text
ANONYMOUS
   |
   | username + senha válida
   v
PASSWORD_VERIFIED
   |
   +-- usuário sem TOTP confirmado --> MFA_ENROLL_PENDING
   |
   +-- usuário com TOTP confirmado --> MFA_VERIFY_PENDING
                                          |
                                          | TOTP ou recovery code válido
                                          v
                                   FULLY_AUTHENTICATED
                                          |
                                          v
                                      ffp_session
```

`MFA_ENROLL_PENDING` e `MFA_VERIFY_PENDING` não podem usar as APIs financeiras normais.

Preferência arquitetural: manter `ffp_session` reservado exclusivamente à sessão completa e usar um cookie temporário separado, por exemplo `ffp_mfa_pending`, `HttpOnly`, `SameSite=Strict`, com `Secure` seguindo a política de HTTPS e TTL curto. O token temporário deve usar salt/purpose distinto do token de sessão e carregar somente identidade e finalidade necessárias.

TTL inicial recomendado para o estado pendente: **5 minutos**. Se a arquitetura integrada em `main` oferecer mecanismo equivalente melhor, Claude pode reutilizá-lo, preservando a separação entre senha validada e sessão completa.

## Proteção contra sessão antiga e revogação

A sessão atual é stateless e assinada. Para que reset/reconfiguração do MFA possa invalidar cookies anteriormente emitidos, o slice deve introduzir um mecanismo de versão/epoch de sessão validado no servidor.

Modelo preferencial:

- `User.session_version` inteiro, aditivo, com valor inicial seguro;
- o cookie completo inclui a versão emitida;
- `get_current_user()` exige que a versão do cookie corresponda à versão atual do usuário;
- cookies antigos sem versão devem falhar fechados após o rollout, forçando novo login;
- reconfiguração/reset do MFA incrementa a versão e invalida todas as sessões anteriores.

Se `main` já tiver mecanismo de revogação equivalente no momento da implementação, reutilizá-lo em vez de criar um segundo mecanismo.

## Modelo de dados esperado

Claude deve primeiro confrontar o schema atual e escolher a menor modelagem coerente. A direção preferencial é separar material MFA do modelo financeiro.

Estrutura lógica mínima equivalente:

### Fator TOTP por usuário

- `user_id` único;
- `secret_encrypted`;
- `confirmed_at` nullable;
- `setup_started_at`;
- `setup_expires_at`;
- `last_accepted_timestep` nullable;
- `failed_attempts`;
- `locked_until` nullable;
- timestamps normais do projeto.

### Recovery codes

- `user_id`/`factor_id`;
- `code_hash`/verifier;
- `created_at`;
- `used_at` nullable.

Requisitos:

- um fator TOTP ativo por usuário neste slice;
- cada usuário tem seu próprio segredo;
- nenhuma chave TOTP é compartilhada por household;
- migration aditiva e reversível;
- nenhuma alteração destrutiva em `users` ou tabelas financeiras;
- nenhum backfill inventa um TOTP para usuário existente.

## Criptografia do segredo TOTP

Usar primitiva autenticada fornecida por biblioteca madura já compatível com o projeto. Como `cryptography` já existe, preferir reutilizar sua infraestrutura em vez de implementar criptografia manual.

`MFA_ENCRYPTION_KEY` deve ser uma chave própria, gerada aleatoriamente e fornecida por variável de ambiente. O valor real nunca entra em Git. `.env.example` deve conter apenas placeholder/documentação segura. CI deve usar chave falsa fixa exclusivamente de teste, nunca chave de produção.

A implementação deve falhar explicitamente na inicialização/uso do MFA se a chave necessária estiver ausente ou inválida; não cair para plaintext.

Rotação de `MFA_ENCRYPTION_KEY` não precisa ser implementada neste slice, mas o formato persistido não deve impedir evolução futura. Se for criada versão de chave, documentar sem aumentar escopo além do necessário.

## Enrollment inicial

Depois da senha correta, usuário sem fator confirmado deve entrar somente no fluxo de enrollment.

Fluxo obrigatório:

1. gerar segredo aleatório criptograficamente seguro;
2. criptografar o segredo antes de persistir;
3. registrar setup pendente com expiração;
4. gerar URI `otpauth://` localmente;
5. gerar QR Code dentro da aplicação;
6. exibir QR Code e chave manual somente nessa etapa;
7. solicitar código TOTP de 6 dígitos;
8. validar no backend;
9. somente após código válido marcar o fator como confirmado;
10. gerar recovery codes;
11. persistir apenas os verifiers/hashes dos recovery codes;
12. exibir os recovery codes uma única vez;
13. criar/rotacionar a sessão completa somente após a confirmação do segundo fator.

URI conceitual:

```text
otpauth://totp/Family%20Finance:<username>?secret=<BASE32>&issuer=Family%20Finance&algorithm=SHA1&digits=6&period=30
```

O valor real jamais deve aparecer em logs, audit trail ou mensagens de erro.

Setup expirado não ativa MFA e deve exigir novo segredo. Não reutilizar indefinidamente segredo de enrollment abandonado.

## Login de usuário já inscrito

Fluxo obrigatório:

1. validar username/senha pelo mecanismo existente;
2. não criar `ffp_session` ainda;
3. emitir apenas estado/cookie MFA pendente com TTL curto;
4. solicitar TOTP;
5. validar rate limit;
6. validar TOTP e timestep;
7. rejeitar timestep já aceito;
8. registrar a aceitação de forma atômica/concorrente segura;
9. limpar o estado MFA pendente;
10. emitir nova sessão completa com `session_version` atual;
11. registrar audit event sem segredo/código.

Um usuário com cookie pendente não pode acessar diretamente `/api/reports`, dashboard, lançamentos, importações, Advisor, Integridade ou qualquer endpoint que dependa de autenticação completa.

## Anti-replay

Armazenar `last_accepted_timestep` ou mecanismo equivalente.

A aceitação deve ser atômica: duas requisições concorrentes com o mesmo timestep não podem ambas concluir login. Implementar com transação/UPDATE condicional/lock adequado que funcione tanto nos testes SQLite quanto no PostgreSQL real, sem confiar apenas em comparação feita em memória.

## Rate limiting

Implementar proteção persistente ou server-authoritative compatível com a arquitetura atual.

Piso inicial recomendado:

- até 5 tentativas TOTP inválidas em uma janela curta;
- bloqueio temporário de aproximadamente 5 minutos;
- reset/decadência após autenticação válida;
- resposta não deve revelar o código esperado nem detalhes do segredo;
- registrar evento de excesso de tentativa sem incluir TOTP.

Claude pode ajustar números se houver política global já existente em `main`, mas deve documentar a decisão e manter proteção equivalente ou superior.

## Recovery codes

Após enrollment confirmado gerar **10 códigos de recuperação** criptograficamente aleatórios, com entropia suficiente e formato legível.

Requisitos:

- exibir em plaintext apenas no momento da geração;
- armazenar apenas hash/verifier seguro;
- cada código é single-use;
- comparação resistente a timing quando aplicável;
- uso bem-sucedido marca o código como consumido na mesma transação da autenticação;
- código usado nunca pode autenticar novamente;
- regenerar conjunto invalida todos os códigos anteriores;
- regeneração exige sessão completa + reautenticação forte;
- número de códigos restantes pode ser exibido, mas nunca o conteúdo antigo.

## Reconfiguração do autenticador

Como MFA é obrigatório para todos os usuários ativos, não oferecer um simples botão persistente de **Desativar MFA** que deixe a conta novamente password-only.

Fornecer **Reconfigurar autenticador**:

1. exigir sessão completa;
2. exigir senha atual;
3. exigir TOTP atual ou recovery code válido;
4. gerar novo segredo pendente;
5. exibir novo QR/chave manual;
6. exigir código válido do novo fator;
7. somente então substituir o fator anterior;
8. invalidar todos os recovery codes antigos e gerar novo conjunto;
9. incrementar `session_version`/revogar sessões;
10. criar uma nova sessão completa para a operação atual somente depois do fluxo concluído.

Nunca invalidar o fator antigo antes da confirmação do novo, para não criar lockout acidental.

## Break-glass por acesso local ao servidor

Se o usuário perder autenticador **e** recovery codes, o sistema precisa de recuperação operacional sem criar um endpoint web poderoso de reset.

Preferência: comando local de operador, por exemplo conceitualmente:

```text
python -m app.cli.mfa reset --username <usuario>
```

O comando real deve seguir o padrão CLI existente e:

- rodar somente com acesso direto ao host/banco;
- nunca ser exposto como rota HTTP;
- exigir confirmação explícita do operador;
- não mostrar segredo anterior;
- remover/inutilizar o fator e recovery codes;
- incrementar `session_version` para revogar sessões;
- registrar audit event de reset administrativo/local;
- deixar o usuário em estado de enrollment obrigatório no próximo login.

Não criar senha mestra, código universal, bypass escondido ou recovery code hardcoded.

## Auditoria

Reutilizar `AuditEvent` e o padrão já existente; não criar um segundo sistema de auditoria sem necessidade.

Eventos mínimos equivalentes:

- `mfa.enrollment_started`;
- `mfa.enrolled`;
- `mfa.challenge_failed`;
- `mfa.rate_limited`;
- `mfa.authenticated`;
- `mfa.recovery_code_used`;
- `mfa.recovery_codes_regenerated`;
- `mfa.reconfigured`;
- `mfa.reset_local`.

Metadados permitidos: `user_id`, `household_id`, timestamp, resultado, origem/rota e dados operacionais não sensíveis compatíveis com o logger atual.

Nunca registrar:

- senha;
- código TOTP;
- segredo TOTP;
- URI `otpauth`;
- recovery code;
- `MFA_ENCRYPTION_KEY`;
- QR Code em base64/bytes.

## Frontend

Preservar a UI/identidade visual atual e não introduzir framework frontend novo.

Fluxos mínimos:

### Após senha — enrollment obrigatório

- título claro de autenticação em duas etapas;
- QR Code;
- chave manual como fallback de leitura;
- instrução para Google Authenticator ou qualquer app TOTP compatível;
- campo de 6 dígitos com `autocomplete="one-time-code"` quando aplicável;
- confirmação;
- tela única de recovery codes com ação de copiar/imprimir se tecnicamente segura.

### Após senha — challenge normal

- campo de 6 dígitos;
- opção explícita para usar recovery code;
- mensagens genéricas para falha;
- nenhuma tela financeira renderizada antes de autenticação completa.

### Segurança da conta

Adicionar seção equivalente a:

```text
Configurações
└── Segurança
    ├── Autenticação em duas etapas: ativa
    ├── Reconfigurar autenticador
    ├── Gerar novos códigos de recuperação
    └── Códigos de recuperação restantes: N
```

Não mostrar segredo TOTP já confirmado.

## Dependências

Não implementar TOTP/QR criptográfico manualmente se houver biblioteca Python madura, pequena e mantida que satisfaça o contrato. Claude deve justificar qualquer dependência nova no PR e fixar faixa compatível no `pyproject.toml`.

É aceitável adicionar uma biblioteca TOTP e uma biblioteca QR local se necessário. Não adicionar SDK Google.

Antes de introduzir nova dependência, verificar se a stack atual já entrega a capacidade de forma segura e legível.

## Migration e compatibilidade

A migration deve ser aditiva e validada nos dois caminhos existentes de CI:

- banco vazio até `head`;
- upgrade da baseline suportada até `head`;
- downgrade da revisão deste slice quando o gate atual exigir.

Requisitos de rollout:

- usuários existentes permanecem existentes e ativos;
- nenhuma senha é rehashada/desconhecida pela migration;
- nenhuma transação/documento/fato financeiro é modificado;
- usuário existente sem MFA entra em enrollment obrigatório depois da senha;
- sessões emitidas pela versão anterior não podem continuar como sessão completa silenciosamente após o enforcement novo;
- rollback da aplicação deve ser documentado antes de aplicar em base real.

## Testes obrigatórios

Criar testes automatizados específicos, além de manter todos os gates existentes.

### Enrollment

- usuário existente + senha correta recebe estado de enrollment, nunca sessão completa;
- QR/URI usa issuer/account corretos sem chamada externa;
- segredo persistido não é plaintext;
- confirmação com TOTP válido ativa o fator;
- TOTP inválido não ativa;
- setup expirado falha;
- recovery codes são retornados somente na geração e persistidos sem plaintext.

### Login

- senha incorreta continua falhando;
- senha correta + MFA confirmado não cria sessão completa antes do TOTP;
- pending cookie/token não autentica APIs normais;
- TOTP válido cria sessão completa;
- TOTP inválido não cria sessão;
- usuário consulta também exige MFA;
- usuário admin também exige MFA;
- usuário inativo continua recusado independentemente de MFA.

### Sessão

- cookie completo inclui/valida versão de sessão ou mecanismo equivalente;
- cookie legado sem prova/versão exigida é recusado após rollout;
- reconfiguração/reset invalida sessão anterior;
- pending cookie tem TTL curto e purpose restrito;
- logout limpa sessão completa e pending state.

### Anti-replay e concorrência

- mesmo timestep não autentica duas vezes;
- duas validações concorrentes do mesmo código resultam em no máximo uma aceitação;
- timestep fora da janela aceita é rejeitado;
- pequena diferença de relógio prevista pela política funciona.

### Rate limit

- tentativas inválidas acumulam;
- limite produz bloqueio temporário;
- bloqueio não vira permanente;
- sucesso posterior zera/normaliza o estado conforme desenho;
- logs/audit não incluem TOTP.

### Recovery

- recovery válido autentica;
- inválido falha;
- usado uma vez não funciona novamente;
- regeneração invalida todos os anteriores;
- recovery code não aparece em log/audit.

### Reconfiguração

- exige senha + segundo fator atual;
- segredo antigo permanece válido enquanto novo não foi confirmado;
- após confirmação, segredo antigo deixa de funcionar;
- sessões antigas são revogadas;
- recovery codes antigos são invalidados.

### Break-glass

- reset local exige confirmação;
- reset não expõe segredo;
- reset revoga sessões;
- próximo login exige novo enrollment;
- reset de usuário inexistente/inativo falha explicitamente sem alterar outro usuário.

### Household/roles/regressão

- MFA não altera `household_id` nem relaxa isolamento;
- autenticação de um usuário não concede contexto de outro household;
- autorização admin/consulta integrada pelo slice anterior permanece intacta depois do MFA;
- nenhuma rota mutável ganha bypass por estar no fluxo MFA;
- todos os invariantes e testes financeiros permanecem verdes.

### Compatibilidade real

Além da suíte automatizada, o engenheiro responsável deve executar ao menos um teste manual real de enrollment/login com **Google Authenticator** antes do merge/deploy final. O PR deve registrar essa evidência como manual; CI não deve depender de telefone, conta Google ou rede externa.

## Testes de segurança obrigatórios

Tentar explicitamente e comprovar recusa de:

1. acessar API financeira após apenas senha;
2. usar pending cookie/token como `ffp_session` completo;
3. alterar flag/estado no frontend para pular MFA;
4. reutilizar TOTP já aceito;
5. reutilizar recovery code;
6. continuar usando cookie completo emitido antes de reset/reconfiguração;
7. obter segredo pela rota de status/configuração após enrollment;
8. fazer logs capturarem `secret`, `otp`, `otpauth`, recovery code ou chave de criptografia;
9. gerar QR por domínio/serviço externo;
10. perfil de consulta acessar sem MFA por não poder escrever.

## Escopo explicitamente excluído

Não implementar neste slice:

- OAuth/Login with Google;
- SMS OTP;
- WhatsApp OTP;
- e-mail OTP;
- push MFA;
- passkeys/WebAuthn;
- biometria;
- device trust;
- SSO;
- IdP externo;
- Tailscale como substituto de MFA da aplicação;
- política de investimento;
- alteração do motor financeiro;
- backup externo;
- observabilidade ampla da Fase 4;
- mudança geral da rotina de atualização/rollback;
- exposição pública da aplicação;
- Tailscale Funnel.

## Relação com HTTPS/Tailscale

MFA é uma segunda camada e não substitui HTTPS/Tailscale.

A topologia permanece:

```text
Dispositivo autorizado
        |
        | Tailscale / HTTPS
        v
Family Finance
        |
        | senha
        v
MFA TOTP / recovery
        |
        v
sessão completa
        |
        v
APIs financeiras autorizadas
```

Nunca usar a existência da tailnet como justificativa para entregar sessão completa sem MFA depois que este slice estiver ativo.

## Procedimento de implementação para Claude

Claude deve trabalhar nesta ordem:

1. reler `main` após o merge do slice anterior;
2. produzir discovery curto dos contratos atuais de auth/session/roles/migrations/testes;
3. abrir Technical Challenge antes de qualquer divergência material;
4. criar migration e modelos aditivos;
5. implementar serviço TOTP/crypto/recovery com testes unitários;
6. implementar pending auth state separado da sessão completa;
7. integrar enrollment e login no backend;
8. implementar anti-replay, rate limit e session revocation;
9. implementar recovery codes;
10. implementar reconfiguração e CLI break-glass;
11. implementar UI mínima completa;
12. adicionar auditoria sanitizada;
13. executar testes focados;
14. executar toda a suíte/gates existentes;
15. atualizar documentação operacional necessária;
16. apresentar evidências e parar antes do merge.

Commits podem ser separados internamente por responsabilidade, mas este item da Fase 4 deve permanecer **um único vertical slice revisável**, sem antecipar backup externo/observabilidade.

## Evidência obrigatória no Draft PR

Claude deve registrar no PR:

```text
FASE:
STATUS:
BASE SHA:
HEAD SHA:
ARQUIVOS ALTERADOS:
MIGRATION:
MODELO DE AUTENTICAÇÃO:
CRIPTOGRAFIA DO TOTP:
SESSION REVOCATION:
ANTI-REPLAY:
RATE LIMIT:
RECOVERY CODES:
BREAK-GLASS:
AUDIT EVENTS:
TESTES FOCADOS:
SUÍTE COMPLETA:
GATES CI:
TESTE MANUAL GOOGLE AUTHENTICATOR:
RISCOS/PENDÊNCIAS:
ROLLBACK:
```

Não aceitar apenas `feito`, `implementado` ou screenshot do QR como Definition of Done.

## Critérios de aceite

O slice só pode ser considerado concluído quando:

- todo usuário ativo precisa de segundo fator para sessão completa;
- senha isolada nunca libera as APIs protegidas;
- enrollment funciona com Google Authenticator;
- TOTP é validado no backend;
- segredo TOTP está criptografado com chave separada;
- recovery codes estão somente em forma one-way no banco;
- recovery codes são single-use;
- anti-replay TOTP funciona inclusive sob concorrência;
- rate limiting temporário funciona;
- pending auth não é sessão;
- sessões antigas são revogáveis e reset/reconfiguração invalida cookies prévios;
- reconfiguração não cria janela password-only;
- break-glass exige acesso local e não cria bypass web;
- QR é gerado localmente;
- nenhum segredo aparece em logs/audit/PR;
- admin e consulta continuam respeitando autorização do slice anterior;
- household isolation permanece fail-closed;
- migrations e rollback foram testados conforme gates atuais;
- todos os testes financeiros e os gates exigidos estão verdes no mesmo head final;
- teste manual real com Google Authenticator foi registrado;
- Claude parou antes do merge.

## Rollback

Antes do deploy real, documentar o rollback da aplicação e da migration.

Princípios:

- migration aditiva para permitir retorno de versão sem perda de dados financeiros;
- nunca apagar fatores/recovery codes automaticamente em rollback;
- rollback não pode tocar transações, documentos ou snapshots;
- se a versão antiga não entende `session_version`, o procedimento deve considerar invalidação controlada de sessões e retorno ao comportamento anterior sem expor segredo;
- qualquer downgrade destrutivo de tabelas MFA só pode ocorrer em ambiente descartável de teste, não como rotina automática sobre base real;
- backup atual deve existir antes da migration em base contendo dados reais.

## Relação de revisão

Claude é executor. O engenheiro responsável revisará arquitetura, autenticação, autorização, household isolation, criptografia, migration, anti-replay, rate limiting, session revocation, recovery, logs, privacidade, frontend, testes e CI.

Claude pode e deve contestar tecnicamente este Work Order com evidência concreta. Divergência não resolvida bloqueia merge.

**Não fazer merge.**
