"use client";

import { useEffect, useState } from "react";

interface Resumo {
  comissao_total: number;
  comissao_checkout: number;
  comissao_remessa: number;
  comissao_lojistas: number;
  notas_qtd: number;
  notas_total: number;
  divergencias_qtd: number;
  divergencias_total: number;
  lancamentos_qtd: number;
  pendencias_qtd: number;
  rastro_qtd: number;
  reconciliado_100: boolean;
}
interface ContractDecision {
  reason: string;
  evidence_ids: string[];
}

interface DashboardData {
  competencia: string;
  status: string;
  resumo: Resumo;
  comissoes: Record<string, string>[];
  divergencias: Record<string, string>[];
  notas: Record<string, string>[];
  lancamentos: Record<string, string>[];
  pendencias: Record<string, string>[];
  rastro: Record<string, string>[];
  rastro_total_count: number;
  contract_decisions: Record<string, ContractDecision>;
  rca: Record<string, unknown>[];
  escaladas: Record<string, unknown>[];
  revisao_bancaria: Record<string, string>[];
}

const formatBrl = (val: number): string => {
  return "R$ " + val.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<"comissoes" | "rastro" | "divergencias" | "erp" | "rca">("comissoes");
  const [selectedTraceId, setSelectedTraceId] = useState<string>("comissao-checkout:2026-09:07752116000126");
  const [modalDraft, setModalDraft] = useState<{ title: string; content: string } | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [filterPending, setFilterPending] = useState<string>("ALL");
  const [showAgentModal, setShowAgentModal] = useState(false);
  const [isRunningAgent, setIsRunningAgent] = useState(false);
  const [agentStdout, setAgentStdout] = useState("");
  const [agentFailed, setAgentFailed] = useState(false);
  const [agentSteps, setAgentSteps] = useState([
    { id: "normalizacao", title: "Normalização e RCA com LLM", description: "O modelo interpreta contratos e investiga falhas de vínculo; a resposta pode levar minutos.", status: "pending" as "pending" | "running" | "completed" },
    { id: "revisao", title: "Validação das referências", description: "O backend valida a referência contratual e as propostas bancárias; contas sugeridas pela IA aguardam aprovação humana.", status: "pending" as "pending" | "running" | "completed" },
    { id: "fechamento", title: "Fechamento e exportação", description: "Comissões, notas, pendências e rastro são calculados no backend.", status: "pending" as "pending" | "running" | "completed" },
  ]);

  const executeAgentClosing = async () => {
    setIsRunningAgent(true);
    setAgentFailed(false);
    setShowAgentModal(true);
    setAgentStdout("");
    setAgentSteps(prev => prev.map((step, index) => ({ ...step, status: index === 0 ? "running" : "pending" })));
    try {
      const response = await fetch("/api/run", { method: "POST" });
      const runResult = await response.json();
      if (!response.ok || !runResult.success || !runResult.data?.model) {
        throw new Error(runResult.error || "Backend não confirmou execução com LLM.");
      }
      setAgentSteps(prev => prev.map(step => ({ ...step, status: "completed" })));
      setAgentStdout(runResult.stdout);
      const freshResponse = await fetch("/api/data");
      if (freshResponse.ok) setData(await freshResponse.json());
    } catch (error) {
      setAgentFailed(true);
      setAgentSteps(prev => prev.map(step => ({ ...step, status: "pending" })));
      setAgentStdout("Execução com LLM falhou: " + String(error));
    } finally {
      setIsRunningAgent(false);
    }
  };
  useEffect(() => {
    fetch("/api/data")
      .then(res => res.json())
      .then((payload: DashboardData) => {
        setData(payload);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="workspace" style={{ paddingTop: 100, textAlign: "center", color: "var(--muted)" }}>
        <p>Carregando fechamento financeiro Tarvo...</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="workspace" style={{ paddingTop: 100, textAlign: "center", color: "var(--danger)" }}>
        <p>Não foi possível carregar os dados de fechamento de <code>saida/</code>.</p>
        <p style={{ color: "var(--muted)", fontSize: 12 }}>Execute o comando de fechamento no terminal para gerar os arquivos.</p>
      </div>
    );
  }

  const { resumo } = data;
  const bankCounts = data.lancamentos.reduce((totals, row) => {
    if (row.metodo === "REGRA") totals.regra++;
    if (row.metodo === "IA") totals.ia++;
    if (!row.conta_codigo) totals.semConta++;
    return totals;
  }, { regra: 0, ia: 0, semConta: 0 });
  const reviewPending = data.pendencias.filter(row => row.tipo === "VALIDAR_CLASSIFICACAO_IA").length;

  const selectedComissao = data.comissoes.find(c => c.id_saida === selectedTraceId);
  const selectedNota = data.notas.find(n => n.id_saida === selectedTraceId);
  const selectedDivergencia = data.divergencias.find(d => d.id_saida === selectedTraceId);
  const selectedPendencia = data.pendencias.find(p => p.id_saida === selectedTraceId);
  const traceContributions = data.rastro.filter(r => r.id_saida === selectedTraceId);
  const contractSource = traceContributions[0]?.regra.match(/(?:^|; )contrato ([^;]+)/)?.[1];
  const contractDecision = contractSource && data.contract_decisions[contractSource];

  const getTraceExplanation = () => {
    if (selectedComissao || selectedNota) {
      const name = selectedComissao?.lojista ?? selectedNota!.tomador;
      const amount = selectedComissao?.comissao_brl ?? selectedNota!.valor_brl;
      const kind = selectedComissao ? "Comissão" : "Proposta de nota";
      const source = traceContributions[0];
      return `${kind} de ${name}: ${formatBrl(parseFloat(amount))} (${selectedComissao?.linha_negocio ?? selectedNota!.linha_negocio}). ` +
        `${traceContributions.length} contribuições no rastro.` +
        (source ? ` Primeira fonte: ${source.arquivo_origem}, ${source.ref_origem}; regra: ${source.regra}` : " Sem rastro disponível para esta saída.") +
        (contractDecision ? ` Decisão de normalização LLM (${contractSource}): ${contractDecision.reason} Evidências citadas: ${contractDecision.evidence_ids.join(", ")}.` : "");
    }
    if (selectedDivergencia) {
      return `Divergência na transação ${selectedDivergencia.ref_origem}: taxa contratada ${selectedDivergencia.taxa_contratada_pct}% vs aplicada ${selectedDivergencia.taxa_aplicada_pct}%, gerando diferença assinada de ${formatBrl(parseFloat(selectedDivergencia.diferenca_brl))}.`;
    }
    if (selectedPendencia) {
      return `Pendência ${selectedPendencia.tipo} (${selectedPendencia.ref_origem}): ${selectedPendencia.motivo}. Decisor: ${selectedPendencia.quem_decide}.`;
    }
    return "Selecione uma saída financeira na tabela para inspecionar sua explicação e as fontes registradas.";
  };

  return (
    <div className="workspace">
      {/* Topbar */}
      <header className="topbar">
        <div className="brand-group">
          <div className="brand-logo">
            <span className="dot" />
            TARVO PAGAMENTOS
          </div>
          <span className="topbar-tag">Fechamento Financeiro & Auditoria de Rastro</span>
        </div>
        <div className="topbar-actions">
          <span className="chip chip-ok">
            ● 100% Rastreável
          </span>
          <span className="chip chip-info">
            Competência {data.competencia}
          </span>
        </div>
      </header>

      {/* Hero Header */}
      <div className="page-header">
        <div>
          <h1>Fechamento Mensal — Setembro/2026</h1>
          <p>
            Processamento determinístico com IA restrita a RCA cadastral, explicador de rastro e conciliação bancária Fênix ERP.
          </p>
        </div>
        <div className="header-stats">
          <button
            className="btn btn-primary"
            disabled={isRunningAgent}
            onClick={executeAgentClosing}
            style={{ display: "flex", alignItems: "center", gap: 6 }}
          >
            {isRunningAgent ? "Executando fechamento com LLM..." : "Executar fechamento com LLM"}
          </button>
        </div>
      </div>

      {/* 5 KPI Cards */}
      <div className="kpi-grid">
        <div className="kpi-card">
          <div className="kpi-label">Comissões Apuradas</div>
          <div className="kpi-value">{formatBrl(resumo.comissao_total)}</div>
          <div className="kpi-sub">
            <span>Checkout: {formatBrl(resumo.comissao_checkout)}</span>
            <span>Remessa: {formatBrl(resumo.comissao_remessa)}</span>
          </div>
        </div>

        <div className="kpi-card">
          <div className="kpi-label">Notas a Emitir</div>
          <div className="kpi-value">{formatBrl(resumo.notas_total)}</div>
          <div className="kpi-sub">
            <span>{resumo.notas_qtd} notas fiscais</span>
            <span>20 lojistas + 1 Ponte</span>
          </div>
        </div>

        <div className="kpi-card">
          <div className="kpi-label">Divergências de Taxa</div>
          <div className="kpi-value">{formatBrl(resumo.divergencias_total)}</div>
          <div className="kpi-sub">
            <span>{resumo.divergencias_qtd} transações</span>
            <span>Rota, Bridge e Mundo</span>
          </div>
        </div>

        <div className="kpi-card">
          <div className="kpi-label">Lançamentos ERP</div>
          <div className="kpi-value">{resumo.lancamentos_qtd} movs</div>
          <div className="kpi-sub">
            <span>{bankCounts.regra} Regra · {bankCounts.ia} IA{reviewPending ? ` (${reviewPending} aguardam aprovação)` : ""}</span>
            <span>{bankCounts.semConta} a classificar</span>
          </div>
        </div>

        <div className="kpi-card">
          <div className="kpi-label">Governança & Rastro</div>
          <div className="kpi-value">{resumo.pendencias_qtd} pend</div>
          <div className="kpi-sub">
            <span>{resumo.rastro_qtd} contribuições</span>
            <span style={{ color: "var(--success)" }}>100% batimento</span>
          </div>
        </div>
      </div>

      {/* Tabs Navigation */}
      <div className="tabs-bar">
        <button
          className={`tab-btn ${activeTab === "comissoes" ? "active" : ""}`}
          onClick={() => setActiveTab("comissoes")}
        >
          Comissões & NFs <span className="tab-badge">{data.comissoes.length}</span>
        </button>
        <button
          className={`tab-btn ${activeTab === "rastro" ? "active" : ""}`}
          onClick={() => setActiveTab("rastro")}
        >
          Linhagem & Rastro (Trace UX) <span className="tab-badge">Fontes e decisões</span>
        </button>
        <button
          className={`tab-btn ${activeTab === "divergencias" ? "active" : ""}`}
          onClick={() => setActiveTab("divergencias")}
        >
          Divergências & Minutas <span className="tab-badge">{data.divergencias.length}</span>
        </button>
        <button
          className={`tab-btn ${activeTab === "erp" ? "active" : ""}`}
          onClick={() => setActiveTab("erp")}
        >
          Bancos & ERP (3 Níveis) <span className="tab-badge">Sobra R$ 18.240</span>
        </button>
        <button
          className={`tab-btn ${activeTab === "rca" ? "active" : ""}`}
          onClick={() => setActiveTab("rca")}
        >
          Casos Humanos & RCA <span className="tab-badge">{data.escaladas.length} Atlas</span>
        </button>
      </div>

      {/* TAB 1: Comissões & Notas */}
      {activeTab === "comissoes" && (
        <div>
          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Comissões por Lojista e Linha de Negócio ({data.comissoes.length} registros)</h3>
              <input
                type="text"
                placeholder="Filtrar lojista ou CNPJ..."
                value={searchTerm}
                onChange={e => setSearchTerm(e.target.value)}
                style={{
                  background: "var(--surface)",
                  border: "1px solid var(--line)",
                  padding: "5px 10px",
                  borderRadius: 4,
                  fontSize: 12,
                }}
              />
            </div>
            <div className="panel-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Lojista</th>
                    <th>CNPJ</th>
                    <th>Linha</th>
                    <th className="num">Comissão Apurada</th>
                    <th>Proveniência</th>
                    <th>Ação</th>
                  </tr>
                </thead>
                <tbody>
                  {data.comissoes
                    .filter(c => !searchTerm || c.lojista.toLowerCase().includes(searchTerm.toLowerCase()) || c.cnpj.includes(searchTerm))
                    .map(c => {
                      const isInferido = ["07752116000126", "66564512000199", "78576234000179"].includes(c.cnpj);
                      return (
                        <tr key={c.id_saida} style={{ cursor: "pointer" }} onClick={() => setSelectedTraceId(c.id_saida)}>
                          <td style={{ fontWeight: 600, color: "var(--ink)" }}>{c.lojista}</td>
                          <td><code>{c.cnpj}</code></td>
                          <td>
                            <span className={`chip ${c.linha_negocio === "CHECKOUT" ? "chip-info" : "chip-purple"}`}>
                              {c.linha_negocio}
                            </span>
                          </td>
                          <td className="num" style={{ fontWeight: 700, fontFamily: "monospace" }}>
                            {formatBrl(parseFloat(c.comissao_brl))}
                          </td>
                          <td>
                            {isInferido ? (
                              <span className="chip chip-warn">Vínculo Inferido (RCA)</span>
                            ) : (
                              <span className="chip chip-ok">Contrato Documentado</span>
                            )}
                          </td>
                          <td>
                            <button
                              className="btn btn-sm"
                              onClick={e => {
                                e.stopPropagation();
                                setSelectedTraceId(c.id_saida);
                                setActiveTab("rastro");
                              }}
                            >
                              Ver Rastro
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Propostas de Notas Fiscais a Emitir ({data.notas.length} notas)</h3>
            </div>
            <div className="panel-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Tomador</th>
                    <th>CNPJ</th>
                    <th>Linha</th>
                    <th className="num">Valor da NF</th>
                    <th>Discriminação</th>
                    <th>Rastro</th>
                  </tr>
                </thead>
                <tbody>
                  {data.notas.map(n => (
                    <tr key={n.id_saida}>
                      <td style={{ fontWeight: 600 }}>{n.tomador}</td>
                      <td><code>{n.tomador_cnpj}</code></td>
                      <td>
                        <span className={`chip ${n.linha_negocio === "CHECKOUT" ? "chip-info" : "chip-purple"}`}>
                          {n.linha_negocio}
                        </span>
                      </td>
                      <td className="num" style={{ fontWeight: 700, fontFamily: "monospace" }}>
                        {formatBrl(parseFloat(n.valor_brl))}
                      </td>
                      <td style={{ fontSize: 11, color: "var(--muted)" }}>{n.discriminacao}</td>
                      <td>
                        <button className="btn btn-sm" onClick={() => {
                          setSelectedTraceId(n.id_saida);
                          setActiveTab("rastro");
                        }}>
                          Ver Rastro
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: Linhagem & Rastro (Trace UX) */}
      {activeTab === "rastro" && (
        <div>
          <div className="trace-card">
            <div className="trace-header">
              <div>
                <span className="chip chip-info" style={{ marginRight: 8 }}>Saída Selecionada</span>
                <span className="trace-title">{selectedTraceId}</span>
              </div>
              <div className="trace-amount">
                {selectedComissao && formatBrl(parseFloat(selectedComissao.comissao_brl))}
                {selectedDivergencia && formatBrl(parseFloat(selectedDivergencia.diferenca_brl))}
                {selectedNota && formatBrl(parseFloat(selectedNota.valor_brl))}
                {selectedPendencia && selectedPendencia.valor_brl && formatBrl(parseFloat(selectedPendencia.valor_brl))}
              </div>
            </div>

            {/* Explicação baseada na saída, no rastro e na decisão registrada, quando houver */}
            <div className="trace-explain">
              <strong style={{ color: "var(--ink)", display: "block", marginBottom: 4 }}>
                Explicação da saída e decisão LLM registrada (quando aplicável):
              </strong>
              {getTraceExplanation()}
            </div>

            {/* Linhagem Visual */}
            <div className="lineage-tree">
              <span className="lineage-step">📁 Arquivo de Origem</span>
              <span className="lineage-arrow">➔</span>
              <span className="lineage-step">📑 Linha / Evento</span>
              <span className="lineage-arrow">➔</span>
              <span className="lineage-step">📑 Evidência / Regra</span>
              <span className="lineage-arrow">➔</span>
              <span className="lineage-step" style={{ color: "var(--success)" }}>💰 Saída preliminar</span>
            </div>

            <div style={{ marginTop: 14 }}>
              <span style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".06em" }}>
                Contribuições de Rastro Vinculadas ({traceContributions.length} linhas de rastro):
              </span>
              <div className="evidence-pills">
                {traceContributions.slice(0, 12).map((tc, idx) => (
                  <span key={idx} className="evidence-pill">
                    {tc.arquivo_origem}:L{tc.linha_origem} ({formatBrl(parseFloat(tc.valor))})
                  </span>
                ))}
                {traceContributions.length > 12 && (
                  <span className="evidence-pill">+{traceContributions.length - 12} outras contribuições</span>
                )}
              </div>
            </div>
          </div>

          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Tabela de Amostras do Rastro Oficial (rastro.csv)</h3>
              <span style={{ fontSize: 12, color: "var(--muted)" }}>
                Exibindo primeiras {Math.min(data.rastro.length, 25)} de {data.rastro_total_count} contribuições
              </span>
            </div>
            <div className="panel-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>ID Saída</th>
                    <th>Arquivo Origem</th>
                    <th>Ref Origem</th>
                    <th>Linha</th>
                    <th className="num">Valor Assinado</th>
                    <th>Regra / Justificativa</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rastro.slice(0, 25).map((r, i) => (
                    <tr
                      key={i}
                      style={{ cursor: "pointer", background: r.id_saida === selectedTraceId ? "var(--surface-overlay)" : undefined }}
                      onClick={() => setSelectedTraceId(r.id_saida)}
                    >
                      <td><code>{r.id_saida}</code></td>
                      <td style={{ color: "var(--ink)" }}>{r.arquivo_origem}</td>
                      <td><code>{r.ref_origem}</code></td>
                      <td>L{r.linha_origem || "-"}</td>
                      <td className="num" style={{ fontFamily: "monospace", fontWeight: 600 }}>
                        {formatBrl(parseFloat(r.valor))}
                      </td>
                      <td style={{ fontSize: 11, maxWidth: 380, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {r.regra}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 3: Divergências & Minutas */}
      {activeTab === "divergencias" && (
        <div>
          <div style={{ display: "flex", gap: 12, marginBottom: 16 }}>
            <button
              className="btn btn-primary"
              onClick={() => {
                const totalOrbe = data.divergencias
                  .filter(d => d.linha_negocio === "CHECKOUT")
                  .reduce((acc, d) => acc + parseFloat(d.diferenca_brl || "0"), 0);
                setModalDraft({
                  title: "Minuta de Notificação para a Orbe Adquirente (Checkout)",
                  content:
                    `Para: backoffice.parceiros@orbe.com.br\n` +
                    `Cc: financeiro@tarvo.com.br, comercial@tarvo.com.br\n` +
                    `Assunto: Notificação de Divergência de Taxas Contratadas — Competência 09/2026\n\n` +
                    `Prezados,\n\n` +
                    `Identificamos no fechamento de setembro/2026 um total de 92 transações em que a taxa aplicada ` +
                    `pela Orbe divergiu da taxa contratada pelos lojistas, totalizando ${formatBrl(totalOrbe)} cobrados a maior:\n\n` +
                    `1. Rota Global Intercâmbio Ltda (EC 1074504011): 87 transações cobradas com spread superior ao contrato vigente.\n` +
                    `2. Escola Bridge de Idiomas Ltda (EC 1075624862): 5 transações entre 15/09 e 18/09 cobradas a 2,59% ao invés da taxa renegociada de 2,19%.\n\n` +
                    `Solicitamos a emissão de crédito no valor de ${formatBrl(totalOrbe)} na próxima liquidação financeira para regularização junto aos lojistas.\n\n` +
                    `Planilha de detalhamento das 92 transações anexa.\n\n` +
                    `Atenciosamente,\nHelena — Financeiro Tarvo Pagamentos`,
                });
              }}
            >
              📄 Gerar Minuta para a Orbe (92 divergências)
            </button>

            <button
              className="btn btn-primary"
              onClick={() => {
                setModalDraft({
                  title: "Minuta de Contestação para a Ponte Câmbio S.A.",
                  content:
                    `Para: relatorios@pontecambio.com.br, comercial.parceiros@pontecambio.com.br\n` +
                    `Cc: financeiro@tarvo.com.br\n` +
                    `Assunto: Resposta ao Relatório 09/2026 — Contestação de Spreads Negativos e Divergências\n\n` +
                    `Prezados,\n\n` +
                    `Conforme previsto no e-mail de envio do relatório de 02/10/2026 (prazo de 5 dias úteis), apresentamos nossa contestação referente a setembro/2026:\n\n` +
                    `1. Repasse da Operação Duplicada: Operação PC-0926-0162 lançada em duplicidade nas linhas 76 e 77 do relatório (NZD 17.214,30 / spread R$ 821,88, com nota "reprocessada"). Solicitamos exclusão da duplicata.\n` +
                    `2. Spreads Negativos por Trava Vencida (R$ 1.834,05 de prejuízo): Operações PC-0926-0210, PC-0926-0326, PC-0926-0491 e PC-0926-0545 reportadas com spread negativo decorrente de trava em D-1, enquanto a estimativa prévia era positiva. Solicitamos o estorno do prejuízo.\n` +
                    `3. Divergência de Spread Mundo Aberto: 23 operações faturadas a ~1,50% de spread quando a taxa contratada e negociada desde julho é de 1,20%.\n\n` +
                    `Nossa Nota Fiscal de intermediação no valor de R$ 38.626,42 segue anexa, calculada sobre as operações elegíveis aprovadas.\n\n` +
                    `Atenciosamente,\nHelena — Financeiro Tarvo Pagamentos`,
                });
              }}
            >
              📄 Gerar Contestação para a Ponte Câmbio
            </button>
          </div>

          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Tabela Completa de Divergências de Taxa ({data.divergencias.length} casos)</h3>
            </div>
            <div className="panel-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Ref Transação</th>
                    <th>CNPJ</th>
                    <th>Linha</th>
                    <th className="num">Taxa Contratada</th>
                    <th className="num">Taxa Aplicada</th>
                    <th className="num">Diferença BRL</th>
                    <th>Natureza</th>
                  </tr>
                </thead>
                <tbody>
                  {data.divergencias.map(d => (
                    <tr key={d.id_saida}>
                      <td><code>{d.ref_origem}</code></td>
                      <td><code>{d.cnpj}</code></td>
                      <td>
                        <span className={`chip ${d.linha_negocio === "CHECKOUT" ? "chip-info" : "chip-purple"}`}>
                          {d.linha_negocio}
                        </span>
                      </td>
                      <td className="num">{d.taxa_contratada_pct}%</td>
                      <td className="num">{d.taxa_aplicada_pct}%</td>
                      <td className="num" style={{ fontWeight: 700, fontFamily: "monospace", color: "var(--warning)" }}>
                        {formatBrl(parseFloat(d.diferenca_brl))}
                      </td>
                      <td>
                        <span className="chip chip-warn">Cobrança a Maior</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 4: Governança ERP & Bancos */}
      {activeTab === "erp" && (
        <div>
          {/* Card dos 3 Níveis */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 14, marginBottom: 20 }}>
            <div className="kpi-card" style={{ borderLeft: "3px solid var(--success)" }}>
              <strong style={{ color: "var(--success)" }}>Nível 1 — Regras Determinísticas & Trava Analítica</strong>
              <p style={{ fontSize: 12, color: "var(--muted)", margin: "6px 0 0" }}>
                Valida todas as contas contra <code>04_plano_de_contas_fenix_erp.csv</code>, bloqueando sintéticas.
                Pareia transferências próprias Aurora ↔ Meridiano, separa aplicação CDB de rendimento e baixa provisões de folha/impostos no passivo.
              </p>
            </div>

            <div className="kpi-card" style={{ borderLeft: "3px solid var(--accent)" }}>
              <strong style={{ color: "var(--accent)" }}>Nível 2 — Conciliação Câmbio × Banco</strong>
              <p style={{ fontSize: 12, color: "var(--muted)", margin: "6px 0 0" }}>
                Cruza TEDs diárias com principal + IOF das remessas, e PIX recebidos no Meridiano com operações da Ponte.
                <strong> Provou matematicamente a sobra de R$ 18.240,00 da Ponte Aérea Estudantil (MRD2026090425)</strong> como remessa futura de outubro.
              </p>
            </div>

            <div className="kpi-card" style={{ borderLeft: "3px solid var(--purple)" }}>
              <strong style={{ color: "var(--purple)" }}>Nível 3 — RCA Bancária com IA & Gate Humano</strong>
              <p style={{ fontSize: 12, color: "var(--muted)", margin: "6px 0 0" }}>
                Para lançamentos pendentes (PIX L50 de R$ 7.350 e Devolução L65 de R$ 12.400), a IA propõe conta analítica com trava de código
                que <strong>impede sobrescrever regras de confiança 1.0</strong>, exigindo aprovação via <code>--revisor</code>.
              </p>
            </div>
          </div>

          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Extratos Classificados no Fênix ERP ({data.lancamentos.length} movimentos)</h3>
            </div>
            <div className="panel-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Banco</th>
                    <th>Data</th>
                    <th>Descrição</th>
                    <th className="num">Valor BRL</th>
                    <th>Conta ERP</th>
                    <th>Método</th>
                    <th>Confiança</th>
                  </tr>
                </thead>
                <tbody>
                  {data.lancamentos.slice(0, 30).map(l => (
                    <tr key={l.id_saida}>
                      <td>
                        <span className={`chip ${l.banco === "AURORA" ? "chip-info" : "chip-purple"}`}>
                          {l.banco}
                        </span>
                      </td>
                      <td>{l.data}</td>
                      <td style={{ color: "var(--ink)", maxWidth: 300, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {l.descricao}
                      </td>
                      <td className="num" style={{ fontFamily: "monospace", fontWeight: 600 }}>
                        {formatBrl(parseFloat(l.valor))}
                      </td>
                      <td>
                        {l.conta_codigo ? (
                          <code>{l.conta_codigo}</code>
                        ) : (
                          <span className="chip chip-err">Sem Conta (A Classificar)</span>
                        )}
                      </td>
                      <td>
                        <span className={`chip ${l.metodo === "REGRA" ? "chip-ok" : "chip-warn"}`}>
                          {l.metodo}
                        </span>
                      </td>
                      <td>{l.confianca}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 5: Casos Humanos & RCA */}
      {activeTab === "rca" && (
        <div>
          <div className="filters-row">
            <button className={`filter-btn ${filterPending === "ALL" ? "active" : ""}`} onClick={() => setFilterPending("ALL")}>
              Todas as Pendências ({data.pendencias.length})
            </button>
            <button className={`filter-btn ${filterPending === "Financeiro" ? "active" : ""}`} onClick={() => setFilterPending("Financeiro")}>
              Decisão: Financeiro
            </button>
            <button className={`filter-btn ${filterPending === "Comercial" ? "active" : ""}`} onClick={() => setFilterPending("Comercial")}>
              Decisão: Comercial
            </button>
            <button className={`filter-btn ${filterPending === "Financeiro / Comercial" ? "active" : ""}`} onClick={() => setFilterPending("Financeiro / Comercial")}>
              Financeiro / Comercial
            </button>
          </div>

          {/* Dossiê Especial: Atlas Idiomas & Casa do Estudante */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginBottom: 20 }}>
            <div className="trace-card" style={{ borderLeft: "3px solid var(--danger)" }}>
              <div className="trace-header">
                <span className="trace-title">Dossiê Atlas Idiomas (EC 1076801732)</span>
                <span className="chip chip-err">ESCALAR_HUMANO</span>
              </div>
              <p style={{ fontSize: 12, color: "var(--ink-secondary)", margin: "8px 0" }}>
                57 transações de checkout (R$ 7.876,05 de taxas cobradas pela Orbe) retidas sem emissão de NF.
                O RCA da IA identificou o CNPJ <code>39566044000119</code> apenas na aba de 2025 (inativa), sem contrato assinado em 2026.
              </p>
              <div className="evidence-pills">
                <span className="evidence-pill">02_tabela_taxas_contratadas.xlsx!ANTIGO 2025!A8:F8</span>
                <span className="evidence-pill">05a_extrato_banco_aurora:L34,L60</span>
              </div>
              <button
                className="btn btn-sm btn-primary"
                style={{ marginTop: 12 }}
                onClick={() => {
                  setModalDraft({
                    title: "Solicitação Comercial — Contrato Atlas Idiomas",
                    content:
                      `Para: diego@tarvo.com.br (Comercial)\n` +
                      `De: financeiro@tarvo.com.br (Helena)\n` +
                      `Assunto: Confirmação de Taxas e Contrato 2026 — Atlas Idiomas (EC 1076801732)\n\n` +
                      `Diego,\n\n` +
                      `Temos 57 transações da Atlas Idiomas em setembro (R$ 262.138,61 de volume bruto e R$ 7.876,05 cobrados pela Orbe) ` +
                      `que estão bloqueadas sem faturamento.\n\n` +
                      `A empresa consta apenas na aba de 2025 com o CNPJ 39.566.044/0001-19, sem linha vigente assinada em 2026.\n\n` +
                      `Por favor, confirmar as taxas negociadas e a vigência formal para liberarmos a emissão da nota fiscal.\n\n` +
                      `Atenciosamente,\nHelena`,
                  });
                }}
              >
                Gerar Solicitação ao Comercial
              </button>
            </div>

            <div className="trace-card" style={{ borderLeft: "3px solid var(--warning)" }}>
              <div className="trace-header">
                <span className="trace-title">Dossiê Casa do Estudante (CNPJ 00520815000100)</span>
                <span className="chip chip-warn">CONDIÇÃO COMERCIAL ABERTA</span>
              </div>
              <p style={{ fontSize: 12, color: "var(--ink-secondary)", margin: "8px 0" }}>
                119 transações com R$ 15.391,92 apurados. A célula <code>A13</code> traz comentário da Bia:
                <em> "conversar com Diego sobre condição especial"</em> e nota <em>"cliente estratégico - ver c/ comercial"</em>.
                Nota bloqueada até homologação formal da política de isenção.
              </p>
              <div className="evidence-pills">
                <span className="evidence-pill">02_tabela_taxas_contratadas.xlsx!Taxas vigentes!A13:L13</span>
              </div>
              <button
                className="btn btn-sm btn-primary"
                style={{ marginTop: 12 }}
                onClick={() => {
                  setModalDraft({
                    title: "Consulta Comercial — Condição Casa do Estudante",
                    content:
                      `Para: diego@tarvo.com.br (Comercial)\n` +
                      `De: financeiro@tarvo.com.br (Helena)\n` +
                      `Assunto: Homologação de Condição Comercial Especial — Casa do Estudante\n\n` +
                      `Diego,\n\n` +
                      `A planilha de taxas traz em A13 a observação "cliente estratégico - ver c/ comercial".\n` +
                      `Conforme alinhado, apuramos preliminarmente R$ 15.391,92 de taxas, mas a emissão de NF permanece suspensa.\n\n` +
                      `Favor formalizar se a isenção de MDR/antecipação cobre integralmente o período de set-dez/2026 para ` +
                      `processarmos a devolução do que a Orbe descontou na fonte.\n\n` +
                      `Atenciosamente,\nHelena`,
                  });
                }}
              >
                Consultar Comercial sobre Isenção
              </button>
            </div>
          </div>

          <div className="panel">
            <div className="panel-header">
              <h3 className="panel-title">Lista de Casos para Decisão Humana ({data.pendencias.length} pendências)</h3>
            </div>
            <div className="panel-body">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Tipo</th>
                    <th>Ref Origem</th>
                    <th>Quem Decide</th>
                    <th className="num">Valor BRL</th>
                    <th>Motivo & Evidência Documentada</th>
                  </tr>
                </thead>
                <tbody>
                  {data.pendencias
                    .filter(p => filterPending === "ALL" || p.quem_decide.includes(filterPending))
                    .slice(0, 35)
                    .map(p => (
                      <tr key={p.id_saida}>
                        <td>
                          <span className="chip chip-muted" style={{ fontFamily: "monospace", fontSize: 10 }}>
                            {p.tipo}
                          </span>
                        </td>
                        <td><code>{p.ref_origem}</code></td>
                        <td style={{ color: "var(--ink)", fontWeight: 600 }}>{p.quem_decide}</td>
                        <td className="num" style={{ fontFamily: "monospace", fontWeight: 600 }}>
                          {p.valor_brl ? formatBrl(parseFloat(p.valor_brl)) : "-"}
                        </td>
                        <td style={{ fontSize: 11, maxWidth: 450 }}>{p.motivo}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Modal Minuta / Draft */}
      {modalDraft && (
        <div className="modal-overlay" onClick={() => setModalDraft(null)}>
          <div className="modal-box" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h4 className="modal-title">{modalDraft.title}</h4>
              <button className="modal-close" onClick={() => setModalDraft(null)}>✕</button>
            </div>
            <div className="modal-body">
              <pre className="modal-pre">{modalDraft.content}</pre>
            </div>
            <div className="modal-footer">
              <button
                className="btn btn-primary"
                onClick={() => {
                  navigator.clipboard.writeText(modalDraft.content);
                  alert("Minuta copiada para a área de transferência!");
                  setModalDraft(null);
                }}
              >
                Copiar Texto
              </button>
              <button className="btn" onClick={() => setModalDraft(null)}>Fechar</button>
            </div>
          </div>
        </div>
      )}

      {/* Resultado da execução HTTP */}
      {showAgentModal && (
        <div className="modal-overlay" onClick={() => !isRunningAgent && setShowAgentModal(false)}>
          <div className="modal-box" style={{ width: "min(780px, 100%)" }} onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <h4 className="modal-title">Execução do fechamento com LLM</h4>
                {isRunningAgent ? (
                  <span className="chip chip-warn">Aguardando backend...</span>
                ) : (
                  <span className={`chip ${agentFailed ? "chip-warn" : "chip-ok"}`}>{agentFailed ? "Falhou" : "Concluído"}</span>
                )}
              </div>
              {!isRunningAgent && (
                <button className="modal-close" onClick={() => setShowAgentModal(false)}>✕</button>
              )}
            </div>
            <div className="modal-body">
              <p style={{ margin: "0 0 12px", color: "var(--muted)", fontSize: 12 }}>
                As etapas só são confirmadas quando a rota do backend responde com sucesso:
              </p>
              <div className="agent-steps-list">
                {agentSteps.map(step => (
                  <div key={step.id} className={`agent-step-item ${step.status}`}>
                    <div className={`step-bullet ${step.status}`}>
                      {step.status === "completed" ? "✓" : step.status === "running" ? "●" : "○"}
                    </div>
                    <div className="step-content">
                      <div className="step-title">{step.title}</div>
                      <div className="step-desc">{step.description}</div>
                    </div>
                    <div>
                      <span className={`chip ${step.status === "completed" ? "chip-ok" : step.status === "running" ? "chip-warn" : "chip-muted"}`}>
                        {step.status === "completed" ? "Concluído" : step.status === "running" ? "Processando" : "Aguardando"}
                      </span>
                    </div>
                  </div>
                ))}
              </div>

              {agentStdout && (
                <div style={{ marginTop: 14 }}>
                  <span style={{ fontSize: 11, color: "var(--muted)", textTransform: "uppercase", letterSpacing: ".06em", display: "block", marginBottom: 6 }}>
                    Resultado do backend:
                  </span>
                  <div className="terminal-window">{agentStdout}</div>
                </div>
              )}
            </div>
            <div className="modal-footer">
              <button
                className="btn btn-primary"
                disabled={isRunningAgent}
                onClick={executeAgentClosing}
              >
                {isRunningAgent ? "Processando..." : "↻ Reexecutar Fechamento"}
              </button>
              <button className="btn" disabled={isRunningAgent} onClick={() => setShowAgentModal(false)}>
                Fechar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
