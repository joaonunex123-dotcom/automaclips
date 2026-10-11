"""Gráficos, tabelas e slides da fonte: mostrar INTEIRO em vez de cortar.

O reframe 9:16 padrão recorta o centro do 16:9 e joga fora ~68% da largura.
Num gráfico isso é desastroso: o eixo, a legenda e metade das barras somem.
Este módulo acha os trechos em que a fonte é "conteúdo" (e não pessoa) e o
render passa a mostrá-lo com fit/contain, sobre fundo preenchido:

    fundo "desfoque"  cópia desfocada e escurecida da própria cena, ampliada
    fundo "solido"    cor neutra escura (grafico.cor_solida)

Detecção (heurística local, sem chamada paga) sobre quadros amostrados:

* sem rosto (Haar do OpenCV);
* muita área plana (poucos pixels com gradiente) — fundo de slide;
* poucas cores dominantes (a cor mais frequente concentra o quadro);
* linhas retas alinhadas aos eixos (grade, tabela, eixo de gráfico).

Os quatro precisam valer juntos. O gatilho é conservador de propósito: um
falso positivo troca o crop de uma pessoa por um quadro pequeno no meio da
tela, e um falso negativo só deixa o clip como era antes. Sem detecção
confiável o clip sai como antes — a degradação é sempre para o comportamento
anterior, e qualquer exceção aqui vira "nenhum segmento".

Um modelo de visão poderia desempatar os casos cinza; não está ligado: o
cliente de LLM do projeto não aceita imagem hoje. O gancho `confirmar` de
`detectar` é injetável para quando existir (com cache e orçamento).
"""
import logging
import re
import subprocess

from editing import capa as capa_mod

log = logging.getLogger(__name__)

FUNDOS = ("desfoque", "solido")

# Limiares calibrados com um gráfico de barras sintético (plano 0.87, cores
# 0.98, 17 linhas de eixo) contra 107 quadros de uma reportagem real (nenhum
# passou; o maior "plano" foi 0.68, a vinheta de abertura).
LIMIAR_PLANO = 0.70
LIMIAR_CORES = 0.75
LIMIAR_EIXOS = 4
LIMIAR_BORDAS = 0.04

_DIMENSOES = re.compile(r"Stream #.*Video:.*?[ ,](\d{2,5})x(\d{2,5})")


class ErroGrafico(Exception):
    """Falha ao analisar a fonte."""


# --- características de um quadro ---------------------------------------------

def caracteristicas(bgr, detectar_rosto=None):
    """Medidas de um quadro BGR pequeno. Função pura sobre o array."""
    import cv2
    import numpy as np

    cinza = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = cinza.shape[:2]
    magnitude = np.hypot(cv2.Sobel(cinza, cv2.CV_32F, 1, 0),
                         cv2.Sobel(cinza, cv2.CV_32F, 0, 1))
    plano = float((magnitude < 12).mean())

    bordas = cv2.Canny(cinza, 60, 150)
    densidade = float((bordas > 0).mean())

    retas = cv2.HoughLinesP(bordas, 1, np.pi / 180, threshold=40,
                            minLineLength=int(w * 0.15), maxLineGap=3)
    eixos = 0
    if retas is not None:
        eixos = sum(1 for l in retas[:, 0]
                    if abs(int(l[2]) - int(l[0])) <= 2
                    or abs(int(l[3]) - int(l[1])) <= 2)

    quantizado = (bgr // 32).astype(np.int32)
    indice = quantizado[..., 0] * 64 + quantizado[..., 1] * 8 + quantizado[..., 2]
    histograma = np.bincount(indice.ravel(), minlength=512)
    cores = float(np.sort(histograma)[::-1][:8].sum() / indice.size)

    area = 0.0
    if detectar_rosto is not None:
        rostos = detectar_rosto(bgr)
        if rostos:
            area = max(r[2] * r[3] for r in rostos) / float(w * h)

    caixa = None
    pontos = np.argwhere(bordas > 0)
    if len(pontos) > 20:
        y0, x0 = np.percentile(pontos, 1, axis=0)
        y1, x1 = np.percentile(pontos, 99, axis=0)
        caixa = (float(x0) / w, float(y0) / h, float(x1) / w, float(y1) / h)

    return {"plano": plano, "bordas": densidade, "eixos": int(eixos),
            "cores": cores, "rosto": area, "caixa": caixa}


def eh_grafico(c):
    """Se as medidas de UM quadro dizem 'conteúdo, não pessoa'."""
    if c["rosto"] > 0.0:
        return False
    return (c["plano"] >= LIMIAR_PLANO and c["cores"] >= LIMIAR_CORES
            and (c["eixos"] >= LIMIAR_EIXOS or c["bordas"] >= LIMIAR_BORDAS))


# --- segmentos ----------------------------------------------------------------

def montar_segmentos(instantes, flags, caixas, passo_s, duracao_s,
                     min_duracao_s=1.5, maximo=3, lacuna_max=1):
    """Agrupa quadros consecutivos marcados em segmentos [inicio, fim].

    `instantes` são os centros das amostras (relativos ao clip). Uma lacuna
    de até `lacuna_max` amostras entre dois marcados é fechada: um quadro de
    transição no meio de um slide não pode partir o segmento em dois.
    """
    n = len(flags)
    marcados = list(flags)
    i = 0
    while i < n:
        if not marcados[i]:
            j = i
            while j < n and not marcados[j]:
                j += 1
            if 0 < i and j < n and (j - i) <= lacuna_max:
                for k in range(i, j):
                    marcados[k] = True
            i = j
        else:
            i += 1

    segmentos, i = [], 0
    while i < n:
        if not marcados[i]:
            i += 1
            continue
        j = i
        while j < n and marcados[j]:
            j += 1
        inicio = max(0.0, instantes[i] - passo_s / 2.0)
        fim = min(duracao_s, instantes[j - 1] + passo_s / 2.0)
        if fim - inicio >= min_duracao_s:
            uteis = [caixas[k] for k in range(i, j) if caixas[k]]
            caixa = None
            if uteis:
                caixa = (min(c[0] for c in uteis), min(c[1] for c in uteis),
                         max(c[2] for c in uteis), max(c[3] for c in uteis))
            segmentos.append({"inicio_s": round(inicio, 3),
                              "fim_s": round(fim, 3), "caixa": caixa})
        i = j
    return segmentos[:maximo]


def dimensoes_do_arquivo(caminho, ffmpeg="ffmpeg", executar=None):
    """(largura, altura) do vídeo, lida do `ffmpeg -i` (sem ffprobe)."""
    executar = executar or subprocess.run
    r = executar([ffmpeg, "-hide_banner", "-i", caminho],
                 capture_output=True, text=True)
    achado = _DIMENSOES.search(r.stderr or "")
    if not achado:
        raise ErroGrafico(f"não consegui ler a resolução de {caminho}")
    return int(achado.group(1)), int(achado.group(2))


def detectar(video_path, inicio_s, fim_s, config, ffmpeg="ffmpeg",
             executar=None, detectar_rosto="auto", confirmar=None):
    """Segmentos gráficos do trecho, relativos ao início do clip.

    Devolve [] (nunca levanta) quando nada é confiável: o chamador renderiza
    como antes. `confirmar(segmentos) -> segmentos` é o gancho do modelo de
    visão; ausente, a heurística local decide sozinha.
    """
    cfg = config["grafico"]
    duracao = float(fim_s) - float(inicio_s)
    if duracao <= 0:
        return []
    try:
        taxa = float(cfg["amostras_por_s"])
        quadros = capa_mod.amostrar(video_path, inicio_s, fim_s, ffmpeg,
                                    executar, fps=taxa)
        if not quadros:
            return []
        if detectar_rosto == "auto":
            detectar_rosto = capa_mod._detector_de_rosto()
        medidas = [caracteristicas(q["bgr"], detectar_rosto) for q in quadros]
        flags = [eh_grafico(m) for m in medidas]
        instantes = [q["t"] - float(inicio_s) for q in quadros]
        segmentos = montar_segmentos(
            instantes, flags, [m["caixa"] for m in medidas], 1.0 / taxa,
            duracao, min_duracao_s=float(cfg["min_duracao_s"]),
            maximo=int(cfg["maximo_por_clip"]))
        if confirmar is not None and segmentos:
            segmentos = confirmar(segmentos)
    except Exception as e:
        log.warning("Detecção de gráfico falhou (%s); clip sai como antes.", e)
        return []

    if segmentos:
        log.info("Gráfico detectado em %s: %s", video_path,
                 ", ".join(f"{s['inicio_s']:.1f}-{s['fim_s']:.1f}s"
                           for s in segmentos))
    return segmentos


# --- geometria ----------------------------------------------------------------

def plano_do_segmento(segmento, config, src_w, src_h, altura_alvo=None):
    """Acrescenta ao segmento o recorte (sem cortar o gráfico) e a escala.

    `largura_final` é a largura do conteúdo na tela de 1080. Abaixo de
    `largura_minima` (70%) o recorte restringe-se à caixa do conteúdo — as
    margens vazias saem, o gráfico não — e a ampliação vai até o limite da
    tela. O resultado, ampliado ou não, é registrado no log.
    """
    cfg = config["grafico"]
    largura = int(config["saida"]["largura"])
    altura = int(altura_alvo or config["saida"]["altura"])

    def contido(w, h):
        escala = min(largura / float(w), altura / float(h))
        return int(round(w * escala)), int(round(h * escala))

    cw, ch = contido(src_w, src_h)
    plano = {**segmento, "recorte": None, "largura_final": cw,
             "altura_final": ch, "ampliado": False}

    minimo = float(cfg["largura_minima"]) * largura
    caixa = segmento.get("caixa")
    if cw < minimo and caixa:
        margem = 0.03
        x0 = max(0.0, caixa[0] - margem)
        y0 = max(0.0, caixa[1] - margem)
        x1 = min(1.0, caixa[2] + margem)
        y1 = min(1.0, caixa[3] + margem)
        rw, rh = max(2, int((x1 - x0) * src_w)), max(2, int((y1 - y0) * src_h))
        nw, nh = contido(rw, rh)
        if nw > cw:
            plano.update(
                recorte={"x": int(x0 * src_w), "y": int(y0 * src_h),
                         "w": rw - rw % 2, "h": rh - rh % 2},
                largura_final=nw, altura_final=nh, ampliado=True)
            log.info("Gráfico pequeno (%dpx < %dpx): ampliado para %dpx "
                     "recortando só a margem vazia.", cw, int(minimo), nw)
        else:
            log.info("Gráfico pequeno (%dpx < %dpx) e sem ganho possível.",
                     cw, int(minimo))
    elif cw < minimo:
        log.info("Gráfico pequeno (%dpx < %dpx) e sem caixa de conteúdo.",
                 cw, int(minimo))
    return plano


def planejar(video_path, segmentos, config, altura_alvo=None, ffmpeg="ffmpeg",
             executar=None):
    """Segmentos + geometria. Falha ao ler a resolução => sem segmentos."""
    if not segmentos:
        return []
    try:
        src_w, src_h = dimensoes_do_arquivo(video_path, ffmpeg, executar)
    except Exception as e:
        log.warning("Sem resolução da fonte (%s); sem modo gráfico.", e)
        return []
    return [plano_do_segmento(s, config, src_w, src_h, altura_alvo)
            for s in segmentos]


# --- filtergraph --------------------------------------------------------------

def _cor_ffmpeg(valor):
    return "0x" + str(valor).strip().lstrip("#")[:6].upper()


def prefixo_split(n_segmentos, rotulo_base="[gorig]"):
    """Divide [0:v] em (1 + n) cópias: a do reframe e uma por segmento."""
    saidas = rotulo_base + "".join(f"[gsrc{i}]" for i in range(n_segmentos))
    return f"[0:v]split={1 + n_segmentos}{saidas};"


def filtro_segmento(config, indice, seg, altura_alvo=None, duracao_s=None):
    """Grafo de UM segmento: sai em [vg<i>] com alfa que entra e sai suave."""
    cfg = config["grafico"]
    largura = int(config["saida"]["largura"])
    altura = int(altura_alvo or config["saida"]["altura"])
    fps = int(config["saida"]["fps"])
    a, b = float(seg["inicio_s"]), float(seg["fim_s"])
    if duracao_s:
        b = min(b, float(duracao_s))
    d = max(0.05, min(float(cfg["transicao_s"]), (b - a) / 2.0))

    recorte = seg.get("recorte")
    corte = (f"crop={recorte['w']}:{recorte['h']}:{recorte['x']}:{recorte['y']},"
             if recorte else "")
    entrada = (f"[gsrc{indice}]trim=start={a:.3f}:end={b:.3f},"
               f"setpts=PTS-STARTPTS+{a:.3f}/TB,fps={fps},{corte}")
    fit = (f"scale={largura}:{altura}:force_original_aspect_ratio=decrease,"
           "setsar=1")
    fade = (f"format=yuva420p,fade=t=in:st={a:.3f}:d={d:.3f}:alpha=1,"
            f"fade=t=out:st={b - d:.3f}:d={d:.3f}:alpha=1")

    if cfg["fundo"] == "solido":
        cor = _cor_ffmpeg(cfg["cor_solida"])
        return (f"{entrada}{fit},"
                f"pad={largura}:{altura}:(ow-iw)/2:(oh-ih)/2:color={cor},"
                f"{fade}[vg{indice}];")

    sigma = float(cfg["desfoque_sigma"])
    # Fundo: reduz ANTES de borrar (blur em 1080x1920 custaria quase todo o
    # tempo do render), amplia de volta e escurece para o conteúdo saltar.
    return (
        f"{entrada}split=2[gf{indice}][gb{indice}];"
        f"[gb{indice}]scale={largura // 10}:{altura // 10}:"
        "force_original_aspect_ratio=increase,"
        f"crop={largura // 10}:{altura // 10},gblur=sigma={sigma / 10:.2f},"
        f"scale={largura}:{altura}:flags=bicubic,eq=brightness=-0.12[gbb{indice}];"
        f"[gf{indice}]{fit}[gff{indice}];"
        f"[gbb{indice}][gff{indice}]overlay=(W-w)/2:(H-h)/2,{fade}[vg{indice}];"
    )


def filtro_composicao(base="[vrbase]", saida="[vr]", n_segmentos=0):
    """Empilha os [vg<i>] sobre a base, sempre devolvendo o rótulo final."""
    if not n_segmentos:
        return ""
    trecho, atual = "", base
    for i in range(n_segmentos):
        proximo = saida if i == n_segmentos - 1 else f"[vgc{i}]"
        trecho += (f"{atual}[vg{i}]overlay=0:0:eof_action=pass:"
                   f"format=auto{',setsar=1' if i == n_segmentos - 1 else ''}"
                   f"{proximo};")
        atual = proximo
    return trecho
