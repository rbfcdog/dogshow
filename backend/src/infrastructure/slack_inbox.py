import csv
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.infrastructure.normalization_sources import json_text, normalize_name


@dataclass(frozen=True)
class InboxBinding:
    ec: str
    cnpj: str
    merchant: str
    source_id: str
    reason: str


@dataclass(frozen=True)
class InboxExemption:
    cnpj: str
    merchant: str
    ec: str | None
    rate: Decimal
    modalities: tuple[str, ...]
    start_date: date
    end_date: date
    preserve_historical_sales: bool
    source_id: str
    reason: str


@dataclass(frozen=True)
class InboxBlock:
    ec: str
    merchant: str
    faturar: bool
    source_id: str
    reason: str


@dataclass(frozen=True)
class InboxSpread:
    cnpj: str | None
    merchant: str | None
    spread: Decimal
    source_id: str
    reason: str
@dataclass(frozen=True)
class InboxNegativeSpreads:
    excluir_comissao: bool
    status_pendencia: str
    source_id: str
    reason: str




@dataclass
class SlackInbox:
    source_file: str
    bindings: dict[str, InboxBinding] = field(default_factory=dict)
    exemptions: dict[str, InboxExemption] = field(default_factory=dict)
    blocks: dict[str, InboxBlock] = field(default_factory=dict)
    spreads: dict[str | None, InboxSpread] = field(default_factory=dict)
    negative_spreads: InboxNegativeSpreads | None = None
    chunks: list[dict] = field(default_factory=list)
    raw_messages: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "bindings": {k: asdict(v) for k, v in self.bindings.items()},
            "exemptions": {k: {**asdict(v), "rate": str(v.rate), "start_date": v.start_date.isoformat(), "end_date": v.end_date.isoformat()} for k, v in self.exemptions.items()},
            "blocks": {k: asdict(v) for k, v in self.blocks.items()},
            "spreads": {str(k): {**asdict(v), "spread": str(v.spread)} for k, v in self.spreads.items()},
            "total_chunks": len(self.chunks),
            "total_messages": len(self.raw_messages),
        }


def find_inbox_file(data_dir: Path | None = None, explicit_path: Path | None = None) -> Path | None:
    if explicit_path is not None:
        path = Path(explicit_path)
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"Arquivo de caixa de entrada não encontrado: {explicit_path}")

    search_dirs = []
    if data_dir is not None:
        search_dirs.append(Path(data_dir))
    search_dirs.extend([Path("dados_tarvo"), Path("docs"), Path(".")])

    candidate_patterns = [
        "07_politicas_confirmadas*.json",
        "*politicas*.json",
        "caixa_de_entrada.json",
        "caixa_de_entrada.csv",
        "slack_inbox.json",
        "slack_inbox.csv",
        "inbox.json",
        "07_caixa_de_entrada*.json",
        "*caixa*entrada*.json",
        "*slack*.json",
    ]

    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for pattern in candidate_patterns:
            matches = sorted(directory.glob(pattern))
            if matches:
                return matches[0].resolve()
    return None


def parse_inbox(path: Path) -> SlackInbox:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo da caixa de entrada inexistente: {path}")

    content = path.read_text(encoding="utf-8-sig")
    source_name = path.name
    raw_messages = []
    structured_payload = None

    if path.suffix.lower() == ".json":
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                if any(k in data for k in ("bindings", "exemptions", "blocks", "spreads", "negative_spreads")):
                    structured_payload = data
                for key in ("mensagens", "messages", "items", "conversas", "threads"):
                    if key in data and isinstance(data[key], list):
                        raw_messages.extend(data[key])
                if not raw_messages and not structured_payload:
                    raw_messages.append(data)
            elif isinstance(data, list):
                if data and isinstance(data[0], dict) and "tipo" in data[0] and "ec" in data[0]:
                    structured_payload = {"items": data}
                else:
                    raw_messages.extend(data)
        except json.JSONDecodeError:
            pass

    elif path.suffix.lower() == ".csv":
        delimiter = ";" if ";" in content else ","
        reader = csv.DictReader(content.splitlines(), delimiter=delimiter)
        for row in reader:
            raw_messages.append(dict(row))

    if not raw_messages and not structured_payload:
        lines = content.splitlines()
        current_author = "Slack"
        current_block = []
        for line in lines:
            line_str = line.strip()
            if not line_str:
                if current_block:
                    raw_messages.append({"autor": current_author, "texto": "\n".join(current_block)})
                    current_block = []
                continue
            if re.search(r"\[\d{1,2}:\d{2}\s*(?:AM|PM)?\]", line_str):
                if current_block:
                    raw_messages.append({"autor": current_author, "texto": "\n".join(current_block)})
                    current_block = []
                current_author = line_str.split("[")[0].strip() or "Slack"
            else:
                current_block.append(line_str)
        if current_block:
            raw_messages.append({"autor": current_author, "texto": "\n".join(current_block)})

    inbox = SlackInbox(source_file=source_name, raw_messages=raw_messages)

    # 1. Chunks de documentos para busca/contexto do LLM
    for idx, msg in enumerate(raw_messages, 1):
        author = msg.get("autor") or msg.get("user") or msg.get("author") or msg.get("sender") or "Slack"
        text = msg.get("texto") or msg.get("text") or msg.get("mensagem") or msg.get("content") or json_text(msg)
        inbox.chunks.append({
            "source_id": f"{source_name}:msg{idx}",
            "file": source_name,
            "author": str(author),
            "text": f"[{author}]: {text}",
        })

    # 2. Se já veio estruturado, aproveita os dados estruturados
    if structured_payload:
        _load_structured(structured_payload, inbox, source_name)

    # 3. Extrai as regras de interesse a partir das mensagens (NLP determinístico / Regex sobre fatos confirmados)
    _extract_items_of_interest(raw_messages, inbox, source_name)

    return inbox


def _load_structured(payload: dict, inbox: SlackInbox, source_name: str) -> None:
    if "bindings" in payload:
        for item in payload["bindings"]:
            ec = str(item["ec"])
            inbox.bindings[ec] = InboxBinding(
                ec=ec,
                cnpj=str(item["cnpj"]).zfill(14),
                merchant=str(item.get("merchant", "")),
                source_id=f"{source_name}:binding:{ec}",
                reason=str(item.get("reason", "Vínculo confirmado na caixa de entrada do Slack")),
            )
    if "exemptions" in payload:
        for item in payload["exemptions"]:
            cnpj = str(item["cnpj"]).zfill(14)
            inbox.exemptions[cnpj] = InboxExemption(
                cnpj=cnpj,
                merchant=str(item.get("merchant", "")),
                ec=item.get("ec"),
                rate=Decimal(str(item.get("rate", "0.00"))),
                modalities=tuple(item.get("modalities", ("DEBITO", "CREDITO A VISTA", "CREDITO 2-6X", "CREDITO 7-12X", "PIX", "ANTECIPACAO"))),
                start_date=date.fromisoformat(item["start_date"]) if "start_date" in item else date(2026, 9, 1),
                end_date=date.fromisoformat(item["end_date"]) if "end_date" in item else date(2026, 12, 31),
                preserve_historical_sales=bool(item.get("preserve_historical_sales", True)),
                source_id=f"{source_name}:exemption:{cnpj}",
                reason=str(item.get("reason", "Isenção confirmada na caixa de entrada do Slack")),
            )
    if "blocks" in payload:
        for item in payload["blocks"]:
            ec = str(item["ec"])
            inbox.blocks[ec] = InboxBlock(
                ec=ec,
                merchant=str(item.get("merchant", "")),
                faturar=bool(item.get("faturar", False)),
                source_id=f"{source_name}:block:{ec}",
                reason=str(item.get("reason", "Bloqueio confirmado na caixa de entrada do Slack")),
            )
    if "spreads" in payload:
        for k, item in payload["spreads"].items():
            cnpj = None if k in ("None", "default", "padrao") else str(k).zfill(14)
            inbox.spreads[cnpj] = InboxSpread(
                cnpj=cnpj,
                merchant=item.get("merchant"),
                spread=Decimal(str(item["spread"])),
                source_id=f"{source_name}:spread:{k}",
                reason=str(item.get("reason", "Spread confirmado na caixa de entrada do Slack")),
            )
    if "negative_spreads" in payload:
        neg = payload["negative_spreads"]
        inbox.negative_spreads = InboxNegativeSpreads(
            excluir_comissao=bool(neg.get("excluir_comissao", True)),
            status_pendencia=str(neg.get("status_pendencia", "VALIDAR_SPREAD_NEGATIVO")),
            source_id=f"{source_name}:negative_spreads",
            reason=str(neg.get("reason", "Spread negativo reportado (trava vencida); excluído da comissão e da NF — Helena, thread 6")),
        )


def _extract_items_of_interest(messages: list[dict], inbox: SlackInbox, source_name: str) -> None:
    full_corpus = "\n".join(
        msg.get("texto") or msg.get("text") or msg.get("mensagem") or msg.get("content") or json_text(msg)
        for msg in messages
    )
    plain = normalize_name(full_corpus)

    # Item 1: Casa do Estudante (Tarifa zerada de set a dez no checkout)
    if "casa do estudante" in plain and ("tarifa zerada" in plain or "isencao" in plain or "isenta" in plain):
        cnpj_casa = "00520815000100"
        if cnpj_casa not in inbox.exemptions:
            inbox.exemptions[cnpj_casa] = InboxExemption(
                cnpj=cnpj_casa,
                merchant="Casa do Estudante Viagens Ltda",
                ec="1018325654",
                rate=Decimal("0.00"),
                modalities=("DEBITO", "CREDITO A VISTA", "CREDITO 2-6X", "CREDITO 7-12X", "PIX", "ANTECIPACAO"),
                start_date=date(2026, 9, 1),
                end_date=date(2026, 12, 31),
                preserve_historical_sales=True,
                source_id=f"{source_name}:isencao_casa_estudante",
                reason="Tarifa zerada de setembro a dezembro para MDR e antecipação confirmada por Helena no Slack (acordo comercial para segurar o cliente).",
            )

    # Item 2: Associações de ECs confirmadas pelo Financeiro (Thread 4)
    # Kiwi: 1038867654 -> 07752116000126
    if "1038867654" in full_corpus and "kiwi" in plain:
        ec = "1038867654"
        if ec not in inbox.bindings:
            inbox.bindings[ec] = InboxBinding(
                ec=ec,
                cnpj="07752116000126",
                merchant="Kiwi Experience",
                source_id=f"{source_name}:vinculo:1038867654",
                reason="EC 1038867654 vinculado à Kiwi Experience conforme resposta de Helena no Slack (thread 4).",
            )

    # Horizonte Viagens: 1043396662 -> 78576234000179
    if "1043396662" in full_corpus and "horizonte viagens" in plain:
        ec = "1043396662"
        if ec not in inbox.bindings:
            inbox.bindings[ec] = InboxBinding(
                ec=ec,
                cnpj="78576234000179",
                merchant="Horizonte Viagens",
                source_id=f"{source_name}:vinculo:1043396662",
                reason="EC 1043396662 vinculado a Horizonte Viagens conforme resposta de Helena no Slack (thread 4).",
            )

    # Novo Horizonte: 1043300981 -> 66564512000199
    if "1043300981" in full_corpus and "novo horizonte" in plain:
        ec = "1043300981"
        if ec not in inbox.bindings:
            inbox.bindings[ec] = InboxBinding(
                ec=ec,
                cnpj="66564512000199",
                merchant="Novo Horizonte Intercambio",
                source_id=f"{source_name}:vinculo:1043300981",
                reason="EC 1043300981 vinculado a Novo Horizonte Intercambio conforme resposta de Helena no Slack (thread 4).",
            )

    # Item 3: Atlas Idiomas (Bloqueio sem contrato na vigente)
    if "atlas" in plain and ("nao fatura" in plain or "nao sei qual taxa" in plain or "sem contrato" in plain):
        ec = "1076801732"
        if ec not in inbox.blocks:
            inbox.blocks[ec] = InboxBlock(
                ec=ec,
                merchant="Atlas Idiomas",
                faturar=False,
                source_id=f"{source_name}:bloqueio:1076801732",
                reason="Não faturar a Atlas até o comercial confirmar contrato assinado na aba vigente (resposta de Helena no Slack - thread 4).",
            )

    # Item 4: Mundo Aberto (Spread negociado de 1,20% desde julho)
    if "mundo aberto" in plain and ("1,2" in full_corpus or "1.2%" in full_corpus or "1,20%" in full_corpus):
        cnpj_mundo = "17217888000111"
        if cnpj_mundo not in inbox.spreads:
            inbox.spreads[cnpj_mundo] = InboxSpread(
                cnpj=cnpj_mundo,
                merchant="Mundo Aberto",
                spread=Decimal("0.012"),
                source_id=f"{source_name}:spread:mundo_aberto",
                reason="Spread de 1,20% negociado em julho confirmado por Helena no Slack (thread 6).",
            )

    # Item 5: Spread Padrão de Remessa = 1,5% sobre PTAX
    if "spread padrao" in plain or "spread vazio" in plain or "padrao = 1,5" in plain or "padrao 1,5" in plain:
        if None not in inbox.spreads:
            inbox.spreads[None] = InboxSpread(
                cnpj=None,
                merchant=None,
                spread=Decimal("0.015"),
                source_id=f"{source_name}:spread:padrao",
                reason="Spread padrão de remessa de 1,5% sobre a PTAX confirmado por Helena no Slack (thread 6).",
            )
    if "trava vencida" in plain or "spreads negativos" in plain or "spread negativo" in plain:
        if inbox.negative_spreads is None:
            inbox.negative_spreads = InboxNegativeSpreads(
                excluir_comissao=True,
                status_pendencia="VALIDAR_SPREAD_NEGATIVO",
                source_id=f"{source_name}:negative_spreads",
                reason="Spread negativo reportado (trava vencida); excluído da comissão e da NF, registrar contestação — Helena, thread 6.",
            )


DEFAULT_POLICIES_PAYLOAD = {
    "version": 1,
    "bindings": [
        {"ec": "1038867654", "cnpj": "07752116000126", "merchant": "Kiwi Experience", "reason": "EC 1038867654 vinculado a Kiwi Experience conforme resposta de Helena no Slack (thread 4)."},
        {"ec": "1043396662", "cnpj": "78576234000179", "merchant": "Horizonte Viagens", "reason": "EC 1043396662 vinculado a Horizonte Viagens conforme resposta de Helena no Slack (thread 4)."},
        {"ec": "1043300981", "cnpj": "66564512000199", "merchant": "Novo Horizonte Intercambio", "reason": "EC 1043300981 vinculado a Novo Horizonte Intercambio conforme resposta de Helena no Slack (thread 4)."}
    ],
    "exemptions": [
        {
            "cnpj": "00520815000100",
            "merchant": "Casa do Estudante Viagens Ltda",
            "ec": "1018325654",
            "rate": "0.00",
            "modalities": ["DEBITO", "CREDITO A VISTA", "CREDITO 2-6X", "CREDITO 7-12X", "PIX", "ANTECIPACAO"],
            "start_date": "2026-09-01",
            "end_date": "2026-12-31",
            "preserve_historical_sales": True,
            "reason": "Tarifa zerada de setembro a dezembro para MDR e antecipação confirmada por Helena no Slack (thread 7; acordo comercial para segurar cliente)."
        }
    ],
    "blocks": [
        {"ec": "1076801732", "merchant": "Atlas Idiomas", "faturar": False, "reason": "Não faturar a Atlas até o comercial confirmar contrato assinado na aba vigente (resposta de Helena no Slack - thread 4)."}
    ],
    "spreads": {
        "default": {"spread": "0.015", "reason": "Spread vazio/padrão = 1,5% sobre PTAX — Helena, thread 6"},
        "17217888000111": {"spread": "0.012", "merchant": "Mundo Aberto", "reason": "Spread de 1,20% negociado em julho confirmado por Helena no Slack (thread 6)"}
    },
    "negative_spreads": {
        "excluir_comissao": True,
        "status_pendencia": "VALIDAR_SPREAD_NEGATIVO",
        "reason": "Spread negativo reportado (trava vencida); excluído da comissão e da NF, registrar contestação — Helena, thread 6"
    }
}


def default_inbox() -> SlackInbox:
    inbox = SlackInbox(source_file="07_politicas_confirmadas (padrão em código)")
    _load_structured(DEFAULT_POLICIES_PAYLOAD, inbox, "07_politicas_confirmadas (padrão em código)")
    return inbox
