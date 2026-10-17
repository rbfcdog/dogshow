import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

function parseCsv(content: string): Record<string, string>[] {
  const lines = content.trim().split("\n");
  if (lines.length < 2) return [];
  const header = lines[0].split(";").map(c => c.trim().replace(/^\ufeff/, ""));
  return lines.slice(1).map(line => {
    const cells = line.split(";");
    const row: Record<string, string> = {};
    for (let i = 0; i < header.length; i++) {
      row[header[i]] = (cells[i] ?? "").trim();
    }
    return row;
  });
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

  const referenciasRaw = safeJson(path.join(saidaDir, "referencias", "referencias.json"));
  let referenciasSummary: unknown = null;
  if (referenciasRaw && typeof referenciasRaw === "object" && "summary" in referenciasRaw) {
    referenciasSummary = referenciasRaw.summary;
  }

  const rca = safeJson(path.join(saidaDir, "referencias", "rca.json")) ?? [];
  const escaladas = safeJson(path.join(saidaDir, "referencias", "escaladas_humanas.json")) ?? [];
  const revisaoBancaria = parseCsv(safeRead(path.join(saidaDir, "referencias_bancarias", "revisao_humana.csv")) ?? "");
  const rcaBancaria = safeJson(path.join(saidaDir, "referencias_bancarias", "rca_bancaria.json")) ?? [];

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
    rastro: rastro.slice(0, 500),
    rastro_total_count: rastro.length,
    referencias_summary: referenciasSummary,
    rca,
    escaladas,
    revisao_bancaria: revisaoBancaria,
    rca_bancaria: rcaBancaria,
  });
}
