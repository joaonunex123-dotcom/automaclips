"""Efeitos visuais e de mixagem do clip: punch-in, flash, vinheta, barra e
ducking. Tudo aqui é função pura de string — o ffmpeg só roda em render.py.

A seção `efeitos` do template_config.json é DESLIGADA por padrão. Com todas as
flags `ativo: false`, nenhuma função deste módulo acrescenta uma letra ao
filtergraph: o render sai com os mesmos filtros e parâmetros de antes.

Visual
  zoom     punch-in suave (bump seno², com ease-in-out) em picos de energia da
           fala (`modo: "picos"`) ou a cada N segundos (`modo: "intervalo"`)
  flash    clarão curto que decai nas transições: abertura e fim do hook
  vinheta  escurecimento das bordas
  barra    barra fina de progresso na base

Áudio
  voz      normaliza a fala do clip para `lufs` (loudnorm, uma passada)
  ducking  sidechaincompress: efeitos e pad abaixam quando a voz fala
  pad      cama ambiente baixa por categoria, em loop (arquivo gerado por
           editing/gerar_sfx.py; ver assets/sfx/README.md)
  teto     cada efeito é limitado a `sfx_abaixo_da_voz_db` dB abaixo da voz,
           medindo a sonoridade do arquivo — não confia no volume do template
"""
import logging
import os

log = logging.getLogger(__name__)

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODOS_ZOOM = ("picos", "intervalo")
EFEITOS_VISUAIS = ("zoom", "flash", "vinheta", "barra")
EFEITOS_AUDIO = ("voz", "ducking", "pad")


class ErroEfeitos(Exception):
    """Arquivo de pad ausente ou configuração impossível de executar."""


def _ativo(config, nome):
    return bool(config.get("efeitos", {}).get(nome, {}).get("ativo"))


def visual_ativo(config):
    return any(_ativo(config, n) for n in EFEITOS_VISUAIS)


def audio_ativo(config):
    return any(_ativo(config, n) for n in EFEITOS_AUDIO)


def algum_ativo(config):
    return visual_ativo(config) or audio_ativo(config)


def _cor_ffmpeg(valor):
    return "0x" + str(valor).strip().lstrip("#")[:6].upper()


# --- instantes ----------------------------------------------------------------

def _espacados(candidatos, duracao_s, janela_s, espaco_s, maximo):
    """Escolhe instantes em ordem de tempo respeitando espaço e teto.

    Só entram instantes cujo bump inteiro cabe no clip (início + janela).
    """
    aceitos = []
    for c in sorted(float(x) for x in candidatos):
        if c < 0.0 or c + janela_s > duracao_s:
            continue
        if aceitos and c - aceitos[-1] < espaco_s:
            continue
        aceitos.append(c)
        if maximo and len(aceitos) >= maximo:
            break
    return aceitos


def instantes_zoom(config, duracao_s, picos=None):
    zoom = config["efeitos"]["zoom"]
    if not zoom.get("ativo") or duracao_s <= 0:
        return []
    dur = float(zoom["duracao_s"])
    maximo = int(zoom.get("maximo_por_clip", 0))
    espaco = max(float(zoom.get("espacamento_minimo_s", 0.0)), dur * 1.5)
    if zoom["modo"] == "picos":
        # O bump começa um pouco ANTES do pico: o olho lê o zoom como reação
        # ao que foi dito, e não como atraso.
        antecedencia = dur * 0.25
        candidatos = [p - antecedencia for p in (picos or [])]
    else:
        passo = float(zoom["intervalo_s"])
        candidatos = []
        t = passo * 0.5
        while t + dur <= duracao_s:
            candidatos.append(t)
            t += passo
    return _espacados(candidatos, duracao_s, dur, espaco, maximo)


def instantes_flash(config, duracao_s):
    """Abertura e virada do hook para o conteúdo (as transições do clip)."""
    flash = config["efeitos"]["flash"]
    if not flash.get("ativo") or duracao_s <= 0:
        return []
    instantes = []
    if flash.get("na_abertura"):
        instantes.append(0.0)
    hook = config.get("hook", {})
    if flash.get("no_fim_do_hook") and hook.get("ativo"):
        fim = float(hook.get("duracao_s") or 0.0)
        if 0.0 < fim < duracao_s:
            instantes.append(fim)
    return instantes


# --- filtros de vídeo ---------------------------------------------------------

def _bump(var_t, inicio, dur):
    """Pulso suave 0 -> 1 -> 0 em [inicio, inicio+dur]; zero fora."""
    return (f"pow(sin(PI*clip(({var_t}-{inicio:.3f})/{dur:.3f},0,1)),2)")


def filtro_punch(config, duracao_s, entrada, altura=None, picos=None):
    """'' sem zoom ativo; senão '<entrada>fps,zoompan[vp];'.

    zoompan com d=1 consome um quadro por vez; o tempo vem de `on/fps` (o
    fps= logo antes fixa a taxa, então on/fps é o tempo real). O fator é
    1 + (fator-1) * soma dos bumps, que o espaçamento mínimo mantém separados.
    """
    instantes = instantes_zoom(config, duracao_s, picos)
    if not instantes:
        return ""
    zoom = config["efeitos"]["zoom"]
    saida = config["saida"]
    largura = int(saida["largura"])
    altura = int(altura) if altura else int(saida["altura"])
    fps = int(saida["fps"])
    amp = float(zoom["fator"]) - 1.0
    dur = float(zoom["duracao_s"])
    soma = "+".join(_bump(f"(on/{fps})", i, dur) for i in instantes)
    return (
        f"{entrada}fps={fps},"
        f"zoompan=z='1+{amp:.4f}*({soma})':d=1:"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={largura}x{altura}:fps={fps}[vp];"
    )


def filtro_acabamento(config, duracao_s, entrada):
    """Flash + vinheta + barra sobre a composição final, antes da legenda.

    Devolve (trecho_do_grafo, rotulo_de_saida). Sem nada ativo: ('', entrada).
    A legenda vem depois (no render), então o flash não a branqueia.
    """
    ef = config["efeitos"]
    cadeia = []
    instantes = instantes_flash(config, duracao_s)
    if instantes:
        flash = ef["flash"]
        dur = float(flash["duracao_s"])
        termos = "+".join(
            f"between(t,{i:.3f},{i + dur:.3f})*(1-(t-{i:.3f})/{dur:.3f})"
            for i in instantes
        )
        cadeia.append(
            f"eq=brightness='{float(flash['intensidade']):.3f}*({termos})':eval=frame"
        )
    if _ativo(config, "vinheta"):
        cadeia.append(f"vignette=angle={float(ef['vinheta']['angulo']):.3f}")

    trecho, rotulo = "", entrada
    if cadeia:
        trecho += f"{rotulo}{','.join(cadeia)}[vfx];"
        rotulo = "[vfx]"

    if _ativo(config, "barra") and duracao_s > 0:
        barra = ef["barra"]
        saida = config["saida"]
        largura, fps = int(saida["largura"]), int(saida["fps"])
        altura = int(barra["altura"])
        y = "H-h" if barra.get("posicao", "base") == "base" else "0"
        # color como fonte + overlay: o x do overlay é reavaliado por quadro
        # (o drawbox avalia w uma vez só, e a barra ficaria cheia desde o 0).
        trecho += (
            f"color=c={_cor_ffmpeg(barra['cor'])}:s={largura}x{altura}:"
            f"r={fps}:d={duracao_s:.3f}[barra];"
            f"{rotulo}[barra]overlay=x='-w+w*t/{duracao_s:.3f}':y={y}:"
            "shortest=1[vbar];"
        )
        rotulo = "[vbar]"
    return trecho, rotulo


# --- áudio --------------------------------------------------------------------

_LUFS_CACHE = {}


def lufs_do_arquivo(caminho):
    """Sonoridade (K-weighted, sem gating) do arquivo, ou None se não der."""
    if caminho in _LUFS_CACHE:
        return _LUFS_CACHE[caminho]
    try:
        import soundfile as sf
        from editing.gerar_sfx import lufs_sem_gating
        dados, taxa = sf.read(caminho, always_2d=False)
        if dados.ndim > 1:
            dados = dados.mean(axis=1)
        valor = float(lufs_sem_gating(dados)) if taxa == 48000 else None
    except Exception as e:      # arquivo ilegível, sem soundfile, taxa != 48k
        log.debug("Sem LUFS para %s: %s", caminho, e)
        valor = None
    _LUFS_CACHE[caminho] = valor
    return valor


def volume_com_teto(volume, lufs_arquivo, voz_lufs, abaixo_db):
    """min(volume do template, ganho que deixa o som `abaixo_db` sob a voz).

    Sem a sonoridade do arquivo, assume o padrão do gerador (-24 LUFS) e
    limita o ganho a 1.0 — nunca amplifica um som que não se mediu.
    """
    if lufs_arquivo is None:
        return min(float(volume), 1.0)
    ganho_max = 10 ** ((voz_lufs - abaixo_db - lufs_arquivo) / 20.0)
    return min(float(volume), ganho_max)


def caminho_pad(config, categoria, base_dir=None):
    pad = config["efeitos"]["pad"]
    raiz = config["sfx"].get("diretorio") or ""
    if not os.path.isabs(raiz):
        raiz = os.path.join(base_dir or _RAIZ, raiz)
    return os.path.join(raiz, pad["arquivo"].format(categoria=categoria or "full"))


def plano_pad(config, categoria, base_dir=None):
    """[] sem pad; senão um 'som' com loop=True para entrar como entrada -i."""
    if not _ativo(config, "pad"):
        return []
    caminho = caminho_pad(config, categoria, base_dir)
    if not os.path.exists(caminho):
        raise ErroEfeitos(
            f"pad ausente: {caminho}. Rode `python -m editing.gerar_sfx` ou "
            "desligue efeitos.pad.ativo."
        )
    return [{
        "tipo": "pad", "loop": True, "instante_s": 0.0, "caminho": caminho,
        "volume": float(config["efeitos"]["pad"]["volume"]),
    }]


def verificar_pads(config, categorias, base_dir=None):
    """Falha uma vez, antes da fila, se faltar o pad de alguma categoria."""
    if not _ativo(config, "pad"):
        return
    faltando = sorted({
        caminho_pad(config, c, base_dir) for c in categorias
        if not os.path.exists(caminho_pad(config, c, base_dir))
    })
    if faltando:
        raise ErroEfeitos(
            "pads não encontrados: " + ", ".join(faltando) +
            ". Rode `python -m editing.gerar_sfx` ou desligue efeitos.pad.ativo."
        )


def cadeia_audio(config, sons, duracao_s, medir=None):
    """Grafo de áudio com efeitos ativos. '' quando não há o que fazer.

    Entradas: [0:a] é a voz; sons[i] é a entrada [i+1:a] (efeitos e pad). Voz
    normalizada -> (ducking) -> mix com a cama dos efeitos, sempre com
    normalize=0 para a voz não afundar a cada fonte acrescentada.
    """
    ef = config["efeitos"]
    if not audio_ativo(config):
        return ""
    medir = medir or lufs_do_arquivo
    voz_lufs = float(ef["voz"]["lufs"])
    abaixo = float(ef["teto_sfx_abaixo_da_voz_db"])
    taxa = 48000

    partes = []
    if _ativo(config, "voz"):
        partes.append(
            f"[0:a]loudnorm=I={voz_lufs:g}:TP=-1.5:LRA=11,"
            f"aresample={taxa}[voz0]"
        )
    else:
        partes.append(f"[0:a]aresample={taxa}[voz0]")

    bus = []
    for i, som in enumerate(sons or []):
        rotulo = f"[bus{i}]"
        atraso = int(round(float(som["instante_s"]) * 1000))
        if som.get("tipo") == "pad":
            fade = min(0.6, duracao_s / 4.0)
            partes.append(
                f"[{i + 1}:a]aresample={taxa},volume={som['volume']:g},"
                f"atrim=duration={duracao_s:.3f},"
                f"afade=t=in:d={fade:.2f},"
                f"afade=t=out:st={max(0.0, duracao_s - fade):.3f}:d={fade:.2f}"
                f"{rotulo}"
            )
        else:
            vol = volume_com_teto(som["volume"], medir(som["caminho"]),
                                  voz_lufs, abaixo)
            partes.append(
                f"[{i + 1}:a]aresample={taxa},adelay={atraso}:all=1,"
                f"volume={vol:.4f}{rotulo}"
            )
        bus.append(rotulo)

    if not bus:
        partes.append("[voz0]anull[aout]")
        return ";".join(partes)

    if _ativo(config, "ducking"):
        d = ef["ducking"]
        partes.append("[voz0]asplit=2[voz][sc]")
        if len(bus) == 1:
            mix = f"{bus[0]}apad=whole_dur={duracao_s:.3f}[mix]"
        else:
            mix = (f"{''.join(bus)}amix=inputs={len(bus)}:duration=longest:"
                   f"dropout_transition=0:normalize=0,"
                   f"apad=whole_dur={duracao_s:.3f}[mix]")
        partes.append(mix)
        partes.append(
            f"[mix][sc]sidechaincompress=threshold={float(d['limiar']):g}:"
            f"ratio={float(d['razao']):g}:attack={float(d['ataque_ms']):g}:"
            f"release={float(d['liberacao_ms']):g}:makeup=1[duck]"
        )
        fonte = "[duck]"
        voz = "[voz]"
    else:
        fonte = None
        voz = "[voz0]"
    entradas = voz + (fonte if fonte else "".join(bus))
    n = 2 if fonte else 1 + len(bus)
    partes.append(
        f"{entradas}amix=inputs={n}:duration=first:"
        "dropout_transition=0:normalize=0[aout]"
    )
    return ";".join(partes)
