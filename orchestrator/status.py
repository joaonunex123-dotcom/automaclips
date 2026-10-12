"""ops/status.json: o estado do orquestrador num arquivo que máquina lê.

Existe para o mini PC: um monitor (cron de `jq`, Uptime Kuma, um script) lê
este arquivo e avisa quando algo está errado, sem ninguém abrir o log. Escrito
de forma atômica (arquivo temporário + rename) para um leitor nunca ver JSON
pela metade.

Campos:
  atualizado_em      quando este arquivo foi escrito (local, ISO)
  ultima_execucao    início, fim e o resultado de cada etapa
  fila               clips prontos x limiar, agendados, vídeos descobertos
  quota_youtube      usada/limite do dia (Pacífico), uploads que ainda cabem
  espaco_livre_gb    do disco dos downloads, e o mínimo configurado
  token_youtube      vencimento e dias restantes (alerta quando perto)
  alertas            lista de textos; vazia = tudo certo
"""
import json
import logging
import os
from datetime import date, datetime

import settings

log = logging.getLogger(__name__)


def token_youtube(hoje=None, caminho=None):
    """Situação do token OAuth do YouTube, sem ler o conteúdo do arquivo."""
    hoje = hoje or date.today()
    caminho = caminho or settings.YOUTUBE_OAUTH_TOKEN
    info = {"arquivo_existe": os.path.exists(caminho), "vence_em": None,
            "dias_restantes": None, "alerta": False, "aviso": ""}
    if not info["arquivo_existe"]:
        info.update(alerta=True, aviso="sem token do YouTube: rode "
                    "`python -m publish.publicar --autorizar`")
        return info
    texto = (settings.YOUTUBE_TOKEN_VENCE_EM or "").strip()
    if not texto:
        info["aviso"] = ("vencimento desconhecido; informe "
                         "YOUTUBE_TOKEN_VENCE_EM=AAAA-MM-DD no .env")
        return info
    try:
        vence = date.fromisoformat(texto)
    except ValueError:
        info.update(alerta=True,
                    aviso=f"YOUTUBE_TOKEN_VENCE_EM inválido: {texto!r}")
        return info
    dias = (vence - hoje).days
    info.update(vence_em=vence.isoformat(), dias_restantes=dias)
    if dias < 0:
        info.update(alerta=True, aviso=f"token VENCIDO há {-dias} dia(s)")
    elif dias <= settings.TOKEN_ALERTA_DIAS:
        info.update(alerta=True, aviso=f"token vence em {dias} dia(s)")
    return info


def montar(conn, etapas=None, inicio=None, fim=None, agora=None):
    """O dict do status.json. `etapas` = {nome: {"ok": bool, "erro": str|None}}."""
    from orchestrator import fila
    from pipeline import limpeza
    from publish import quota, scheduler

    agora = agora or datetime.now()
    prontos = fila.clips_prontos(conn)
    limiar = settings.FILA_MINIMA_CLIPS
    livre = limpeza.espaco_livre_gb()
    minimo = settings.LIMPEZA_ESPACO_MIN_GB
    usada = quota.usada(conn)
    limite = settings.YOUTUBE_QUOTA_DIARIA
    token = token_youtube(agora.date())
    fuso_ok, local_h, conf_h = scheduler.fuso_confere()
    gasto = repositorio_custo(conn)

    alertas = []
    if limiar and prontos < limiar:
        alertas.append(f"fila baixa: {prontos} clips prontos (< {limiar})")
    if minimo and livre < minimo:
        alertas.append(f"pouco disco: {livre:.1f} GB livres (< {minimo:g})")
    if token["alerta"]:
        alertas.append(token["aviso"])
    if not fuso_ok:
        alertas.append(
            f"fuso do SO (UTC{local_h:+g}) difere de "
            f"{settings.FUSO_AGENDAMENTO} (UTC{conf_h:+g}): horários deslocados")
    if settings.ORCAMENTO_USD and gasto >= settings.ORCAMENTO_USD:
        alertas.append(f"orçamento estourado: US$ {gasto:.2f}")
    for nome, r in (etapas or {}).items():
        if not r.get("ok"):
            alertas.append(f"etapa {nome} falhou: {r.get('erro') or '?'}")

    return {
        "atualizado_em": agora.isoformat(timespec="seconds"),
        "ultima_execucao": {
            "inicio": inicio.isoformat(timespec="seconds") if inicio else None,
            "fim": fim.isoformat(timespec="seconds") if fim else None,
            "etapas": etapas or {},
        },
        "modo": {
            "auto_publish": bool(settings.AUTO_PUBLISH),
            "limpar_fonte": bool(settings.LIMPAR_FONTE_APOS_CLIPS),
            "privacidade_youtube": settings.YOUTUBE_PRIVACIDADE,
        },
        "fila": {
            "prontos": prontos, "minimo": limiar,
            "agendados": fila.clips_agendados(conn),
            "videos_descobertos": fila.videos_descobertos(conn),
        },
        "cadencia": {
            "posts_por_dia": settings.posts_por_dia(settings.PLATAFORMA_YOUTUBE),
            "horarios": list(settings.HORARIOS_PADRAO),
            "fuso": settings.FUSO_AGENDAMENTO, "fuso_do_so_confere": fuso_ok,
        },
        "quota_youtube": {
            "usada": usada, "limite": limite, "restante": max(0, limite - usada),
            "margem": settings.YOUTUBE_QUOTA_MARGEM,
            "uploads_restantes": quota.uploads_restantes(conn),
        },
        "espaco_livre_gb": round(livre, 1),
        "espaco_minimo_gb": minimo,
        "custo_usd": round(gasto, 4),
        "orcamento_usd": settings.ORCAMENTO_USD,
        "token_youtube": token,
        "alertas": alertas,
    }


def repositorio_custo(conn):
    from db import repositorio
    return float(repositorio.custo_acumulado(conn))


def escrever(dados, caminho=None):
    """Grava de forma atômica. Nunca levanta: status é acessório."""
    caminho = caminho or settings.STATUS_PATH
    try:
        os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
        tmp = f"{caminho}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, caminho)
    except OSError as e:
        log.warning("Não consegui escrever %s: %s", caminho, e)
        return None
    return caminho
