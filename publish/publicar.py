"""Agenda e publica os clips renderizados. Ponto de entrada da etapa 5.

    python -m publish.publicar

Faz duas coisas, nesta ordem:

1. **Agendar** — pega os clips com arquivo e ainda sem agendamento, gera o
   metadado (uma chamada ao Claude por clip, servindo as duas plataformas) e
   marca um horário livre para cada.
2. **Publicar** — pega os agendamentos cuja hora chegou e os processa.

O modo sombra vive no passo 2, e só nele: com `AUTO_PUBLISH=false` tudo
acontece de verdade — metadado gerado, horário atribuído, quota conferida — e
a publicação para na porta, marcada como 'simulado'. É o que permite olhar a
fila inteira, com os textos que realmente sairiam, antes de qualquer coisa ir
ao ar.

Ao ligar a publicação real (etapa 6), `--reagendar-simulados` devolve o que já
foi planejado para a fila em vez de reconstruir tudo: o metadado gerado (e
pago) continua valendo.
"""
import argparse
import json
import logging
import sys
from datetime import datetime

import settings
from db import repositorio
from pipeline import transcribe as transcribe_mod
from publish import instagram as instagram_mod
from publish import metadata as metadata_mod
from publish import quota
from publish import scheduler
from publish import youtube as youtube_mod

log = logging.getLogger(__name__)


class SemQuota(Exception):
    """O upload não cabe no que resta da quota do dia."""


# --- agendamento --------------------------------------------------------------

def clips_pendentes(conn, plataformas, limite=None):
    """Clips com arquivo, faltando agendamento em pelo menos uma plataforma.

    Agrupados por CLIP e não por plataforma para o metadado ser gerado uma vez
    só: gerar por plataforma dobraria o custo e ainda produziria um título e
    uma caption que não conversam entre si.
    """
    pendentes = {}
    for plataforma in plataformas:
        for linha in repositorio.clips_para_agendar(conn, plataforma, limite):
            entrada = pendentes.setdefault(
                linha["id"], {"linha": linha, "plataformas": []}
            )
            entrada["plataformas"].append(plataforma)

    ordenados = sorted(pendentes.values(),
                       key=lambda e: (-e["linha"]["score_final"], e["linha"]["id"]))
    return ordenados[:limite] if limite else ordenados


def _fala(linha, carregar_transcricao):
    caminho = linha["transcricao_path"] or ""
    if not caminho:
        return ""
    try:
        transcricao = carregar_transcricao(caminho)
    except Exception as e:
        # Metadado sem a fala sai pior, mas sai; derrubar o agendamento por
        # causa de um .json ilegível seria perder o clip inteiro.
        log.warning("Transcrição ilegível para o clip %s: %s", linha["id"], e)
        return ""
    return metadata_mod.fala_do_trecho(
        transcricao, float(linha["inicio_s"]), float(linha["fim_s"])
    )


def agendar_pendentes(conn, plataformas=None, limite=None, agora=None,
                      gerar=None, carregar_transcricao=None):
    """Gera metadado e marca horário. Devolve {plataforma: quantidade}."""
    plataformas = plataformas or settings.PLATAFORMAS
    limite = settings.PUBLISH_MAX_POR_EXECUCAO if limite is None else limite
    agora = agora or datetime.now()
    gerar = gerar or metadata_mod.gerar
    carregar_transcricao = carregar_transcricao or transcribe_mod.carregar

    pendentes = clips_pendentes(conn, plataformas, limite)
    if not pendentes:
        return {}
    log.info("%d clips para agendar.", len(pendentes))

    # Os horários de cada plataforma são reservados de uma vez: pedir um por
    # vez faria cada consulta ignorar os que a própria execução acabou de
    # escolher e ainda não gravou.
    slots = {
        plataforma: scheduler.proximos_slots(
            conn, plataforma, len(pendentes), agora=agora
        )
        for plataforma in plataformas
    }

    contagem = {}
    for indice, entrada in enumerate(pendentes):
        linha = entrada["linha"]
        try:
            meta = gerar(
                dict(linha), fala=_fala(linha, carregar_transcricao),
                titulo_fonte=linha["titulo_fonte"], canal=linha["canal_nome"],
                conn=conn,
            )
        except Exception as e:
            log.warning("Metadado falhou para o clip %s: %s", linha["id"], e)
            continue

        for plataforma in entrada["plataformas"]:
            disponiveis = slots.get(plataforma) or []
            if indice >= len(disponiveis):
                log.info(
                    "Sem horário livre em %s para o clip %s dentro de %d dias; "
                    "fica para a próxima execução.",
                    plataforma, linha["id"], settings.AGENDAMENTO_MAX_DIAS,
                )
                continue

            if plataforma == settings.PLATAFORMA_YOUTUBE:
                texto = metadata_mod.para_youtube(meta, linha["url_fonte"])
                titulo, descricao = texto["titulo"], texto["descricao"]
            else:
                titulo = meta["titulo"]
                descricao = metadata_mod.para_instagram(meta)["caption"]

            repositorio.agendar_publicacao(
                conn, linha["id"], plataforma, disponiveis[indice],
                titulo=titulo, descricao=descricao, hashtags=meta["hashtags"],
            )
            contagem[plataforma] = contagem.get(plataforma, 0) + 1
            log.info("Clip %s agendado em %s para %s.",
                     linha["id"], plataforma, disponiveis[indice])
    return contagem


# --- publicação ---------------------------------------------------------------

def _enviar_youtube(conn, linha, agora=None):
    if not quota.cabe(conn, agora=agora):
        raise SemQuota(
            f"upload custa {settings.YOUTUBE_CUSTO_UPLOAD} e sobrou "
            f"{quota.restante(conn, agora=agora)} — {quota.resumo(conn, agora=agora)}"
        )
    cliente = youtube_mod.construir_cliente()
    video_id, url = youtube_mod.enviar(
        cliente, linha["render_path"], linha["titulo"], linha["descricao"],
        tags=json.loads(linha["hashtags"] or "[]"),
    )
    # Registrada DEPOIS do sucesso: quota, ao contrário de dinheiro, não volta,
    # e reservar por uma chamada que falhou custa um upload que caberia.
    quota.registrar(conn, agora=agora)
    return video_id, url


def _enviar_instagram(conn, linha, agora=None):
    return instagram_mod.publicar(
        conn, linha["render_path"], linha["descricao"], agora=agora
    )


ENVIADORES = {
    settings.PLATAFORMA_YOUTUBE: _enviar_youtube,
    settings.PLATAFORMA_INSTAGRAM: _enviar_instagram,
}


def processar_vencidas(conn, agora=None, limite=None, enviadores=None,
                       auto_publish=None):
    """Publica (ou simula) o que já venceu. Devolve {status: quantidade}."""
    agora = agora or datetime.now()
    limite = settings.PUBLISH_MAX_POR_EXECUCAO if limite is None else limite
    enviadores = ENVIADORES if enviadores is None else enviadores
    if auto_publish is None:
        auto_publish = settings.AUTO_PUBLISH

    vencidas = repositorio.publicacoes_vencidas(
        conn, agora=scheduler.formatar(agora), limite=limite
    )
    log.info("%d publicações venceram.", len(vencidas))

    contagem = {}
    for linha in vencidas:
        plataforma = linha["plataforma"]

        if not auto_publish:
            # Modo sombra: tudo pronto, nada enviado. A quota é conferida
            # mesmo assim, para a simulação ser fiel — mas não é consumida,
            # porque nenhuma chamada aconteceu.
            if (plataforma == settings.PLATAFORMA_YOUTUBE
                    and not quota.cabe(conn, agora=agora)):
                log.warning("Clip %s caberia hoje? Não: %s",
                            linha["clip_id"], quota.resumo(conn, agora=agora))
            repositorio.marcar_publicacao(conn, linha["id"],
                                          repositorio.PUB_SIMULADO)
            contagem[repositorio.PUB_SIMULADO] = (
                contagem.get(repositorio.PUB_SIMULADO, 0) + 1
            )
            log.info("[sombra] %s: %r ficaria pronto para %s.",
                     plataforma, linha["titulo"], linha["agendado_para"])
            continue

        if not (linha["render_path"] or ""):
            repositorio.marcar_publicacao(
                conn, linha["id"], repositorio.PUB_FALHA,
                erro="render ausente — o arquivo do clip sumiu",
            )
            contagem[repositorio.PUB_FALHA] = contagem.get(repositorio.PUB_FALHA, 0) + 1
            continue

        enviar = enviadores.get(plataforma)
        if enviar is None:
            repositorio.marcar_publicacao(
                conn, linha["id"], repositorio.PUB_FALHA,
                erro=f"plataforma sem enviador: {plataforma}",
            )
            contagem[repositorio.PUB_FALHA] = contagem.get(repositorio.PUB_FALHA, 0) + 1
            continue

        try:
            id_externo, url = enviar(conn, linha, agora=agora)
        except Exception as e:
            # Amplo pelo mesmo motivo do resto do pipeline: os SDKs levantam
            # hierarquias sem nada em comum, e um post que falhou não pode
            # derrubar os outros do dia.
            log.warning("Publicação %s falhou: %s", linha["id"], e)
            repositorio.marcar_publicacao(
                conn, linha["id"], repositorio.PUB_FALHA, erro=str(e)
            )
            contagem[repositorio.PUB_FALHA] = contagem.get(repositorio.PUB_FALHA, 0) + 1
            continue

        repositorio.marcar_publicacao(
            conn, linha["id"], repositorio.PUB_PUBLICADO,
            id_externo=id_externo, url=url,
        )
        contagem[repositorio.PUB_PUBLICADO] = (
            contagem.get(repositorio.PUB_PUBLICADO, 0) + 1
        )
        log.info("Publicado em %s: %s", plataforma, url)
    return contagem


# --- resumo e CLI -------------------------------------------------------------

def _resumo(conn, agendadas, processadas, agora=None):
    linhas = ["", "--- publicação ---"]
    if agendadas:
        for plataforma in sorted(agendadas):
            linhas.append(f"  agendados em {plataforma:<10} {agendadas[plataforma]}")
    else:
        linhas.append("  agendados          0")
    for status in sorted(processadas):
        linhas.append(f"  {status:<18} {processadas[status]}")

    linhas += ["", "--- fila por plataforma ---"]
    total = repositorio.contar_publicacoes(conn)
    for (plataforma, status) in sorted(total):
        linhas.append(f"  {plataforma:<10} {status:<12} {total[(plataforma, status)]}")

    if settings.PLATAFORMA_YOUTUBE in settings.PLATAFORMAS:
        linhas += ["", f"--- quota YouTube ---", f"  {quota.resumo(conn, agora=agora)}"]

    proximas = repositorio.proximas_publicacoes(conn)
    if proximas:
        linhas += ["", "--- agenda ---"]
        for pub in proximas:
            linhas.append(
                f"  {pub['agendado_para']}  {pub['plataforma']:<10} "
                f"{(pub['titulo'] or '')[:48]}"
            )

    if not settings.AUTO_PUBLISH:
        linhas += [
            "",
            "AUTO_PUBLISH=false: nada foi publicado. Os textos acima são os que",
            "sairiam. Para valer, ligue AUTO_PUBLISH e rode com",
            "--reagendar-simulados.",
        ]
    return "\n".join(linhas)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Agenda e publica os clips renderizados."
    )
    parser.add_argument("--limite", type=int, default=None,
                        help=f"itens por execução (padrão: {settings.PUBLISH_MAX_POR_EXECUCAO})")
    parser.add_argument("--so-agendar", action="store_true",
                        help="gera metadado e marca horário, sem processar vencidas")
    parser.add_argument("--so-publicar", action="store_true",
                        help="processa o que venceu, sem agendar nada novo")
    parser.add_argument("--reagendar-simulados", action="store_true",
                        help="devolve as publicações do modo sombra para a fila")
    parser.add_argument("--autorizar", action="store_true",
                        help="fluxo OAuth do YouTube (interativo, roda uma vez)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.autorizar:
        try:
            youtube_mod.autorizar()
        except youtube_mod.ErroYouTubeUpload as e:
            log.error("%s", e)
            return 2
        return 0

    conn = repositorio.conectar()
    try:
        if args.reagendar_simulados:
            devolvidas = repositorio.reagendar_simulados(conn)
            log.info("%d publicações voltaram para 'agendado'.", devolvidas)

        agendadas = {} if args.so_publicar else agendar_pendentes(
            conn, limite=args.limite
        )
        processadas = {} if args.so_agendar else processar_vencidas(
            conn, limite=args.limite
        )
        print(_resumo(conn, agendadas, processadas))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
