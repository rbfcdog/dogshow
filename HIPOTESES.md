# Tarvo — regras confirmadas, decisões e pendências

## Proveniência

Fonte das confirmações: transcrição do financeiro fornecida pelo usuário, mantida apenas localmente. Não houve consulta direta ao Slack. Horários abaixo são os da transcrição, sem atribuir uma data absoluta a mensagens que só informam “Yesterday”. Respostas do financeiro prevalecem sobre nossas hipóteses anteriores; números sugeridos por participantes não são gabarito.

As regras deste documento estão aplicadas no backend `backend/src/`. Nenhuma devolução, contestação, pagamento ou emissão fiscal real é executada pelo código.

## 1. Checkout — confirmado pelo financeiro

| Regra | Fonte |
|---|---|
| Comissão integral = bruto × taxa contratada, linha por linha; não usar `valor_taxa` e não descontar custo da Orbe | Thread 1, 8:05 PM; thread 15, 10:34 PM |
| Comissão contratada também é a base da NF de checkout | Threads 1 e 5 |
| Arredondar ao centavo por linha com metade afastando-se de zero, inclusive negativos; somar depois | Thread 9, 9:13 PM |
| Divergência depende de taxa aplicada diferente da contratada; valor = bruto × diferença dos percentuais | Thread 9 |
| Centavo decorrente apenas do truncamento da Orbe não é divergência | Thread 9 |
| Estorno entra no mês do estorno; taxa e vigência da venda original, inclusive vendas de agosto | Thread 2, 8:06 PM |
| Antecipação entra na mesma comissão/NF; reversão anula a antecipação referenciada | Thread 5, 8:47 PM |
| Rota Global não teve reajuste contratual | Thread 7, 8:49 PM |
| Casa do Estudante: tarifa zero de setembro a dezembro para MDR e antecipação, sem alterar remessas | Thread 7 |
| Atlas: não faturar até o Comercial confirmar o contrato/taxa | Thread 4, 8:24 PM |
| Uma NF por lojista/mês, só com saldo positivo; negativo não é transportado automaticamente e exige tratamento por Helena | Thread 5 |
| Devolução ao lojista e cobrança à Orbe exigem decisão de Helena, com transação e valor | Thread 1 |
| Ajustes tardios da Orbe (06_orbe_ajustes_tardios_*.csv): incorporados com mesmo formato e regras do arquivo 01 | Comunicado Manuel Freitas 1:10 PM (seção 16) |
Associações exatas confirmadas na thread 4:

| EC | Cadastro vigente | CNPJ |
|---|---|---|
| `1043300981` | Novo Horizonte Intercambio | `66564512000199` |
| `1043396662` | Horizonte Viagens | `78576234000179` |
| `1038867654` | Kiwi Experience | `07752116000126` |

Os CNPJs foram obtidos das linhas vigentes indicadas pelo financeiro. Não há associação por similaridade de nome.

### Interpretação temporal adotada

A isenção da Casa incide sobre a data usada para determinar a taxa: data da venda/antecipação original. Por isso, o estorno em setembro de uma venda de agosto continua abatendo a taxa de agosto. A combinação das regras de isenção e estorno resulta no saldo de checkout de −R$ 5,50 observado no pacote; não há NF nem transporte automático desse saldo.

A pergunta preparada ao financeiro, mantida somente localmente, foi reformulada a pedido do usuário para se basear exclusivamente na observação e no comentário existentes no XLSX. A inspeção dos arquivos não demonstra isenção; a regra implementada acima tem fonte externa à planilha e não foi alterada nesta investigação. Esta nova redação não foi enviada pelo assistente.

### Bloqueios de checkout

- Atlas sem contrato não recebe comissão inventada ou nota. A pendência usa a taxa reportada pela Orbe como valor conhecido, e explicita que não é comissão apurada.
- Ausência da linha original de agosto não bloqueia por si só: data histórica e campos da operação precisam permitir identificar a taxa contratada.
- Origem ausente sem data/modalidade suficiente, contrato ambíguo ou faixa sem taxa ficam bloqueados.
- Reversão de antecipação deve comprovar anulação integral da referência; parcial ou inconsistente exige decisão, pois não há política confirmada para esses casos.
- Repetição de ID de checkout e múltiplas reversões integrais da mesma antecipação são rejeitadas; não existe autorização para deduplicação ou estorno duplo silencioso.

## 2. Remessas — confirmado pelo financeiro

| Regra | Fonte |
|---|---|
| Comissão = 50% do `Spread R$` realizado, não spread estimado nem valor recalculado pelo contrato | Thread 6, 8:48 PM |
| Arredondar a participação por operação com a mesma regra de centavos do checkout | Thread 9, 9:13 PM |
| Competência segue a contratação, inclusive liquidação em outubro | Thread 6 |
| Número de operação único: contar uma vez e pedir correção do relatório repetido | Threads 6 e 13 |
| Spreads negativos com trava vencida ficam fora da comissão/NF e são contestados | Thread 6 |
| Compliance não altera o tratamento financeiro; ressalvas não excluem operações | Thread 6 |
| Spread vazio/“padrão” = 1,5% sobre a PTAX | Thread 6 |
| Mundo Aberto negociou 1,20% desde julho; excesso precisa ser contestado | Thread 6 |
| Divergência cambial é pelo valor cheio, não pela metade da Tarvo | Thread 8, 8:58 PM |
| Realizado de Mundo Aberto entra em setembro mesmo contestado; ajuste só no mês da eventual devolução | Thread 11, 9:45 PM |
| Uma NF para Ponte Câmbio pelo total da participação, não uma por lojista | Thread 5 |
| Considerar contestações dentro do prazo; nenhuma foi aberta ainda | Thread 13, 10:34 PM |
| Conferir o repasse quando recebido é pendência de Helena; não prever descontos no TED | Thread 12, 10:33 PM |

O CNPJ da Ponte, `75374179000146`, consta do e-mail original. A discriminação fiscal usa serviço e competência, sem texto padrão imposto pelo financeiro.

## 3. Critérios cambiais decididos pelo time

### Tolerância

**Status: DECISÃO DO TIME**, autorizada como escolha documentada na thread 8; não é um limite imposto por Helena.

Ignorar diferença absoluta de percentual **menor que 0,01 ponto percentual**. Em fração, o limite é `0.0001`; diferenças iguais ou maiores são reportadas. A variação observada dos percentuais do relatório em torno de taxas regulares é bem menor que esse limite, enquanto Mundo Aberto apresenta diferença próxima de 0,30 ponto percentual. O objetivo é evitar ruído de precisão sem esconder diferença contratual material.

### Base, sinal e valor

Divergência por operação = `valor_ME × PTAX × (spread_aplicado − contratado)`, arredondada ao centavo com `ROUND_HALF_UP`.

- Usar o percentual aplicado da coluna `Spread %`, não deduzi-lo por divisão do spread monetário arredondado.
- Valor positivo representa aplicado acima do contrato; negativo, aplicado abaixo.
- Perdas excluídas continuam podendo gerar divergência e contestação pelo valor cheio assinado.
- A pendência de duplicata informa o spread integral excedente do relatório. Não é uma previsão do repasse nem autorização para somar esse valor ao de outras pendências como uma única exposição.
- A pendência de conferir o TED informa o total esperado da NF, não uma previsão de transferência líquida.
- IDs repetidos com conteúdo econômico conflitante são bloqueados até correção. Metadado “reprocessada” sozinho não autoriza escolher entre valores diferentes.
- Se o contrato do lojista não puder ser identificado, a comparação de taxas fica pendente; a participação conhecida no realizado não é recalculada nem bloqueada por falta dessa taxa. É uma decisão do time baseada na regra confirmada de 50% do realizado.

## 4. Bancos — confirmado pelo financeiro

| Fato/regra | Fonte |
|---|---|
| `conta_codigo` é a contrapartida do movimento bancário | Thread 10, 9:14 PM |
| Folha e tributos de agosto já estavam provisionados | Threads 7 e 10 |
| Repasse de agosto da Ponte recebido em 04/09 já era comissão a receber | Threads 7 e 10 |
| Além desses itens, tarifas, aluguel, cloud, assinaturas e serviços não estavam provisionados e entram na despesa | Thread 10 |
| PIX de R$ 7.350 não identificado não é receita | Thread 7 |
| Devolução de R$ 12.400 é repasse que voltou; motivo ainda desconhecido | Thread 7 |
| Saída de R$ 300 mil em 30/09 cai no Meridiano em 01/10 | Thread 7 |
| Orbe liquida tudo no Aurora, não paga lojistas diretamente | Thread 14, 10:34 PM |
| Saldo negativo do Aurora é conta garantida | Thread 14 |
| Conciliação parcela por parcela da Orbe com banco está fora deste fechamento | Thread 14 |

## 5. Classificações e premissas adotadas pelo time

A proposta de Lucca às 9:06 PM não foi validada literalmente. As contas abaixo aplicam os critérios gerais confirmados e o plano de contas fornecido.

| Evento | Contrapartida |
|---|---|
| Crédito da Orbe | `1.1.03.001` — Recebíveis de adquirente |
| Repasse a lojista e repasse devolvido | `2.1.01.001` — Repasses a lojistas a pagar |
| Transferência própria, inclusive em trânsito | `1.1.09.001` — Transferências entre contas próprias |
| PIX sem identificação | `1.1.09.002` — Créditos não identificados |
| Recursos/ liquidação de remessas | `2.1.02.001` — Recursos de lojistas para remessas a liquidar |
| Comissão cambial de agosto recebida | `1.1.03.002` — Comissões de intermediação a receber |
| Folha provisionada | `2.1.04.001` — Salários a pagar |
| INSS/FGTS provisionados | `2.1.04.002` / `2.1.04.003` |
| PIS/COFINS/ISS/IRRF provisionados | `2.1.03.001` / `.002` / `.003` / `.004` |
| Tarifa/condomínio/cloud/assinatura/seguro/contabilidade | Respectivas contas analíticas de despesa `4.2` |
| Aplicação e resgate de principal | `1.1.02.001` — Aplicação automática Meridiano |
| Rendimento explícito | `3.2.01` — Rendimentos de aplicações |
| IOF explícito | `4.3.01` — IOF |

Premissas específicas:

1. Após regras de fornecedores identificados, `PIX ENVIADO` pelo Aurora é tratado como repasse a lojista. Essa regra é uma premissa operacional do time, não uma confirmação individual de cada beneficiário.
2. Usar a transitória `1.1.09.002` para o crédito sem identificação é decisão do time. Sua natureza de cadastro não é tratada como proibição de contrapartida credora; a conta é analítica e aceita lançamento.
3. Essas duas decisões usam confiança `0.80`; regras exatas usam `1`. Não são probabilidades estatisticamente calibradas. Desconhecido/conta não lançável usa confiança `0` e pendência.
4. Transferência própria exige par inequívoco para conciliação de mesma data/valor. O trânsito confirmado de 30/09 para 01/10 permanece classificado e pendente de conferência do extrato futuro, sem inventar entrada de setembro.
5. Não calcular renda de aplicação pela diferença entre aplicações e resgates. Não inventar saldo inicial do Meridiano nem juros da conta garantida.

### Conferência determinística de recursos do câmbio

As operações são comparadas apenas depois de deduplicar IDs econômicos; spread negativo/zero ou ausência de contrato para comparar taxa não elimina principal + IOF da conferência de recursos.

O controle diário compara TEDs enviadas à Ponte com principal + IOF por contratação. O mensal compara PIX recebidos por cliente com as operações identificadas no recorte. CNPJ explícito tem precedência; sem CNPJ, correspondência nominal é uma hipótese determinística de conferência, com nomes completos normalizados/prefixo, mínimo dois termos e candidato único. Ela não cria cadastro ou taxa nem comprova conciliação documental de uma operação.

Fonte bancária ausente, cobertura incompleta de operações ou PIX sem cliente único não viram caixa zero ou déficit inventado. PIX não atribuível recebe pendência com o valor bancário observado; comparação mensal insuficiente recebe valor desconhecido e não altera comissão/NF. As diferenças monetárias têm rastro assinado de banco menos operações.

Não pressupor saldo inicial zero: diferença dos fluxos do mês pode depender de saldo anterior, adiantamento, operação ausente ou devolução. Não somar diferenças diárias e mensais como exposição.

O residual observado da Ponte Aérea Estudantil, CNPJ `95739378000118`, é R$ 18.240,00. Sua natureza continua sem confirmação; a implementação apenas o registra para apuração. Nenhum valor é devolvido ou deduzido da comissão automaticamente.

## 6. Pendências não resolvidas automaticamente

- Comercial: confirmar contrato/taxa da Atlas.
- Helena/Comercial: revisar diferenças de checkout e autorizar devoluções, inclusive Casa do Estudante.
- Helena: tratar saldo negativo de checkout, sem transporte automático.
- Helena: abrir contestações cambiais e pedir correção da operação repetida à Ponte.
- Helena: conferir o repasse futuro da Ponte sem assumir abatimentos.
- Helena: identificar o PIX, esclarecer o motivo da devolução e conferir a transferência em trânsito em outubro.
- Helena / Ponte Câmbio: esclarecer a diferença de R$ 18.240,00 nos recursos identificados da Ponte Aérea Estudantil, sem presumir devolução, receita ou alteração da comissão.

A execução conclui sem pedir respostas no meio, mas não inventa essas decisões. Cada pendência conserva motivo, responsável, base do valor e rastro. Valores desconhecidos permanecem vazios, não `0.00`; valores conhecidos de naturezas diferentes não são somados como exposição total.

## 7. Verificação e restrições da entrega

No último lote, a normalização contratual registrou 27 chamadas reais concluídas à OpenAI e nenhum cache reutilizado. Os seis CSVs preliminares contêm 5.085 contribuições no rastro, conferidas por `id_saida` contra cada valor monetário exportado, sem divergências. Com as mesmas fontes, a segunda execução deixou 86 transações do EC 1038867654 pendentes de confirmação contratual e reduziu a comissão apurada em R$ 10.886,22; o fechamento não está aprovado. A classificação bancária revisada depende de aprovação humana de nova referência; sem ela, 109 movimentos permanecem sem conta.

Os seis CSVs de `saida/`, o README, as hipóteses e o histórico de prompts são entregáveis versionáveis; fontes financeiras em `dados_tarvo/`, credenciais em `.env` e caches/logs de IA continuam fora do Git. O fechamento padrão requer chamadas novas à OpenAI para normalizar contratos antes da apuração.
