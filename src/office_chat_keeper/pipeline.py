"""Encadeia as etapas: snapshot -> arquivo permanente -> exportação.

    origem (Office)  --cópia-->  snapshot  --leitura-->  arquivo  --> exportação
      só leitura                  foto bruta            memória       Markdown/JSON

Cada seta só anda para a direita. A exportação lê do ARQUIVO (que só cresce),
então uma fonte vazia ou recriada nunca "esvazia" o que já foi exportado.

Cada snapshot é ingerido uma única vez (a tabela ``ingested_snapshots`` lembra
quais já foram), do mais antigo para o mais novo, para que o histórico de
versões saia na ordem certa.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .archive import Alert, Archive, ConversationIngest
from .config import Config
from .conversation import load_conversations
from .export import EXTRA_DATABASES, ConversationExport, export_conversation, export_extras
from .reader import SnapshotReader
from .snapshot import SnapshotInfo, list_snapshots

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    source_id: str
    snapshot: str
    conversations: list[ConversationIngest] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extra_records: int = 0
    unreadable: int = 0
    error: str = ""

    def count(self, outcome: str) -> int:
        return sum(1 for c in self.conversations if c.outcome == outcome)


RESTORED_MARKER = "restored.json"


def _load_restored(cfg: Config) -> dict[str, str]:
    """Fontes restauradas pela ferramenta e ainda não vistas pelo backup: id -> quando."""
    try:
        data = json.loads((cfg.state_dir / RESTORED_MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_restored(cfg: Config, source_id: str, when: datetime) -> None:
    """Chamado pela restauração: o próximo snapshot desta fonte terá o banco 'recriado'."""
    marker = _load_restored(cfg)
    marker[source_id] = when.isoformat(timespec="seconds")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / RESTORED_MARKER).write_text(json.dumps(marker, indent=2), encoding="utf-8")


def ingest_snapshot(archive: Archive, snapshot: SnapshotInfo, *,
                    expect_recreated: bool = False) -> IngestReport:
    """Lê um snapshot e junta seu conteúdo ao arquivo permanente.

    A leitura (lenta) acontece antes; a gravação no arquivo é uma transação
    única: ou o snapshot entra inteiro, ou não entra nada.
    """
    source_id = snapshot.manifest.get("source_id") or snapshot.path.parent.name
    name = snapshot.path.name
    report = IngestReport(source_id, name)
    seen_at = snapshot.taken_at

    with SnapshotReader.from_snapshot(snapshot.path) as reader:
        conversations, report.warnings = load_conversations(reader)
        extras = []
        schema = reader.schema()
        for database in EXTRA_DATABASES:
            for store in schema.get(database, []):
                for record in reader.records(database, store):
                    if not record.deleted and record.value is not None:
                        extras.append((database, store, record.key, record.value))
        report.unreadable = len(reader.errors)

    # Só confiamos em "a conversa não está na fonte" se a foto é coerente e
    # tudo foi lido. Senão, uma falha de leitura pareceria uma perda.
    reliable = snapshot.stable and report.unreadable == 0
    if not reliable:
        report.warnings.append("snapshot instável ou com registros ilegíveis: "
                               "a verificação de conversas ausentes foi pulada")

    with archive.transaction():
        ids_before = archive.conversation_ids()
        for conv in conversations:
            outcome = archive.ingest_conversation(conv, source_id=source_id, snapshot=name,
                                                  seen_at=seen_at)
            report.conversations.append(outcome)
            if outcome.outcome == "rejected":
                alert = archive.add_alert(
                    Alert("conversation_shrunk",
                          f"Possível perda de histórico: a conversa {conv.id} chegou com "
                          f"{len(conv.messages)} mensagem(ns), sem {outcome.removed} das que o "
                          f"arquivo tem. A versão arquivada foi mantida.",
                          source_id, {"conversation_id": conv.id, "title": conv.title,
                                      "incoming_messages": len(conv.messages),
                                      "missing_messages": outcome.removed}),
                    f"shrunk:{conv.id}:{len(conv.messages)}:{outcome.removed}", seen_at)
                if alert:
                    report.alerts.append(alert)
        for database, store, key, value in extras:
            archive.store_extra_record(database, store, key, value, seen_at)
        report.extra_records = len(extras)
        report.alerts += archive.check_source(
            source_id=source_id, snapshot=name,
            db_created_at=snapshot.manifest.get("db_created_at"),
            present_ids={c.id for c in conversations}, ids_before=ids_before,
            seen_at=seen_at, check_absences=reliable, expect_recreated=expect_recreated)
        archive.mark_ingested(source_id, name, taken_at=snapshot.manifest.get("taken_at"),
                              db_created_at=snapshot.manifest.get("db_created_at"),
                              stable=snapshot.stable, conversations=len(conversations))

    log.info("arquivo: fonte=%s snapshot=%s conversas=%d novas=%d atualizadas=%d iguais=%d "
             "antigas=%d rejeitadas=%d extras=%d ilegiveis=%d alertas=%d",
             source_id, name, len(conversations), report.count("new"), report.count("updated"),
             report.count("unchanged"), report.count("older"), report.count("rejected"),
             report.extra_records, report.unreadable, len(report.alerts))
    for alert in report.alerts:
        log.warning("ALERTA %s: fonte=%s ids=%s", alert.kind, alert.source_id,
                    _alert_ids(alert))
    return report


def _alert_ids(alert: Alert) -> str:
    """Só ids no log (nunca títulos ou texto de conversa)."""
    details = alert.details
    ids = [c.get("id") for c in details.get("conversations", [])]
    ids += details.get("absent_ids", [])
    if details.get("conversation_id"):
        ids.append(details["conversation_id"])
    return ",".join(str(i) for i in ids[:20]) or "-"


def ingest_pending(cfg: Config, archive: Archive) -> list[IngestReport]:
    """Ingere todos os snapshots ainda não vistos, do mais antigo ao mais novo.

    Inclui fontes que já sumiram do disco: o que foi fotografado continua valendo.
    """
    reports: list[IngestReport] = []
    if not cfg.snapshots_dir.is_dir():
        return reports
    pending: list[SnapshotInfo] = []
    for source_dir in sorted(p for p in cfg.snapshots_dir.iterdir() if p.is_dir()):
        for snap in list_snapshots(source_dir):
            if not archive.is_ingested(source_dir.name, snap.path.name):
                pending.append(snap)
    pending.sort(key=lambda s: (s.taken_at, s.path.name))
    restored = _load_restored(cfg)
    for snap in pending:
        source_id = snap.path.parent.name
        expected = (source_id in restored
                    and snap.taken_at >= datetime.fromisoformat(restored[source_id]))
        try:
            reports.append(ingest_snapshot(archive, snap, expect_recreated=expected))
            if expected:
                del restored[source_id]
                (cfg.state_dir / RESTORED_MARKER).write_text(json.dumps(restored, indent=2),
                                                             encoding="utf-8")
        except Exception as exc:  # um snapshot ilegível não pode derrubar o backup
            source_id = snap.path.parent.name
            log.error("arquivo: leitura falhou fonte=%s snapshot=%s erro=%s",
                      source_id, snap.path.name, type(exc).__name__)
            # Registramos a falha para não repetir a tentativa a cada 15 minutos.
            archive.mark_ingested(source_id, snap.path.name,
                                  taken_at=snap.manifest.get("taken_at"),
                                  db_created_at=snap.manifest.get("db_created_at"),
                                  stable=snap.stable, conversations=None,
                                  error=type(exc).__name__)
            reports.append(IngestReport(source_id, snap.path.name, error=type(exc).__name__))
    return reports


def alert_if_no_source(cfg: Config, archive: Archive, sources_found: int) -> Alert | None:
    """Nenhuma fonte no disco, mas o arquivo tem conversas: possível perda."""
    key = "no-source"
    now = datetime.now(timezone.utc)
    if sources_found:
        with archive.transaction():
            archive.resolve_alerts("no_source_found", now, dedup_key=key)
        return None
    total = len(archive.conversation_ids())
    if not total:
        return None
    with archive.transaction():
        alert = archive.add_alert(
            Alert("no_source_found",
                  f"Possível perda de histórico: nenhuma fonte pivot.claude.ai foi encontrada, "
                  f"mas o arquivo tem {total} conversa(s). Nada foi apagado do arquivo.",
                  None, {"archived_conversations": total}), key, now)
    if alert:
        log.warning("ALERTA no_source_found: conversas_no_arquivo=%d", total)
    return alert


def export_changed(cfg: Config, archive: Archive, *, force: bool = False,
                   include_thinking: bool = False) -> tuple[list[ConversationExport], dict[str, int]]:
    """Exporta do arquivo as conversas que mudaram desde a última exportação."""
    results: list[ConversationExport] = []
    for conv_id in archive.pending_exports(force=force):
        conv = archive.load_conversation(conv_id)
        if conv is None:
            continue
        result = export_conversation(conv, cfg.exports_dir, include_thinking=include_thinking)
        results.append(result)
        if result.status != "kept-larger":
            with archive.transaction():
                archive.mark_exported(conv_id)
        else:
            log.warning("exportacao: conversa=%s menor que a ja exportada; mantida a antiga", conv_id)
    extras = export_extras(archive, cfg.exports_dir)
    if results:
        log.info("exportacao: conversas=%d gravadas=%d iguais=%d mantidas=%d", len(results),
                 sum(r.status == "written" for r in results),
                 sum(r.status == "unchanged" for r in results),
                 sum(r.status == "kept-larger" for r in results))
    return results, extras
