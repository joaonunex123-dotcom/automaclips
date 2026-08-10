"""Varredura de sourcing: canais monitorados -> vídeos pontuados na fila.

Ponto de entrada da etapa 1. Roda sozinho:

    python -m sourcing.descobrir

e, a partir da etapa 6, é chamado pelo orchestrator a cada 6 h.

Divisão de responsabilidade — é o que mantém tudo testável sem rede:
    sourcing/youtube.py    fala com a API e devolve dicts
    sourcing/score.py      a fórmula, função pura
    db/repositorio.py      grava
    este arquivo           decide o STATUS de cada vídeo e orquestra
"""
import argparse
import logging
import sys

import settings
from db import repositorio
from sourcing import canais as canais_mod
from sourcing import score as score_mod
from sourcing import youtube

log = logging.getLogger(__name__)


def classificar(video, pontuacao, threshold=None):
    """Status de um vídeo recém-pontuado, e o motivo em texto.

    A ordem dos testes importa: duração e idade são propriedades do vídeo e
    valem independentemente de tração, então descartam antes de o score
    entrar na conversa. Um vídeo de 12 h com score altíssimo continua sendo
    caro demais para transcrever.
    """
    threshold = settings.SCORE_THRESHOLD if threshold is None else threshold

    duracao = video.get("duracao_s", 0)
    if duracao < settings.DURACAO_MINIMA_S:
        # Inclui duracao_s == 0, que é como youtube.duracao_para_segundos
        # devolve live em andamento e duração ilegível.
        return repositorio.STATUS_IGNORADO, f"duração {duracao}s abaixo do mínimo"
    if duracao > settings.DURACAO_MAXIMA_S:
        return repositorio.STATUS_IGNORADO, f"duração {duracao}s acima do máximo"

    if pontuacao.idade_horas < 0:
        # Estreia agendada, ou relógio desalinhado. Pontuar isso produziria um
        # score sem significado a partir de uma idade negativa.
        return repositorio.STATUS_IGNORADO, "publicação no futuro"
    if pontuacao.idade_horas > settings.IDADE_MAXIMA_HORAS:
        return repositorio.STATUS_IGNORADO, (
            f"{pontuacao.idade_horas:.1f}h de idade, fora da janela"
        )

    if pontuacao.score < threshold:
        return repositorio.STATUS_ABAIXO_DO_LIMIAR, (
            f"score {pontuacao.score:.1f} < {threshold:.1f}"
        )
    return repositorio.STATUS_DESCOBERTO, f"score {pontuacao.score:.1f}"


def processar_videos(conn, videos, agora=None, threshold=None):
    """Pontua e grava cada vídeo. Devolve {status: quantidade} desta varredura.

    A leitura da observação anterior e a gravação da nova ficam na MESMA
    transação: sem isso, duas varreduras simultâneas leriam o mesmo ponto de
    partida e cada uma gravaria o ganho cheio, dobrando o score de um vídeo
    que não ganhou nada a mais.
    """
    contagem = {}
    for video in videos:
        with repositorio.escrita(conn):
            existente = repositorio.buscar_video(
                conn, video["plataforma"], video["video_id"]
            )
            anteriores = (
                repositorio.views_da_ultima_observacao(conn, existente["id"])
                if existente
                else None
            )
            pontuacao = score_mod.calcular(
                video.get("views", 0), anteriores, video["publicado_em"], agora=agora
            )
            status, motivo = classificar(video, pontuacao, threshold=threshold)
            repositorio.registrar_observacao(
                conn,
                video,
                views=video.get("views", 0),
                ganho=pontuacao.ganho,
                score=pontuacao.score,
                status=status,
            )
        log.debug("%s [%s] %s — %s", video["video_id"], status, video.get("titulo", ""), motivo)
        contagem[status] = contagem.get(status, 0) + 1
    return contagem


def varrer(conn, cliente, lista_de_canais, max_por_canal=None, agora=None, threshold=None):
    """Coleta na API e processa. Separada de main() para os testes chamarem."""
    videos = youtube.coletar(cliente, lista_de_canais, max_por_canal=max_por_canal)
    log.info("%d vídeos coletados de %d canais.", len(videos), len(lista_de_canais))
    return processar_videos(conn, videos, agora=agora, threshold=threshold)


def _resumo(conn, contagem):
    linhas = [
        "",
        "--- varredura ---",
        f"  descobertos       {contagem.get(repositorio.STATUS_DESCOBERTO, 0)}",
        f"  abaixo do limiar  {contagem.get(repositorio.STATUS_ABAIXO_DO_LIMIAR, 0)}",
        f"  ignorados         {contagem.get(repositorio.STATUS_IGNORADO, 0)}",
        f"  (threshold: {settings.SCORE_THRESHOLD:.0f} views/h)",
        "",
        "--- fila acumulada ---",
    ]
    total = repositorio.contar_por_status(conn)
    for status in sorted(total):
        linhas.append(f"  {status:<18} {total[status]}")

    topo = repositorio.listar_por_status(conn, repositorio.STATUS_DESCOBERTO, limite=10)
    if topo:
        linhas.append("")
        linhas.append("--- topo da fila ---")
        for linha in topo:
            titulo = (linha["titulo"] or "")[:60]
            linhas.append(f"  {linha['score']:>9.1f} v/h  {titulo}")
    return "\n".join(linhas)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Varre os canais e alimenta a fila.")
    parser.add_argument(
        "--max-por-canal", type=int, default=None,
        help=f"uploads recentes olhados por canal (padrão: {settings.MAX_VIDEOS_POR_CANAL})",
    )
    parser.add_argument(
        "--threshold", type=float, default=None,
        help=f"corte do score em views/h (padrão: {settings.SCORE_THRESHOLD})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="loga vídeo a vídeo")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        lista_de_canais = canais_mod.carregar()
    except canais_mod.CanaisInvalidos as e:
        log.error("%s", e)
        return 2
    if not lista_de_canais:
        log.error("Nenhum canal ativo em %s.", settings.CANAIS_PATH)
        return 2

    try:
        cliente = youtube.construir_cliente()
    except youtube.ErroYouTube as e:
        log.error("%s", e)
        return 2

    conn = repositorio.conectar()
    try:
        contagem = varrer(
            conn, cliente, lista_de_canais,
            max_por_canal=args.max_por_canal, threshold=args.threshold,
        )
        print(_resumo(conn, contagem))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
