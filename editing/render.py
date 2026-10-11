"""Monta e executa o comando do ffmpeg que produz o clip vertical.

O módulo é dividido em duas metades por um motivo prático: a montagem do
filtergraph é **função pura de string**, e só `renderizar` chama o ffmpeg. Um
filtergraph errado é caro de descobrir (um render de 60 s por tentativa) e
barato de travar em teste — então tudo que decide o formato do comando é
testável sem o binário instalado.

Duas escolhas que evitam armadilhas conhecidas:

* **`-filter_complex` sempre**, mesmo no modo `corte`, que caberia num `-vf`.
  Ter um caminho só, com saída rotulada e `-map` explícito, elimina a classe
  de bug em que mudar o modo do template muda a forma do comando.

* **O ffmpeg roda com `cwd` no diretório do .ass**, referenciando o arquivo
  pelo nome. O filtro `ass` interpreta `\\` e `:` do caminho como sintaxe de
  filtergraph, então um caminho absoluto do Windows (`C:\\Users\\...`) precisa
  de um escape triplo que quebra de forma diferente em cada versão. Trocar o
  diretório de trabalho não precisa de escape nenhum.
"""
import logging
import os
import re
import subprocess

import settings

log = logging.getLogger(__name__)


class ErroRender(Exception):
    """Falha ao montar ou executar o render."""


# O nome do .ass entra cru no filtergraph, então só permitimos caracteres que
# não têm significado lá dentro.
_SEGURO = re.compile(r"[^A-Za-z0-9_.-]")


def nome_base(video_id, clip_id):
    """Nome de arquivo seguro para filtergraph e para os três sistemas."""
    return _SEGURO.sub("_", f"{video_id}_{clip_id}")


def filtro_reframe(config, altura=None):
    """16:9 -> 9:16. Devolve o trecho do grafo que sai em [vr].

    `altura` sobrepõe a de saída: no layout split o clip ocupa só a parte de
    cima, e o reframe passa a produzir largura x altura_do_clip.
    """
    saida = config["saida"]
    largura = int(saida["largura"])
    altura = int(altura) if altura else int(saida["altura"])
    reframe = config["reframe"]
    proporcao = largura / altura

    if reframe["modo"] == "desfoque":
        sigma = reframe["desfoque_sigma"]
        # Fundo: preenche a tela e é borrado; frente: cabe inteiro, centrado.
        # É o formato que preserva a composição original do vídeo — útil quando
        # o enquadramento tem informação nas duas bordas.
        return (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={largura}:{altura}:force_original_aspect_ratio=increase,"
            f"crop={largura}:{altura},gblur=sigma={sigma}[bgb];"
            f"[fg]scale={largura}:{altura}:force_original_aspect_ratio=decrease[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[vr];"
        )

    ancora = reframe["ancora_horizontal"]
    # Recorte que preenche: pega a maior janela 9:16 que cabe no original,
    # limitando pelos DOIS lados. Limitar só pela altura distorceria um vídeo
    # que já viesse mais alto que 9:16 (um Short republicado, por exemplo).
    return (
        f"[0:v]crop=w='min(iw,ih*{proporcao})':h='min(ih,iw/{proporcao})':"
        f"x='(iw-ow)*{ancora}':y='(ih-oh)/2',"
        f"scale={largura}:{altura},setsar=1[vr];"
    )


def filtro_zoom(config, duracao_s, altura=None):
    """Push-in lento. Devolve '' quando desligado (o padrão do template).

    Usa `zoompan`, que é a única forma de zoom temporal do ffmpeg — o `crop`
    avalia largura e altura uma vez só e não serve. O zoompan é conhecido por
    micro-tremor de arredondamento; por isso o template o entrega desligado.
    """
    zoom = config["zoom"]
    if not zoom.get("ativo") or duracao_s <= 0:
        return ""

    saida = config["saida"]
    largura = int(saida["largura"])
    altura = int(altura) if altura else int(saida["altura"])
    fps = int(saida["fps"])
    fator = float(zoom["fator_final"])
    quadros = max(1, int(fps * duracao_s))
    taxa = (fator - 1.0) / quadros

    return (
        f"[vr]zoompan=z='min(1+{taxa:.8f}*on,{fator})':d=1:"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={largura}x{altura}:fps={fps}[vz];"
    )


def cadeia_audio(sons):
    """Mistura os efeitos sobre o áudio original. '' quando não há efeito.

    Cada som é uma ENTRADA separada do ffmpeg (`-i`), atrasada com `adelay`
    até o instante planejado e atenuada pelo volume do template. Entradas de
    verdade, e não filtros de geração, porque é o que permite usar os arquivos
    de áudio que o usuário escolheu.

    `normalize=0` no amix não é detalhe: sem ele o filtro divide o volume pelo
    número de entradas, e a fala afunda um pouco a cada efeito acrescentado —
    um clip com oito efeitos sairia com a voz na metade do volume, sem que
    nada no template pedisse isso.
    """
    if not sons:
        return ""

    partes, rotulos = [], []
    for i, som in enumerate(sons):
        rotulo = f"[sfx{i}]"
        atraso_ms = int(round(float(som["instante_s"]) * 1000))
        # all=1 aplica o atraso a todos os canais: sem isso, um efeito estéreo
        # sai com um canal adiantado.
        partes.append(
            f"[{i + 1}:a]adelay={atraso_ms}:all=1,volume={som['volume']}{rotulo}"
        )
        rotulos.append(rotulo)

    entradas = "[0:a]" + "".join(rotulos)
    partes.append(
        f"{entradas}amix=inputs={len(sons) + 1}:duration=first:"
        "dropout_transition=0:normalize=0[aout]"
    )
    return ";".join(partes)


def _cor_drawbox(valor):
    """#RRGGBB[AA] -> 0xRRGGBB@alfa, a notação de cor do drawbox."""
    texto = str(valor).strip().lstrip("#")
    alfa = int(texto[6:8], 16) / 255 if len(texto) == 8 else 1.0
    return f"0x{texto[:6]}@{alfa:.3f}"


def filtro_split(config, indice_filler, entrada):
    """Empilha o clip (já em `entrada`) sobre o preenchimento. Sai em [vs].

    O preenchimento é a ÚLTIMA entrada do ffmpeg (depois dos efeitos), então
    os índices `[n:a]` da cadeia de áudio não se deslocam. Ele é escalado para
    cobrir a faixa e cortado ao centro, e nenhum áudio dele é mapeado: o som é
    só o do clip.
    """
    from editing import layout as layout_mod

    saida = config["saida"]
    largura = int(saida["largura"])
    fps = int(saida["fps"])
    altura_clip, altura_fill = layout_mod.alturas(config)

    grafo = (
        f"{entrada}fps={fps},format=yuv420p[vc];"
        f"[{indice_filler}:v]fps={fps},"
        f"scale={largura}:{altura_fill}:force_original_aspect_ratio=increase,"
        f"crop={largura}:{altura_fill},setsar=1,format=yuv420p[vf];"
        "[vc][vf]vstack=inputs=2:shortest=1"
    )
    sep = config["split"]["separador"]
    if sep.get("ativo") and sep.get("espessura", 0) > 0:
        espessura = int(sep["espessura"])
        y = max(0, altura_clip - espessura // 2)
        grafo += (
            f",drawbox=x=0:y={y}:w=iw:h={espessura}:"
            f"color={_cor_drawbox(sep['cor'])}:t=fill"
        )
    return grafo + "[vs];"


def filtergraph(config, ass_nome, duracao_s, sons=None, filler=None,
                efeitos=None):
    """Grafo completo: vídeo em [vout], e áudio em [aout] quando há efeito.

    `filler` (dict de editing.layout.planejar_filler) liga o layout split.
    `efeitos` ({"picos": [...]}) alimenta a seção `efeitos` do template; com
    ela toda desligada o grafo é byte a byte o de antes de ela existir.
    """
    from editing import efeitos as efeitos_mod

    altura_clip = None
    if filler:
        from editing import layout as layout_mod
        altura_clip = layout_mod.alturas(config)[0]

    grafo = filtro_reframe(config, altura_clip)
    zoom = filtro_zoom(config, duracao_s, altura_clip)
    grafo += zoom
    entrada = "[vz]" if zoom else "[vr]"
    picos = (efeitos or {}).get("picos")
    punch = efeitos_mod.filtro_punch(config, duracao_s, entrada, altura_clip, picos)
    if punch:
        grafo += punch
        entrada = "[vp]"
    if filler:
        grafo += filtro_split(config, 1 + len(sons or []), entrada)
        entrada = "[vs]"
    acabamento, entrada = efeitos_mod.filtro_acabamento(config, duracao_s, entrada)
    grafo += acabamento
    grafo += f"{entrada}ass={ass_nome}[vout]"

    if efeitos_mod.audio_ativo(config):
        audio = efeitos_mod.cadeia_audio(
            config, sons, duracao_s,
            voz_medida=(efeitos or {}).get("voz_medida"))
    else:
        audio = cadeia_audio(sons)
    if audio:
        grafo += ";" + audio
    return grafo


def comando(config, video_path, inicio_s, duracao_s, ass_nome, saida_path,
            ffmpeg="ffmpeg", sons=None, filler=None, efeitos=None):
    """Argumentos do ffmpeg, como lista. Função pura."""
    saida = config["saida"]
    argumentos = [
        ffmpeg, "-y",
        # -ss ANTES de -i: busca rápida. Como há recodificação, o corte sai
        # exato mesmo assim (o accurate_seek é o padrão). Estas duas opções
        # valem só para a entrada seguinte, então os efeitos abaixo não são
        # cortados junto.
        "-ss", f"{inicio_s:.3f}",
        "-t", f"{duracao_s:.3f}",
        "-i", video_path,
    ]
    for som in sons or []:
        if som.get("loop"):       # cama ambiente: mais curta que o clip
            argumentos += ["-stream_loop", "-1"]
        argumentos += ["-i", som["caminho"]]
    if filler:
        # Última entrada. -stream_loop só quando o arquivo é mais curto que o
        # clip (o plano já o põe em inicio 0); -t limita ao tamanho do clip.
        if filler.get("loop"):
            argumentos += ["-stream_loop", "-1"]
        argumentos += [
            "-ss", f"{float(filler.get('inicio_s', 0.0)):.3f}",
            "-t", f"{duracao_s:.3f}",
            "-i", filler["caminho"],
        ]

    argumentos += [
        "-filter_complex",
        filtergraph(config, ass_nome, duracao_s, sons, filler, efeitos),
        "-map", "[vout]",
    ]
    # Com efeito, o áudio sai do amix. Sem efeito, o '?' torna a trilha
    # opcional: vídeo mudo não pode derrubar o render.
    from editing import efeitos as efeitos_mod
    com_aout = bool(sons) or efeitos_mod.audio_ativo(config)
    argumentos += ["-map", "[aout]"] if com_aout else ["-map", "0:a?"]

    argumentos += [
        "-c:v", str(saida["codec_video"]),
        "-preset", str(saida["preset"]),
        "-crf", str(int(saida["crf"])),
        "-r", str(int(saida["fps"])),
        "-c:a", str(saida["codec_audio"]),
        "-b:a", str(saida["bitrate_audio"]),
        # Move o índice para o começo: sem isso, plataforma e player precisam
        # baixar o arquivo inteiro antes do primeiro quadro.
        "-movflags", "+faststart",
        saida_path,
    ]
    return argumentos


def renderizar(config, video_path, inicio_s, fim_s, ass_texto, nome, destino_dir=None,
               executar=None, caminho_ffmpeg=None, sons=None, filler=None,
               efeitos=None):
    """Escreve o .ass, roda o ffmpeg e devolve o caminho do .mp4."""
    from editing import efeitos as efeitos_mod
    from pipeline.download import _garantir_ffmpeg

    if not os.path.exists(video_path):
        raise ErroRender(f"vídeo-fonte não encontrado: {video_path}")

    destino_dir = destino_dir or settings.RENDER_DIR
    os.makedirs(destino_dir, exist_ok=True)
    executar = executar or subprocess.run
    ffmpeg = _garantir_ffmpeg(caminho_ffmpeg)

    ass_nome = f"{nome}.ass"
    with open(os.path.join(destino_dir, ass_nome), "w", encoding="utf-8") as f:
        f.write(ass_texto)

    saida_path = os.path.join(destino_dir, f"{nome}.mp4")
    duracao = max(0.0, float(fim_s) - float(inicio_s))
    if efeitos_mod.audio_ativo(config):
        medida = efeitos_mod.medir_voz(config, video_path, inicio_s, duracao,
                                       ffmpeg, executar)
        if medida:
            efeitos = {**(efeitos or {}), "voz_medida": medida}
    argumentos = comando(
        config, video_path, inicio_s, duracao, ass_nome, saida_path, ffmpeg,
        sons=sons, filler=filler, efeitos=efeitos,
    )

    # cwd no diretório do .ass: ver a docstring do módulo.
    resultado = executar(argumentos, capture_output=True, text=True, cwd=destino_dir)
    if resultado.returncode != 0:
        ultima = (resultado.stderr or "").strip().splitlines()
        raise ErroRender(
            f"ffmpeg falhou ao renderizar {nome}: "
            f"{ultima[-1] if ultima else 'sem stderr'}"
        )
    log.info("Renderizado %s (%.1fs).", saida_path, duracao)
    return saida_path
