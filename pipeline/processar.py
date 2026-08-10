"""Processa a fila: download -> transcrição -> highlight_detect -> seleção.

Ponto de entrada da etapa 2:

    python -m pipeline.processar

Cada etapa concluída grava seu status no banco antes da seguinte começar. Isso
é o que torna a execução **retomável**: o download de um vídeo de duas horas
não é refeito porque o Whisper morreu depois dele, e uma falha na chamada ao
Claude não custa a transcrição inteira de novo. A alternativa — processar tudo
e gravar no fim — transformaria qualquer interrupção em trabalho perdido.

Falha em um vídeo não derruba os outros: o vídeo vai para 'falha' com o motivo
na coluna `erro` e a execução continua. Uma live sem áudio na fila não pode
segurar a fila.
"""
import argparse
import logging
import sys

import settings
from db import repositorio
from pipeline import download as download_mod
from pipeline import energia as energia_mod
from pipeline import highlight_detect
from pipeline import select_clips
from pipeline import transcribe as transcribe_mod

log = logging.getLogger(__name__)

# Estados de onde um vídeo ainda tem trabalho pela frente, na ordem do
# pipeline. 'falha' fica de fora: um vídeo que já quebrou seria retentado a
# cada execução, para sempre, gastando download e API no mesmo erro — quem
# quiser retentar passa --retentar.
ESTADOS_PENDENTES = (
    repositorio.STATUS_DESCOBERTO,
    repositorio.STATUS_BAIXADO,
    repositorio.STATUS_TRANSCRITO,
)


def fila_pendente(conn, limite=None, retentar=False):
    """Vídeos a processar, do melhor score para o pior."""
    estados = list(ESTADOS_PENDENTES)
    if retentar:
        estados.append(repositorio.STATUS_FALHA)
    marcadores = ", ".join("?" * len(estados))
    sql = (
        f"SELECT * FROM fila_clips WHERE status IN ({marcadores})"
        " ORDER BY score DESC, id"
    )
    parametros = list(estados)
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()


def _garantir_midia(conn, linha, baixar):
    """Passo 1. Reaproveita o que já estiver baixado."""
    atual = repositorio.midia(conn, linha["id"])
    if atual and atual["video_path"] and atual["audio_path"]:
        log.info("%s já baixado.", linha["video_id"])
        return atual

    log.info("Baixando %s...", linha["video_id"])
    resultado = baixar(linha["video_id"])
    repositorio.registrar_midia(
        conn, linha["id"],
        video_path=resultado["video_path"],
        audio_path=resultado["audio_path"],
        # A duração da API pode divergir da do arquivo; vale a do arquivo, que
        # é o que será cortado. Zero significa desconhecida — o ajuste de
        # duração em select_clips trata isso como "sem limite superior".
        duracao_real_s=resultado["duracao_real_s"] or linha["duracao_s"],
        baixado_em=_agora(conn),
    )
    repositorio.definir_status(conn, linha["id"], repositorio.STATUS_BAIXADO)
    return repositorio.midia(conn, linha["id"])


def _garantir_transcricao(conn, linha, midia, transcrever):
    """Passo 2. Reaproveita o .json se já existir."""
    if midia["transcricao_path"]:
        log.info("%s já transcrito.", linha["video_id"])
        return transcribe_mod.carregar(midia["transcricao_path"])

    log.info("Transcrevendo %s...", linha["video_id"])
    caminho, transcricao = transcrever(midia["audio_path"], linha["video_id"])
    repositorio.registrar_midia(
        conn, linha["id"],
        transcricao_path=caminho,
        transcrito_em=_agora(conn),
    )
    repositorio.definir_status(conn, linha["id"], repositorio.STATUS_TRANSCRITO)
    return transcricao


def _analisar(conn, linha, midia, transcricao, detectar, picos_do_audio):
    """Passo 3: Claude + energia + seleção, gravados numa transação."""
    texto = transcribe_mod.texto_com_timestamps(transcricao)
    trechos = detectar(texto)

    picos = picos_do_audio(midia["audio_path"])
    duracao = midia["duracao_real_s"] or transcricao.get("duracao_s") or 0
    avaliados = select_clips.selecionar(trechos, picos=picos, duracao_video=duracao)

    repositorio.registrar_clips(conn, linha["id"], avaliados)
    selecionados = sum(
        1 for c in avaliados if c["status"] == repositorio.CLIP_SELECIONADO
    )
    repositorio.definir_status(
        conn, linha["id"],
        repositorio.STATUS_ANALISADO if selecionados else repositorio.STATUS_SEM_CLIPS,
    )
    return selecionados


def _agora(conn):
    return conn.execute("SELECT datetime('now', 'localtime')").fetchone()[0]


def processar_video(conn, linha, baixar=None, transcrever=None, detectar=None,
                    picos_do_audio=None):
    """Leva um vídeo do estado em que está até 'analisado'.

    As quatro dependências são injetáveis para os testes rodarem sem rede,
    sem ffmpeg, sem Whisper e sem chave de API.
    """
    baixar = baixar or download_mod.baixar
    transcrever = transcrever or transcribe_mod.transcrever_para_arquivo
    detectar = detectar or highlight_detect.detectar
    picos_do_audio = picos_do_audio or energia_mod.picos_do_audio

    midia = _garantir_midia(conn, linha, baixar)
    transcricao = _garantir_transcricao(conn, linha, midia, transcrever)
    # Relê: _garantir_transcricao acabou de gravar transcricao_path.
    midia = repositorio.midia(conn, linha["id"])
    return _analisar(conn, linha, midia, transcricao, detectar, picos_do_audio)


def processar_fila(conn, limite=None, retentar=False, **injecoes):
    """Processa a fila inteira. Devolve {status_final: quantidade}."""
    limite = settings.PIPELINE_MAX_VIDEOS if limite is None else limite
    pendentes = fila_pendente(conn, limite=limite, retentar=retentar)
    log.info("%d vídeos pendentes.", len(pendentes))

    contagem = {}
    for linha in pendentes:
        try:
            selecionados = processar_video(conn, linha, **injecoes)
        except Exception as e:
            # Amplo de propósito: yt-dlp, ffmpeg, Whisper e o SDK da Anthropic
            # levantam hierarquias de exceção sem nada em comum, e nenhuma
            # delas justifica derrubar a fila. O motivo fica na linha.
            log.warning("Falha em %s: %s", linha["video_id"], e)
            repositorio.definir_status(
                conn, linha["id"], repositorio.STATUS_FALHA, erro=str(e)
            )
            contagem[repositorio.STATUS_FALHA] = (
                contagem.get(repositorio.STATUS_FALHA, 0) + 1
            )
            continue

        final = (
            repositorio.STATUS_ANALISADO if selecionados
            else repositorio.STATUS_SEM_CLIPS
        )
        contagem[final] = contagem.get(final, 0) + 1
        log.info("%s: %d clips selecionados.", linha["video_id"], selecionados)
    return contagem


def _resumo(conn, contagem):
    linhas = [
        "",
        "--- pipeline ---",
        f"  analisados  {contagem.get(repositorio.STATUS_ANALISADO, 0)}",
        f"  sem clips   {contagem.get(repositorio.STATUS_SEM_CLIPS, 0)}",
        f"  falhas      {contagem.get(repositorio.STATUS_FALHA, 0)}",
        "",
        "--- fila ---",
    ]
    total = repositorio.contar_por_status(conn)
    for status in sorted(total):
        linhas.append(f"  {status:<18} {total[status]}")

    topo = repositorio.listar_clips(conn, limite=10)
    if topo:
        linhas += ["", "--- topo da fila de edição ---"]
        for clip in topo:
            linhas.append(
                f"  {clip['score_final']:>5.2f}  "
                f"{clip['inicio_s']:>7.1f}-{clip['fim_s']:<7.1f}  "
                f"{(clip['hook_text'] or clip['motivo'])[:52]}"
            )
    return "\n".join(linhas)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Processa a fila de vídeos descobertos até a seleção de trechos."
    )
    parser.add_argument(
        "--limite", type=int, default=None,
        help=f"vídeos por execução (padrão: {settings.PIPELINE_MAX_VIDEOS})",
    )
    parser.add_argument(
        "--retentar", action="store_true",
        help="inclui os vídeos que estão em 'falha'",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = repositorio.conectar()
    try:
        contagem = processar_fila(conn, limite=args.limite, retentar=args.retentar)
        print(_resumo(conn, contagem))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
