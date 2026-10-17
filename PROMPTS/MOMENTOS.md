## 1. Corrigir a leitura da planilha

Pedimos análise do pacote, e a IA inicialmente afirmou que o XLSX tinha colunas desalinhadas. A conferência das coordenadas mostrou células vazias omitidas pelo leitor textual; corrigimos o diagnóstico antes de tratar aquilo como erro dos dados.

Registros: 014–016 (`216521a3`, `6b479da5`, `8e399976`).

## 2. Separar diagnóstico de fechamento

Pedimos um backend Python modular, e a IA entregou uma API que lia 1.943 lançamentos de checkout, 233 linhas cambiais e 184 movimentos bancários. Mantivemos a primeira entrega identificada como **diagnóstico**, sem apresentá-la como comissão ou faturamento aprovados.

Registros: 021–025 (`b6931516`, `f297c95e`, `70e1a727`).

## 3. Corrigir o papel da IA no produto

Questionamos se o case precisava de LLM e contestamos a recomendação inicial de deixá-la apenas no desenvolvimento. A IA reviu a proposta para investigar exceções e estruturar informação, enquanto cálculos financeiros permaneciam determinísticos e falta de contrato continuava visível.

Registros: 031–036 (`26754388`, `05887062`, `b9513493`, `a19fa54c`).

## 4. Substituir premissas por respostas do financeiro

Fornecemos as threads e pedimos aplicação no backend; a IA distinguiu regras confirmadas de propostas ainda não implementadas. Incorporamos arredondamento por operação, isenção da Casa e bloqueio da Atlas e registramos a execução dos seis CSVs com rastro.

Registros: 043, 045–046 e 051 (`a75b8a03`, `08ce5960`, `50be6a64`, `a0f00c0c`).

## 5. Formular uma dúvida que altera a nota

Pedimos uma pergunta concreta para o financeiro, e a IA preparou em `docs/PERGUNTA_FINANCEIRO_CASA_ESTUDANTE.md` o caso do estorno de venda de agosto durante a isenção de setembro. O valor de −R$ 5,50 tornou explícito por que a regra mudava saldo e nota, em vez de aceitar uma resposta genérica.

Registros: 052–055 (`9e2379a1`, `7285ae45`, `8920db8d`, `6045b399`).

## 6. Não apagar a vigência antiga da Bridge

Propusemos usar IA para completar ECs e retirar duplicatas da referência contratual; a IA observou que as duas linhas da Bridge eram versões válidas em períodos distintos. Registramos que a normalização deve preservar ambas e escolher a taxa pela data da venda, não eliminar a linha anterior.

Registros: 056–057 (`bd879c2b`, `44f62014`); decisão detalhada em `docs/NOTAS_SOLUCAO.md`.

## 7. Reagir ao feedback de repetibilidade da banca

Enviamos à IA o retorno de Manuel: ela não deveria escolher cadastro e taxa no caminho de cada número, e perguntas materiais precisavam chegar cedo ao financeiro. Revisamos o plano para usar referências fundamentadas e revisáveis antes da apuração, mantendo execução em lote sem aprovação por transação.

Registros: 076–078 (`1d81721e`, `99ef0eab`, `3ad6cdc1`); revisão em `docs/PLANO_SOLUCAO_TARVO.md`.

## 8. Testar a IA bancária além da taxa de acerto

Pedimos avaliação cega da classificação bancária: a IA acertou 18 de 20 casos, mas citou linha inválida, não ligou uma TED às remessas do dia e atribuiu ao financeiro uma premissa do time. Restringimos IDs e contas válidos, incluímos candidatos cambiais do dia e separamos autoridades no dossiê de revisão.

Registros: 088–089; correções e verificação em `docs/NOTAS_SOLUCAO.md`, seção 13.

## 9. Incorporar dados novos sem edição manual

Repassamos à IA o aviso das 14 linhas tardias da Orbe e perguntamos se o e-mail da Ponte entrava na referência. A ingestão passou a preservar origem das novas linhas e a extração do corpo do EML alimentou normalização e RCA; a execução documentada conferiu 28 contribuições adicionais no rastro.

Registro: 091 (`d63ffb22`); implementação e conferência em `docs/NOTAS_SOLUCAO.md`, seção 12.
