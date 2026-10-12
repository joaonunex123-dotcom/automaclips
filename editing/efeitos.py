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
# Transições da biblioteca (editing/efeitos.py > planejar_transicoes). `flash`
# e `zoom` são os efeitos que já existiam; os outros quatro são novos.
TRANSICOES_NOVAS = ("shutter", "iris", "whip_pan", "glitch")
TRANSICOES = TRANSICOES_NOVAS + ("flash", "zoom")
EFEITOS_VISUAIS = ("zoom", "flash", "vinheta", "barra") + TRANSICOES_NOVAS
EFEITOS_AUDIO = ("voz", "ducking", "pad", "limiter", "sfx_visuais")

# Em que ponto da duração do efeito cai o "momento-chave" (o que o som segue):
# o clarão tem o pico no começo; os demais, no meio (fechado, borrão máximo).
FRACAO_CHAVE = {"shutter": 0.5, "iris": 0.5, "whip_pan": 0.5, "glitch": 0.5,
                "flash": 0.0, "zoom": 0.5}
# Preferência de cada tipo por gatilho: transição estrutural pede cortina/íris;
# ênfase pede algo que "bate" (zoom, glitch).
PREFERENCIA = {
    "estrutura": ("shutter", "iris", "whip_pan", "flash", "glitch", "zoom"),
    "enfase": ("zoom", "glitch", "flash", "whip_pan", "shutter", "iris"),
}
GATILHOS_ESTRUTURA = ("abertura", "virada", "grafico_entrada", "grafico_saida")


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


# --- cadência das transições --------------------------------------------------

def cadencia_governa(config):
    """Se o planejador de transições decide os instantes de flash e zoom.

    Só quando alguma transição NOVA está ligada, ou `cadencia.ativo`. Com só
    flash/zoom ligados (e a cadência desligada) vale o comportamento de antes,
    byte a byte.
    """
    ef = config["efeitos"]
    return (any(_ativo(config, n) for n in TRANSICOES_NOVAS)
            or bool(ef.get("cadencia", {}).get("ativo")))


def _duracao_transicao(config, tipo):
    return float(config["efeitos"][tipo]["duracao_s"])


def planejar_transicoes(config, duracao_s, picos=None, segmentos_grafico=None):
    """Eventos [{t, dur, tipo, gatilho, chave_s}] em ordem de tempo.

    Regras (do pedido): no máximo 1 efeito a cada `cadencia.espacamento_minimo_s`
    (4 s) e variar o tipo, nunca repetindo o anterior quando há alternativa.
    Gatilhos, em ordem de prioridade: abertura (só flash) e virada do hook;
    entrada de gráfico; picos de energia (ênfase); saída de gráfico. Em disputa
    por espaço vence o de maior prioridade e, em empate, o mais cedo.

    Função pura: mesma entrada, mesma saída (nada sorteado).
    """
    if duracao_s <= 0 or not cadencia_governa(config):
        return []
    pool = [n for n in TRANSICOES if _ativo(config, n)]
    if not pool:
        return []
    cad = config["efeitos"]["cadencia"]
    espaco = float(cad["espacamento_minimo_s"])
    maximo = int(cad["maximo_por_clip"])

    candidatos = []        # (prioridade, instante, gatilho)
    flash = config["efeitos"]["flash"]
    if _ativo(config, "flash") and flash.get("na_abertura"):
        candidatos.append((0, 0.0, "abertura"))
    hook = config.get("hook", {})
    fim_hook = float(hook.get("duracao_s") or 0.0) if hook.get("ativo") else 0.0
    if 0.0 < fim_hook < duracao_s:
        candidatos.append((0, fim_hook, "virada"))
    for seg in segmentos_grafico or []:
        candidatos.append((1, float(seg["inicio_s"]), "grafico_entrada"))
        candidatos.append((3, float(seg["fim_s"]), "grafico_saida"))
    for p in picos or []:
        candidatos.append((2, float(p), "enfase"))

    aceitos = []
    for prioridade, instante, gatilho in sorted(candidatos):
        if not 0.0 <= instante <= duracao_s:
            continue
        if any(abs(instante - a[1]) < espaco for a in aceitos):
            continue
        aceitos.append((prioridade, instante, gatilho))
        if maximo and len(aceitos) >= maximo:
            break
    aceitos.sort(key=lambda a: a[1])

    eventos, usos, anterior = [], {n: 0 for n in pool}, None
    for _prio, instante, gatilho in aceitos:
        if gatilho == "abertura":
            tipo = "flash"
        else:
            familia = ("estrutura" if gatilho in GATILHOS_ESTRUTURA
                       else "enfase")
            ordem = [n for n in PREFERENCIA[familia] if n in pool]
            livres = [n for n in ordem if n != anterior] or ordem
            tipo = min(livres, key=lambda n: (usos[n], ordem.index(n)))
        dur = _duracao_transicao(config, tipo)
        chave = FRACAO_CHAVE[tipo]
        inicio = min(max(0.0, instante - chave * dur),
                     max(0.0, duracao_s - dur))
        eventos.append({"t": round(inicio, 3), "dur": round(dur, 3),
                        "tipo": tipo, "gatilho": gatilho,
                        "chave_s": round(inicio + chave * dur, 3)})
        usos[tipo] += 1
        anterior = tipo
    return eventos


# --- SFX dos efeitos visuais ---------------------------------------------------

_ANCORAS = {}


def ancora_do_som(nome):
    """Segundo, dentro do arquivo, em que o som `nome` "bate" (cache)."""
    if nome not in _ANCORAS:
        from editing import gerar_sfx
        _ANCORAS[nome] = gerar_sfx.ancora_do_arquivo(nome)
    return _ANCORAS[nome]


def diretorio_sfx(config, base_dir=None):
    raiz = config["sfx"].get("diretorio") or ""
    if not os.path.isabs(raiz):
        raiz = os.path.join(base_dir or _RAIZ, raiz)
    return raiz


def plano_sfx_visuais(config, eventos, duracao_s, base_dir=None):
    """Sons que acompanham os efeitos visuais, alinhados ao quadro.

    Cada evento dispara o som mapeado em `sfx_visuais.mapa[tipo]`, se a
    categoria dele estiver ligada. O som é posto de modo que o instante em
    que ele "bate" (a âncora) caia no momento-chave do efeito: o arquivo
    começa em `chave_s - ancora`. Se isso cairia antes do clip, o início do
    arquivo é cortado (`corte_ini_s`) em vez de atrasar — a sincronia nunca
    cede. Sons são sintetizados na hora se o arquivo ainda não existir.
    """
    cfg = config["efeitos"]["sfx_visuais"]
    if not cfg.get("ativo") or not eventos:
        return []
    from editing import gerar_sfx

    sons = []
    pasta = diretorio_sfx(config, base_dir)
    for e in eventos:
        nome = cfg["mapa"].get(e["tipo"])
        if not nome or not cfg["categorias"].get(nome):
            continue
        ancora = ancora_do_som(nome)
        inicio = float(e["chave_s"]) - ancora
        if inicio + 0.05 >= duracao_s:
            continue
        sons.append({
            "tipo": "sfx_visual", "evento": e["tipo"], "som": nome,
            "caminho": gerar_sfx.gerar_um(nome, pasta),
            "instante_s": max(0.0, round(inicio, 4)),
            "corte_ini_s": max(0.0, round(-inicio, 4)),
            "chave_s": float(e["chave_s"]),
            "volume": float(cfg["volume"]),
        })
    return sons


def instantes_do_tipo(eventos, tipo):
    return [e["t"] for e in eventos or [] if e["tipo"] == tipo]


def _janela(e):
    return f"between(t,{e['t']:.3f},{e['t'] + e['dur']:.3f})"


def _pulso(e, var="t"):
    """sin(pi * (t - t0)/dur): 0 -> 1 -> 0 na janela do evento.

    `var` existe porque o geq chama o tempo de `T` (maiúsculo) e os demais
    filtros, de `t`.
    """
    return f"sin(PI*({var}-{e['t']:.3f})/{e['dur']:.3f})"


def filtros_transicoes(config, eventos, largura, altura):
    """Lista de filtros de vídeo das transições NOVAS (flash/zoom ficam fora).

    Cada filtro só age dentro da janela do evento (`enable`), então o custo
    fora delas é zero. Tudo em filtros nativos do ffmpeg, sem arquivo externo.
    """
    ef = config["efeitos"]
    saida = []
    for e in eventos or []:
        tipo = e["tipo"]
        if tipo not in TRANSICOES_NOVAS:
            continue
        k = max(0.0, min(1.0, float(ef[tipo]["intensidade"])))
        jan = _janela(e)
        if tipo == "shutter":
            # Cortina de lâminas horizontais que fecham do centro de cada uma
            # e reabrem. k = fração máxima fechada.
            n = 6
            faixa = altura / n
            for i in range(n):
                saida.append(
                    f"drawbox=x=0:y='{i * faixa:.2f}+{faixa:.2f}*(1-{k:.3f}"
                    f"*{_pulso(e)})/2':w=iw:h='{faixa:.2f}*{k:.3f}"
                    f"*{_pulso(e)}':color=black:t=fill:enable='{jan}'")
        elif tipo == "glitch":
            # Franja de cor por deslocamento dos planos de CROMA (nativo em
            # YUV). O rgbashift seria o óbvio, mas força o grafo inteiro a
            # passar por RGB em todos os quadros, não só nos da janela: o
            # render do clip passou de 10 s para 46 s.
            px = int(round(6 + 34 * k))
            terco = e["dur"] / 3.0
            for j, sinal in enumerate((1, -0.6, 1)):
                a = e["t"] + j * terco
                janela = f"between(t,{a:.3f},{a + terco:.3f})"
                d = int(round(px * sinal))
                saida.append(
                    f"chromashift=cbh={-d}:crh={d}:cbv={d // 4}:crv={-d // 4}:"
                    f"edge=smear:enable='{janela}'")
            saida.append(f"eq=saturation={1.0 + 0.7 * k:.2f}:enable='{jan}'")

        elif tipo == "whip_pan":
            # Borrão horizontal em três degraus (sobe, pico, desce): o
            # avgblur não anima o tamanho, então a rampa é por janelas.
            pico = int(20 + 140 * k)
            terco = e["dur"] / 3.0
            for j, tam in enumerate((pico // 3, pico, pico // 3)):
                a = e["t"] + j * terco
                saida.append(
                    f"avgblur=sizeX={max(1, tam)}:sizeY=1:"
                    f"enable='between(t,{a:.3f},{a + terco:.3f})'")
    return saida


def fontes_iris(config, eventos, largura, altura, fps):
    """[(rotulo, trecho_de_grafo, evento)] das máscaras da íris.

    A máscara é uma FONTE de duração finita, desenhada em 1/10 da resolução
    (um `geq` por pixel em 1080x1920 custava +9 s por clip) e ampliada com
    bicúbico — que de quebra suaviza a borda. Cada uma entra por overlay
    com alfa sobre a composição, só durante a janela do evento.
    """
    ef = config["efeitos"]
    w, h = max(2, largura // 10), max(2, altura // 10)
    saida = []
    for i, e in enumerate(x for x in eventos or [] if x["tipo"] == "iris"):
        k = max(0.0, min(1.0, float(ef["iris"]["intensidade"])))
        raio = (w ** 2 / 4.0 + h ** 2 / 4.0) ** 0.5
        pulso = f"sin(PI*T/{e['dur']:.3f})"
        r = f"{raio:.2f}*(1-{k:.3f}*{pulso})"
        dist = f"hypot(X-{w / 2.0:.1f},Y-{h / 2.0:.1f})"
        rotulo = f"[iris{i}]"
        saida.append((rotulo, (
            f"nullsrc=s={w}x{h}:r={fps}:d={e['dur']:.3f},format=yuva420p,"
            f"geq=lum=0:cb=128:cr=128:a='if(gt({dist},{r}),255,0)',"
            f"scale={largura}:{altura}:flags=bicubic,"
            f"setpts=PTS+{e['t']:.3f}/TB{rotulo};"), e))
    return saida


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


def filtro_punch(config, duracao_s, entrada, altura=None, picos=None,
                 instantes=None):
    """'' sem zoom ativo; senão '<entrada>fps,zoompan[vp];'.

    zoompan com d=1 consome um quadro por vez; o tempo vem de `on/fps` (o
    fps= logo antes fixa a taxa, então on/fps é o tempo real). O fator é
    1 + (fator-1) * soma dos bumps, que o espaçamento mínimo mantém separados.
    """
    if instantes is None:
        instantes = instantes_zoom(config, duracao_s, picos)
    if not instantes or not _ativo(config, "zoom"):
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


def filtro_acabamento(config, duracao_s, entrada, eventos=None):
    """Flash + transições + vinheta + barra sobre a composição, antes da legenda.

    Devolve (trecho_do_grafo, rotulo_de_saida). Sem nada ativo: ('', entrada).
    A legenda vem depois (no render), então o flash não a branqueia.
    `eventos` é o plano de planejar_transicoes; quando a cadência governa, os
    instantes do flash vêm dele.
    """
    ef = config["efeitos"]
    cadeia = []
    if eventos is not None and cadencia_governa(config):
        instantes = (instantes_do_tipo(eventos, "flash")
                     if _ativo(config, "flash") else [])
    else:
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
    if eventos:
        saida = config["saida"]
        cadeia += filtros_transicoes(config, eventos, int(saida["largura"]),
                                     int(saida["altura"]))
    if _ativo(config, "vinheta"):
        cadeia.append(f"vignette=angle={float(ef['vinheta']['angulo']):.3f}")

    trecho, rotulo = "", entrada
    if cadeia:
        trecho += f"{rotulo}{','.join(cadeia)}[vfx];"
        rotulo = "[vfx]"
    saida_cfg = config["saida"]
    for i, (fonte, grafo, _e) in enumerate(fontes_iris(
            config, eventos, int(saida_cfg["largura"]),
            int(saida_cfg["altura"]), int(saida_cfg["fps"]))):
        trecho += grafo
        trecho += (f"{rotulo}{fonte}overlay=0:0:eof_action=pass:"
                   f"format=auto[vir{i}];")
        rotulo = f"[vir{i}]"

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


def parse_loudnorm(stderr):
    """measured_* do JSON que o loudnorm imprime no stderr, ou None."""
    import json
    import re
    blocos = re.findall(r"\{[^{}]*\}", stderr or "", re.S)
    for texto in reversed(blocos):
        try:
            dados = json.loads(texto)
            return {
                "I": float(dados["input_i"]), "TP": float(dados["input_tp"]),
                "LRA": float(dados["input_lra"]),
                "thresh": float(dados["input_thresh"]),
                "offset": float(dados["target_offset"]),
            }
        except (ValueError, KeyError):
            continue
    return None


def medir_voz(config, video_path, inicio_s, duracao_s, ffmpeg="ffmpeg",
              executar=None):
    """Primeira passada do loudnorm: mede a fala do trecho (só áudio, rápido).

    A passada única é dinâmica e costuma ficar ~1 LU abaixo do alvo; com a
    medição, a segunda passada aplica um ganho linear exato. Qualquer falha
    devolve None e o render cai para a passada única.
    """
    if not _ativo(config, "voz"):
        return None
    import subprocess
    executar = executar or subprocess.run
    alvo = float(config["efeitos"]["voz"]["lufs"])
    try:
        r = executar(
            [ffmpeg, "-hide_banner", "-nostats", "-ss", f"{inicio_s:.3f}",
             "-t", f"{duracao_s:.3f}", "-i", video_path, "-vn",
             "-af", f"loudnorm=I={alvo:g}:TP=-1.5:LRA=11:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
    except OSError as e:
        log.warning("Medição da voz falhou (%s); loudnorm de uma passada.", e)
        return None
    medida = parse_loudnorm(r.stderr)
    if medida is None:
        log.warning("Sem medição da voz; loudnorm de uma passada.")
    return medida


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


def cadeia_audio(config, sons, duracao_s, medir=None, voz_medida=None):
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
        medidas = ""
        if voz_medida:      # segunda passada: ganho linear sobre a medição
            medidas = (
                f":measured_I={voz_medida['I']:g}:measured_TP={voz_medida['TP']:g}"
                f":measured_LRA={voz_medida['LRA']:g}"
                f":measured_thresh={voz_medida['thresh']:g}"
                f":offset={voz_medida['offset']:g}:linear=true"
            )
        partes.append(
            f"[0:a]loudnorm=I={voz_lufs:g}:TP=-1.5:LRA=11{medidas},"
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
            corte = float(som.get("corte_ini_s") or 0.0)
            aparar = (f"atrim=start={corte:.4f},asetpts=PTS-STARTPTS,"
                      if corte > 0 else "")
            partes.append(
                f"[{i + 1}:a]aresample={taxa},{aparar}adelay={atraso}:all=1,"
                f"volume={vol:.4f}{rotulo}"
            )
        bus.append(rotulo)

    rotulo_final = "[aout]"
    if _ativo(config, "limiter"):
        rotulo_final = "[mst]"

    def _limiter():
        teto = 10 ** (float(ef["limiter"]["teto_db"]) / 20.0)
        return (f"[mst]alimiter=limit={teto:.4f}:attack=5:release=60:"
                "level=disabled[aout]")

    if not bus:
        partes.append(f"[voz0]anull{rotulo_final}")
        if rotulo_final != "[aout]":
            partes.append(_limiter())
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
        f"dropout_transition=0:normalize=0{rotulo_final}"
    )
    if rotulo_final != "[aout]":
        partes.append(_limiter())
    return ";".join(partes)
