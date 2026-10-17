import { NextResponse } from "next/server";


export async function POST() {
  const backendUrl = process.env.BACKEND_URL || "http://127.0.0.1:8000";
  const url = `${backendUrl}/api/v1/fechamento/executar?competencia=2026-09`;

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: AbortSignal.timeout(600000),
    });

    if (!response.ok) {
      const errorText = await response.text();
      return NextResponse.json({
        success: false,
        error: `A rota do backend retornou HTTP ${response.status}: ${errorText}`,
      }, { status: response.status });
    }
    const payload = await response.json();
    if (!payload.success || !payload.summary_text || !payload.model) {
      throw new Error("Backend não confirmou a execução com LLM.");
    }
    return NextResponse.json({
      success: true,
      stdout: payload.summary_text,
      data: payload,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Erro desconhecido";
    return NextResponse.json({
      success: false,
      error: `Não foi possível conectar à porta do backend (${backendUrl}): ${message}`,
    }, { status: 502 });
  }
}
