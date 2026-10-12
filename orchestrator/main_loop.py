"""Laço principal: roda o pipeline inteiro sozinho, no relógio.

    python -m orchestrator.main_loop --uma-vez   # um ciclo e sai
    python -m orchestrator.main_loop             # fica rodando

As etapas, em ordem, cada uma no próprio ritmo:

    sourcing   a cada 6 h     descobre vídeos em alta
    pipeline   a cada 1 h     baixa, transcreve, escolhe os trechos
    editing    a cada 1 h     renderiza os clips
    publish    a cada 15 min  agenda o que falta e publica o que venceu
    analytics  1x por dia     (etapa 7)

**Falha em uma etapa não derruba o laço.** Cada uma roda no próprio
try/except: yt-dlp, ffmpeg, Whisper e três APIs diferentes levantam
hierarquias sem nada em comum, e um canal que saiu do ar não pode impedir que
os clips já renderizados sejam publicados. O erro fica no log com o nome da
etapa e a execução continua.

Duas formas de rodar, e a escolha não é indiferente:

* `--uma-vez` faz um ciclo e sai. É a forma para agendador do sistema (Task
  Scheduler, cron), onde quem garante que o processo volta é o SO. Não precisa
  do APScheduler instalado.
* Sem argumento, fica de pé com o APScheduler. Mais simples de começar, mas se
  o processo morrer ninguém o levanta — o que num pipeline que publica em
  horário marcado significa perder a janela sem aviso.
"""
import argparse
import logging
import sys
import time
from datetime import datetime

import settings
from db import repositorio
from editing import editar
from orchestrator import fila as fila_mod
from orchestrator import status as status_mod
from orchestrator import trava as trava_mod
from pipeline import processar
from publish import preflight
from publish import publicar
from sourcing import canais as canais_mod
from sourcing import descobrir
from sourcing import youtube as youtube_sourcing

log = logging.getLogger(__name__)


# --- as etapas ----------------------------------------------------------------

def etapa_sourcing(conn):
    """Varre os canais monitorados."""
    lista = canais_mod.carregar()
    if not lista:
        log.warning("Nenhum canal ativo em %s.", settings.CANAIS_PATH)
        return {}
    cliente = youtube_sourcing.construir_cliente()
    return descobrir.varrer(conn, cliente, lista)


def etapa_pipeline(conn):
    """Termina o que está em curso e só inicia vídeo novo se a fila está baixa.

    Com FILA_MINIMA_CLIPS=0 a guarda some e vale o comportamento antigo
    (processar toda a fila pendente).
    """
    if not settings.FILA_MINIMA_CLIPS:
        return processar.processar_fila(conn)
    return fila_mod.reabastecer(conn)


def etapa_editing(conn):
    resultado = editar.renderizar_fila(conn)
    _limpar_fontes(conn)
    return resultado


def _limpar_fontes(conn):
    """Apaga o vídeo-fonte dos podcasts prontos. Só com a flag ligada.

    Nunca derruba a etapa de edição: falha de limpeza é aviso, e o render já
    aconteceu.
    """
    if not settings.LIMPAR_FONTE_APOS_CLIPS:
        return {}
    try:
        from pipeline import limpeza
        resultado = limpeza.limpar(conn, simular=False)
    except Exception as e:
        log.warning("Limpeza do vídeo-fonte falhou: %s", e)
        return {}
    liberado = sum(r["bytes"] for r in resultado.values())
    if liberado:
        log.info("Limpeza: %.1f MB liberados em %d vídeo(s).",
                 liberado / 1e6, len(resultado))
    return resultado


def etapa_publish(conn):
    """Agenda o que falta e publica o que venceu, nesta ordem.

    Agendar primeiro de propósito: um clip renderizado agora pode ter horário
    ainda hoje, e inverter faria ele esperar o ciclo seguinte sem motivo.
    """
    agendadas = publicar.agendar_pendentes(conn)
    processadas = publicar.processar_vencidas(conn)
    return {"agendadas": agendadas, "processadas": processadas}


def etapa_analytics(conn):
    """Mede o que foi publicado e recalibra a seleção com o resultado.

    Medir ANTES de recalibrar, no mesmo ciclo: recalibrar sobre o histórico de
    ontem desperdiçaria um dia inteiro de medição que já está disponível.

    As duas metades são independentes de propósito — se a coleta falhar (API
    fora, token vencido), a recalibração ainda roda sobre o que já foi medido
    antes, que continua sendo dado válido.
    """
    from analytics import coletar, recalibrate

    medidos = {}
    try:
        medidos = coletar.coletar(conn)
    except Exception as e:
        log.warning("Coleta de métricas falhou: %s", e)

    return {"medidos": medidos, "recalibracao": recalibrate.recalibrar(conn)}


# (nome, função, nome do setting com o intervalo em minutos)
ETAPAS = (
    ("sourcing", etapa_sourcing, "INTERVALO_SOURCING_MIN"),
    ("pipeline", etapa_pipeline, "INTERVALO_PIPELINE_MIN"),
    ("editing", etapa_editing, "INTERVALO_EDITING_MIN"),
    ("publish", etapa_publish, "INTERVALO_PUBLISH_MIN"),
)


# --- execução -----------------------------------------------------------------

# Último resultado de cada etapa, para o ops/status.json. Estado de
# observação, não de controle: a fonte de verdade do trabalho é o clips.db.
_ESTADO = {"inicio": None, "fim": None, "etapas": {}}


def _escrever_status(conn):
    """Atualiza o ops/status.json. Nunca levanta: status é acessório."""
    try:
        dados = status_mod.montar(
            conn, etapas=dict(_ESTADO["etapas"]), inicio=_ESTADO["inicio"],
            fim=_ESTADO["fim"])
        status_mod.escrever(dados)
        for alerta in dados["alertas"]:
            log.warning("ALERTA: %s", alerta)
    except Exception as e:
        log.warning("Status não atualizado: %s", e)


def rodar_etapa(conn, nome, funcao):
    """Roda uma etapa isolada. Devolve (ok, resultado_ou_erro).

    O except é amplo de propósito — ver a docstring do módulo. O que importa é
    que NADA escape: uma exceção que suba mata o laço, e laço morto às 2 da
    manhã só é descoberto quando alguém nota que parou de sair post.
    """
    inicio = time.monotonic()
    try:
        resultado = funcao(conn)
    except Exception as e:
        log.exception("Etapa %s falhou: %s", nome, e)
        _ESTADO["etapas"][nome] = {"ok": False, "erro": str(e)[:300]}
        return False, e
    duracao = time.monotonic() - inicio
    log.info("Etapa %s concluída em %.1fs: %s", nome, duracao,
             resultado or "nada a fazer")
    _ESTADO["etapas"][nome] = {"ok": True, "erro": None,
                               "duracao_s": round(duracao, 1)}
    return True, resultado


def ciclo(conn, etapas=None):
    """Um ciclo completo, na ordem do pipeline. Devolve {etapa: ok}."""
    etapas = etapas or ETAPAS
    log.info("--- ciclo iniciado %s ---",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    resultados = {}
    _ESTADO["inicio"] = datetime.now()
    _ESTADO["etapas"] = {}
    for nome, funcao, _intervalo in etapas:
        ok, _resultado = rodar_etapa(conn, nome, funcao)
        resultados[nome] = ok
    _ESTADO["fim"] = datetime.now()
    _escrever_status(conn)
    return resultados


def scheduler_fuso():
    from publish import scheduler
    return scheduler.fuso_confere()


def avisar_do_estado(conn):
    """Diz, uma vez por execução, em que modo o laço está rodando.

    Vale um aviso alto: a diferença entre sombra e publicação real não aparece
    em lugar nenhum da saída normal, e é a diferença entre um arquivo em disco
    e um post público que não dá para desfazer.
    """
    if not settings.AUTO_PUBLISH:
        log.info("AUTO_PUBLISH=false — o laço roda inteiro e NADA é publicado.")
        return
    if preflight.parada_de_emergencia_ativa():
        log.warning("PARADA DE EMERGÊNCIA ativa (%s): nada será publicado.",
                    settings.ARQUIVO_PARAR_PUBLICACAO)
        return

    problemas = preflight.verificar(conn)
    if preflight.bloqueios(problemas):
        log.warning("PUBLICAÇÃO REAL ligada, mas há impedimentos:\n%s",
                    preflight.formatar(problemas))
    else:
        log.warning("PUBLICAÇÃO REAL ligada — os posts vão ao ar de verdade.")


def montar_agenda(agendador, conn, etapas=None):
    """Monta os gatilhos do APScheduler."""
    etapas = etapas or ETAPAS
    for nome, funcao, chave in etapas:
        minutos = getattr(settings, chave)
        agendador.add_job(
            rodar_etapa, "interval", minutes=minutos,
            args=[conn, nome, funcao], id=nome, name=nome,
            # Se uma execução atrasar, o APScheduler pode acumular disparos.
            # coalesce junta os atrasados num só, e max_instances=1 impede duas
            # cópias da mesma etapa mexendo na fila ao mesmo tempo.
            coalesce=True, max_instances=1, misfire_grace_time=minutos * 60,
        )
        log.info("Etapa %s agendada a cada %d min.", nome, minutos)

    hora, _, minuto = str(settings.HORARIO_ANALYTICS).partition(":")
    agendador.add_job(
        rodar_etapa, "cron", hour=int(hora), minute=int(minuto or 0),
        args=[conn, "analytics", etapa_analytics], id="analytics",
        coalesce=True, max_instances=1,
    )
    return agendador


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Roda o pipeline inteiro no relógio."
    )
    parser.add_argument(
        "--uma-vez", action="store_true",
        help="um ciclo completo e sai (para cron / Task Scheduler)",
    )
    parser.add_argument(
        "--verificar", action="store_true",
        help="só confere se a publicação real pode ser ligada",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = repositorio.conectar()
    trava = trava_mod.Trava(settings.TRAVA_PATH)
    try:
        if args.verificar:
            problemas = preflight.verificar(conn)
            print(preflight.formatar(problemas))
            return 1 if preflight.bloqueios(problemas) else 0

        avisar_do_estado(conn)
        ok_fuso, local_h, conf_h = scheduler_fuso()
        if not ok_fuso:
            log.warning(
                "O relógio do SO (UTC%+g) não está em %s (UTC%+g): HORARIOS_"
                "PADRAO será lido no fuso errado. Ajuste o fuso da máquina.",
                local_h, settings.FUSO_AGENDAMENTO, conf_h)

        try:
            trava.adquirir()
        except trava_mod.OutraInstancia as e:
            # 0 de propósito: sob timer do systemd/launchd, dois disparos
            # sobrepostos são esperados e não são falha.
            log.warning("%s — saindo sem fazer nada.", e)
            return 0

        if args.uma_vez:
            resultados = ciclo(conn)
            falhas = [n for n, ok in resultados.items() if not ok]
            if falhas:
                log.warning("Etapas com falha: %s", ", ".join(falhas))
            # Sai 0 mesmo com etapa falhada: sob cron, código diferente de zero
            # vira alerta a cada hora, e falha de UMA etapa é esperada (canal
            # fora do ar, API instável). O log é o canal certo para isso.
            return 0

        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError:
            log.error(
                "APScheduler não instalado. Use --uma-vez sob o agendador do "
                "sistema, ou pip install -r requirements.txt"
            )
            return 2

        agendador = montar_agenda(BlockingScheduler(), conn)
        log.info("Laço de pé. Ctrl-C para parar.")
        try:
            agendador.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Laço encerrado.")
        return 0
    finally:
        trava.liberar()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
