# Work Order — Testes com cópias anonimizadas de documentos reais

## Executor

Claude é o executor da implementação deste slice. O engenheiro responsável revisará o resultado antes de qualquer merge.

Claude **não pode fazer merge**, alterar regras financeiras não documentadas, corrigir dados reais automaticamente, reduzir cobertura, remover salvaguardas, mudar invariantes para acomodar fixtures, nem dar a Claude/Codex/Advisor autoridade sobre fatos determinísticos. Claude **pode e deve contestar** este Work Order ou orientações do engenheiro quando houver evidência concreta de contradição com a documentação normativa, comportamento verificável, testes, segurança, integridade financeira ou menor risco de regressão. Divergência não resolvida bloqueia merge.

## Posição no roadmap

Próximo item não concluído em `docs/ROADMAP.md`, imediatamente após exportação Excel/PDF: **testes com cópias anonimizadas dos documentos reais**.

Não avançar neste PR para o go-live manual, novos fluxos de lançamento, novos tipos financeiros, notificações, UI adicional ou qualquer item de Fase 3/4.

## Objetivo

Adicionar uma suíte de regressão baseada em **cópias anonimizadas e sanitizadas** de formatos/documentos reais já suportados pelo sistema, para validar o pipeline existente de parsing, classificação, reconciliação, deduplicação e publicação sem introduzir uma segunda política de cálculo ou expor PII/dados financeiros reais.

Os testes devem provar que os parsers e contratos atuais se comportam corretamente diante de estruturas representativas do mundo real, preservando os invariantes financeiros e os mesmos serviços canônicos usados em produção.

## Fonte de verdade

Antes de implementar, revisar no mínimo:

- `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`;
- `docs/FINANCIAL_RULES.md`;
- `docs/FINANCIAL_INVARIANTS.md`;
- `docs/ROADMAP.md`;
- `docs/ARCHITECTURE.md`;
- parsers/importadores atuais e contratos `ParsedDocument`/reconciliação;
- testes existentes de importer, reconciliation, duplicates, snapshots e API;
- política de criptografia/armazenamento de documentos e qualquer documentação de privacidade aplicável.

Em caso de conflito, a documentação normativa e os invariantes têm precedência sobre conveniência do fixture ou comportamento legado não documentado.

## Escopo obrigatório

1. Criar fixtures anonimizadas/sintetizadas a partir da **estrutura** de documentos reais suportados, sem preservar PII ou números reais desnecessários.
2. Cobrir os principais tipos já suportados pelo repositório, na medida em que existam parsers reais hoje, por exemplo:
   - extrato bancário;
   - fatura de cartão;
   - CSV/OFX suportados;
   - holerite/demonstrativo de pagamento;
   - variantes textuais/PDF já aceitas pelo pipeline atual.
3. Exercitar o pipeline canônico existente, preferencialmente pelos mesmos entrypoints usados pela API/importação, sem chamar helpers privados de forma que burle validações relevantes.
4. Validar, conforme aplicável ao documento:
   - parsing determinístico;
   - competência e datas preservadas;
   - sinais de classificação;
   - reconciliação `reconciled`/`unknown`/`review_required` honesta conforme os componentes disponíveis;
   - tolerância monetária de R$ 0,01 onde definida;
   - duplicidade exata/provável sem remoção destrutiva;
   - precedência canônica sem apagar evidência;
   - household isolation;
   - nenhuma exposição de conteúdo bruto/PII em mensagens de erro ou logs de teste.
5. Garantir que fixtures e snapshots esperados sejam determinísticos e revisáveis no repositório.

## Privacidade e anonimização

É **proibido** commitar documentos reais contendo nome, CPF, RG, matrícula, endereço, e-mail, telefone, agência/conta real, número completo de cartão, código de barras real, identificadores bancários pessoais, empregador/funcionário identificável ou outros dados pessoais/financeiros reais.

A anonimização deve manter somente a estrutura necessária para reproduzir o comportamento técnico. Substituir identificadores por valores obviamente fictícios e substituir valores monetários por números artificiais quando o valor exato real não for necessário para reproduzir uma regra.

Não incluir hashes, metadados, nomes de arquivo ou trechos que permitam reconstruir a origem real. Se um documento não puder ser anonimizado com segurança sem perder a característica técnica necessária, ele **não deve entrar no repositório**; reproduzir somente a característica mínima em fixture sintético equivalente.

## Invariantes aplicáveis

Os testes não podem alterar a semântica de `INV-001` a `INV-022`. Em especial, validar quando aplicável:

- `INV-001`: transferência interna sem efeito operacional;
- `INV-002`: pagamento de cartão é conciliação, não nova despesa;
- `INV-003`/`INV-004`: aplicação/resgate são movimentos patrimoniais;
- `INV-011`/`INV-012`: holerite não duplica consignado e férias não viram renda extra indevida;
- `INV-014`/`INV-015`: duplicidade e fonte canônica preservam evidência sem dupla contagem;
- `INV-016`: estorno compensa gasto sem criar renda operacional comum;
- `INV-017`: compra de cartão segue competência canônica da fatura;
- invariantes de reconciliação/documento e confiança definidos no contrato vigente.

Se faltar fato obrigatório no fixture, o resultado correto é `unknown`/revisão conforme a regra, nunca fabricar saldo, total, competência ou data para fazer o teste passar.

## Proibições técnicas

- Não criar parser paralelo exclusivo para os testes.
- Não duplicar regra financeira no fixture/teste.
- Não alterar tolerâncias, classificações, thresholds ou invariantes apenas para acomodar um documento.
- Não introduzir auto-fix de transações/documentos.
- Não apagar ou normalizar silenciosamente evidência de duplicidade/reconciliação.
- Não reduzir cobertura ou enfraquecer asserts existentes.
- Não adicionar migrations salvo necessidade estritamente demonstrada; este slice deve ser, por padrão, somente fixtures/testes/documentação.
- Não commitar segredos, PII ou documentos reais brutos.

## Critérios de aceite

O PR só pode ser considerado concluído quando:

1. As fixtures anonimizadas não contêm PII/dados reais identificáveis após revisão textual/metadata aplicável.
2. Cada fixture possui um teste que documenta qual comportamento real está protegendo.
3. Os testes passam pelo pipeline canônico e verificam resultados determinísticos, não apenas ausência de exceção.
4. Nenhuma regra financeira, invariant, cálculo canônico ou reconciliação é alterada sem justificativa normativa explícita e revisão separada.
5. Erros continuam fail-closed/honestos: ausência de evidência nunca vira `pass` ou reconciliação fabricada.
6. Nenhuma migration destrutiva ou alteração silenciosa de dados existentes é introduzida.
7. Os 12 gates nomeados do CI passam no head final:
   - `lint`;
   - `unit`;
   - `financial-invariants`;
   - `property-tests`;
   - `parser-reconciliation`;
   - `snapshot-channel-consistency`;
   - `advisor-contract-security`;
   - `projection-parity`;
   - `frontend-syntax`;
   - `alembic-migration`;
   - `integration-postgres`;
   - `docker-build`.
8. O PR documenta quais formatos/casos reais foram representados, quais dados foram substituídos/anonymizados, quais contratos foram exercitados e qualquer limitação residual.

## Testes mínimos esperados

Sem limitar soluções melhores justificadas pela documentação:

- um extrato representativo que feche corretamente e outro com evidência insuficiente que permaneça `unknown`;
- uma fatura representativa com compras/créditos/pagamentos, garantindo que pagamento de fatura não vire nova despesa;
- um caso de holerite representativo validando bruto - descontos = líquido declarado e sem duplicação de consignado;
- um caso de estorno/reversão quando suportado pelo formato;
- um caso de duplicidade/reimportação preservando evidência e sem dupla contagem;
- teste de erro/entrada malformada garantindo que conteúdo bruto sensível não apareça na resposta/log observável;
- household isolation quando o teste atingir API/persistência.

## Riscos a observar

- Fixtures excessivamente simplificados que não reproduzem quirks reais de layout/encoding.
- Anonimização incompleta ou metadados esquecidos.
- Golden files frágeis que forçam mudanças indevidas de parser.
- Testes que validam implementação interna em vez do contrato público/canônico.
- Mudança de regra para fazer um documento específico passar.
- Duplicação de cálculo/reconciliação dentro dos próprios testes.

## Entrega esperada de Claude

Ao concluir, reportar no PR:

- lista dos fixtures adicionados e formato representado;
- estratégia de anonimização e evidência de ausência de PII;
- entrypoints/serviços canônicos exercitados;
- contratos/invariantes cobertos;
- qualquer bug real encontrado e como foi corrigido, se houver;
- migrations/dependências adicionadas, se houver;
- resultado dos 12 gates no head final;
- riscos/limitações residuais.

**Não fazer merge.**