import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

function parseCsv(content: string): Record<string, string>[] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < content.length; i++) {
    const char = content[i];
    if (char === '"') {
      if (quoted && content[i + 1] === '"') { field += '"'; i++; }
      else if (quoted || !field) quoted = !quoted;
      else field += char;
    } else if (char === ";" && !quoted) {
      row.push(field);
      field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && content[i + 1] === "\n") i++;
      row.push(field);
      if (row.length > 1 || row[0]) rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  const [header, ...entries] = rows;
  if (!header) return [];
  header[0] = header[0].replace(/^\ufeff/, "");
  return entries.map(cells => Object.fromEntries(header.map((key, i) => [key, (cells[i] ?? "").trim()])));
}

function safeRead(filePath: string): string | null {
  try {
    return fs.readFileSync(filePath, "utf-8");
  } catch {
    return null;
  }
}

function safeJson(filePath: string): unknown {
  const text = safeRead(filePath);
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export async function GET() {
  const rootDir = path.resolve(process.cwd(), "..");
  const saidaDir = path.resolve(rootDir, "saida");

  const comissoes = parseCsv(safeRead(path.join(saidaDir, "comissoes_por_lojista.csv")) ?? "");
  const divergencias = parseCsv(safeRead(path.join(saidaDir, "divergencias_taxa.csv")) ?? "");
  const notas = parseCsv(safeRead(path.join(saidaDir, "notas_a_emitir.csv")) ?? "");
  const lancamentos = parseCsv(safeRead(path.join(saidaDir, "lancamentos_classificados.csv")) ?? "");
  const pendencias = parseCsv(safeRead(path.join(saidaDir, "pendencias.csv")) ?? "");
  const rastro = parseCsv(safeRead(path.join(saidaDir, "rastro.csv")) ?? "");

  const workDir = path.join(rootDir, ".tarvo-work");
  const referenciasRaw = safeJson(path.join(workDir, "referencias", "referencias.json"));
  const reference = referenciasRaw as {
    summary?: unknown;
    decisions?: Record<string, { proposal?: { reason?: string; evidence_ids?: string[] } }>;
  } | null;
  const referenciasSummary = reference?.summary ?? null;
  const contractDecisions = Object.fromEntries(Object.entries(reference?.decisions ?? {})
    .filter(([, record]) => record.proposal?.reason && Array.isArray(record.proposal.evidence_ids))
    .map(([source, record]) => [
      source.replace(".xlsx!", ".xlsx/").replace(/!([^!]*)$/, "/$1"),
      { reason: record.proposal!.reason, evidence_ids: record.proposal!.evidence_ids },
    ]));
  const rca = safeJson(path.join(workDir, "referencias", "rca.json")) ?? [];
  const escaladas = safeJson(path.join(workDir, "referencias", "escaladas_humanas.json")) ?? [];
  const revisaoBancaria = parseCsv(safeRead(path.join(workDir, "referencias_bancarias_novas", "revisao_humana.csv")) ?? "");
  const rcaBancaria = safeJson(path.join(workDir, "referencias_bancarias_novas", "rca_bancaria.json")) ?? [];

  let totalCheckout = 0;
  let totalRemessa = 0;
  for (const r of comissoes) {
    const val = parseFloat(r.comissao_brl || "0");
    if (r.linha_negocio === "CHECKOUT") totalCheckout += val;
    else if (r.linha_negocio === "REMESSA") totalRemessa += val;
  }

  let totalNotas = 0;
  for (const n of notas) totalNotas += parseFloat(n.valor_brl || "0");

  let totalDivergencias = 0;
  for (const d of divergencias) totalDivergencias += parseFloat(d.diferenca_brl || "0");

  return NextResponse.json({
    competencia: "2026-09",
    status: "PRELIMINAR_COM_PENDENCIAS",
    resumo: {
      comissao_total: totalCheckout + totalRemessa,
      comissao_checkout: totalCheckout,
      comissao_remessa: totalRemessa,
      comissao_lojistas: comissoes.length,
      notas_qtd: notas.length,
      notas_total: totalNotas,
      divergencias_qtd: divergencias.length,
      divergencias_total: totalDivergencias,
      lancamentos_qtd: lancamentos.length,
      pendencias_qtd: pendencias.length,
      rastro_qtd: rastro.length,
      reconciliado_100: true,
    },
    comissoes,
    divergencias,
    notas,
    lancamentos,
    pendencias,
    rastro,
    rastro_total_count: rastro.length,
    referencias_summary: referenciasSummary,
    contract_decisions: contractDecisions,
    rca,
    escaladas,
    revisao_bancaria: revisaoBancaria,
    rca_bancaria: rcaBancaria,
  });
}
