"""Renderiza os trechos selecionados como clips verticais prontos.

Ponto de entrada da etapa 3:

    python -m editing.editar

Pega os clips com status 'selecionado' que ainda não têm arquivo, do melhor
score para o pior, e produz um .mp4 vertical com legenda queimada, hook de
abertura e watermark — tudo pelo template_config.json.

"Ainda não tem arquivo" é a ausência de linha em `renders`, não um status novo:
o veredito da seleção e o artefato são coisas diferentes, e refazer um render
com template novo é apagar o arquivo e a linha, não reabrir uma máquina de
estados.

Falha em um clip não derruba os outros — a mesma política do pipeline.
"""
import argparse
import logging
import os
import sys

import settings
from db import repositorio
from editing import capa as capa_mod
from editing import efeitos as efeitos_mod
from editing import layout as layout_mod
from editing import legendas as legendas_mod
from editing import render as render_mod
from editing import sfx as sfx_mod
from editing import template as template_mod
from pipeline import transcribe as transcribe_mod

log = logging.getLogger(__name__)


class ClipSemFonte(Exception):
    """Faltam vídeo ou transcrição para renderizar este clip."""


def _carregar_fontes(linha, carregar_transcricao):
    video_path = linha["video_path"] or ""
    if not video_path:
        raise ClipSemFonte(
            "sem vídeo baixado (linha de `midia` ausente ou vazia) — "
            "rode o pipeline antes"
        )
    if not os.path.exists(video_path):
        raise ClipSemFonte(f"vídeo-fonte sumiu do disco: {video_path}")

    caminho_transcricao = linha["transcricao_path"] or ""
    if not caminho_transcricao:
        # Sem transcrição o clip ainda é renderizável — sai sem legenda, com o
        # hook e a watermark. Melhor um clip mudo de legenda do que nenhum.
        log.warning("Clip %s sem transcrição: render sem legenda.", linha["id"])
        return video_path, {"segmentos": []}
    return video_path, carregar_transcricao(caminho_transcricao)


def renderizar_clip(conn, linha, config, biblioteca=None, carregar_transcricao=None,
                    renderizar=None, destino_dir=None, planejar_filler=None,
                    gerar_capa=None):
    """Produz o arquivo de um clip e registra o artefato. Devolve o caminho."""
    carregar_transcricao = carregar_transcricao or transcribe_mod.carregar
    renderizar = renderizar or render_mod.renderizar

    video_path, transcricao = _carregar_fontes(linha, carregar_transcricao)
    inicio, fim = float(linha["inicio_s"]), float(linha["fim_s"])
    duracao = fim - inicio

    # As palavras são extraídas UMA vez e servem às duas coisas: a legenda
    # word-by-word e a detecção de palavra-chave do SFX. Recalcular para o
    # segundo uso convidaria os dois a divergirem.
    palavras = legendas_mod.palavras_do_trecho(transcricao, inicio, fim)
    # Layout A/B. A variante é decidida (ou recuperada, se o clip já teve uma)
    # ANTES do .ass porque a posição da legenda muda no split.
    variante = layout_mod.variante_do_clip(conn, linha["id"], config)
    config_layout = layout_mod.config_do_layout(config, variante)
    ass_texto = legendas_mod.gerar_ass(
        config_layout, palavras, hook_text=linha["hook_text"] or "",
        duracao_s=duracao,
    )

    picos = repositorio.picos_do_clip(conn, linha["id"])
    sons = sfx_mod.planejar(
        config, duracao,
        picos=picos,
        palavras=palavras,
        biblioteca=biblioteca if biblioteca is not None else {},
    )
    nome = render_mod.nome_base(linha["video_id"], linha["id"])

    # `filler` só vai para o render quando o layout é split: o caminho "full"
    # chama o renderizador exatamente como antes do A/B existir.
    extras = {}
    plano = layout_mod.filler_do_layout(
        config, variante, duracao, clip_id=linha["id"],
        planejar=planejar_filler,
    )
    if plano:
        extras["filler"] = plano
    # Efeitos de edição: só entram na chamada quando alguma flag está ligada,
    # então com a seção desligada o renderizador é chamado como antes.
    extras_efeitos = {}
    if efeitos_mod.algum_ativo(config):
        sons = list(sons) + efeitos_mod.plano_pad(
            config, layout_mod.separar(variante)[1] or layout_mod.FULL)
        extras_efeitos["picos"] = list(picos or [])

    # Capa: gerada ANTES do render porque o quadro de título entra no vídeo.
    # Falha aqui nunca derruba o clip — a capa é acessório.
    capa_info = _gerar_capa(config, linha, video_path, inicio, fim, nome,
                            destino_dir, gerar_capa)
    frame = config.get("capa", {}).get("frame_inicial", {})
    if capa_info and frame.get("ativo"):
        extras_efeitos["capa_frame"] = {
            "caminho": capa_info["vertical"],
            "duracao_s": float(frame["duracao_s"]),
        }
    if extras_efeitos:
        extras["efeitos"] = extras_efeitos

    caminho = renderizar(
        config_layout, video_path, inicio, fim, ass_texto, nome,
        destino_dir=destino_dir, sons=sons, **extras,
    )
    repositorio.registrar_render(
        conn, linha["id"], caminho,
        template_versao=str(config.get("versao", "")),
        duracao_s=duracao,
        variante=variante,
        capa_path=(capa_info or {}).get("vertical", ""),
    )
    return caminho


def _gerar_capa(config, linha, video_path, inicio, fim, nome, destino_dir,
                gerar_capa=None):
    """Dict da capa, ou None (desligada ou falhou — nunca levanta)."""
    cfg = config.get("capa", {})
    if not (cfg.get("ativa") or cfg.get("frame_inicial", {}).get("ativo")):
        return None
    gerar_capa = gerar_capa or capa_mod.gerar
    destino = destino_dir or settings.RENDER_DIR
    try:
        os.makedirs(destino, exist_ok=True)
        return gerar_capa(config, video_path, inicio, fim,
                          capa_mod.titulo_do_clip(linha), destino, nome)
    except Exception as e:
        log.warning("Capa do clip %s falhou (%s); segue sem ela.",
                    linha["id"], e)
        return None


def renderizar_fila(conn, limite=None, config=None, biblioteca=None, **injecoes):
    """Renderiza a fila de edição. Devolve {'ok': n, 'falha': n}."""
    limite = settings.EDITING_MAX_CLIPS if limite is None else limite
    config = config if config is not None else template_mod.carregar()

    # A biblioteca é conferida UMA vez, antes do primeiro render: um arquivo
    # de efeito faltando é erro de configuração, e descobri-lo clip a clip
    # produziria a fila inteira em 'falha' com a mesma mensagem repetida.
    if biblioteca is None:
        biblioteca = sfx_mod.carregar_biblioteca(config)
    # Idem para o pad ambiente de cada categoria que a fila pode sortear.
    categorias = {layout_mod.separar(v)[1] or layout_mod.FULL
                  for v in layout_mod.variantes_disponiveis(config)}
    efeitos_mod.verificar_pads(config, categorias)
    if biblioteca:
        log.info("Efeitos carregados: %s.", ", ".join(sorted(biblioteca)))

    pendentes = repositorio.clips_para_renderizar(conn, limite=limite)
    log.info("%d clips pendentes de render (template versão %s).",
             len(pendentes), config.get("versao"))

    contagem = {"ok": 0, "falha": 0}
    for linha in pendentes:
        try:
            renderizar_clip(conn, linha, config, biblioteca=biblioteca, **injecoes)
        except Exception as e:
            # Amplo pelo mesmo motivo do pipeline: ffmpeg, disco e transcrição
            # corrompida levantam hierarquias sem nada em comum, e nenhuma
            # delas justifica parar a fila.
            log.warning("Falha ao renderizar o clip %s: %s", linha["id"], e)
            contagem["falha"] += 1
            continue
        contagem["ok"] += 1
    return contagem


def _resumo(conn, contagem, config):
    linhas = [
        "",
        "--- render ---",
        f"  renderizados  {contagem['ok']}",
        f"  falhas        {contagem['falha']}",
        f"  (template versão {config.get('versao')}, "
        f"{config['saida']['largura']}x{config['saida']['altura']}, "
        f"reframe {config['reframe']['modo']}, "
        f"layout {', '.join(sorted(layout_mod.variantes_disponiveis(config)))}, "
        f"sfx {'ligado' if config['sfx'].get('ativo') else 'desligado'})",
    ]

    pendentes = repositorio.clips_para_renderizar(conn)
    linhas += ["", f"--- fila de edição: {len(pendentes)} pendentes ---"]
    for clip in pendentes[:10]:
        linhas.append(
            f"  {clip['score_final']:>5.2f}  {clip['video_id']:<12} "
            f"{(clip['hook_text'] or clip['motivo'])[:48]}"
        )
    if not settings.AUTO_PUBLISH:
        linhas += [
            "",
            "AUTO_PUBLISH=false: os clips ficam em disco e nada é publicado.",
        ]
    return "\n".join(linhas)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Renderiza os trechos selecionados como clips verticais."
    )
    parser.add_argument(
        "--limite", type=int, default=None,
        help=f"clips por execução (padrão: {settings.EDITING_MAX_CLIPS})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = template_mod.carregar()
    except template_mod.TemplateInvalido as e:
        log.error("%s", e)
        return 2

    conn = repositorio.conectar()
    try:
        contagem = renderizar_fila(conn, limite=args.limite, config=config)
    except (sfx_mod.ErroSFX, efeitos_mod.ErroEfeitos) as e:
        # Erro de configuração, não de um clip: abortar é melhor do que
        # renderizar a fila inteira sem os efeitos que o template mandava.
        log.error("%s", e)
        return 2
    else:
        print(_resumo(conn, contagem, config))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
