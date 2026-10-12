"""Capa (thumbnail) do clip: o melhor quadro do trecho com o título por cima.

Gera, ao lado do .mp4, uma pasta `<nome>_capa/` com

    capa_1080x1920.png   vertical (Shorts / Reels / TikTok)
    capa_1280x720.png    horizontal (thumbnails.set do YouTube)

O quadro é escolhido, não sorteado: o trecho é amostrado e cada quadro recebe
uma pontuação (nitidez pela variância do Laplaciano, rosto via OpenCV, brilho
aceitável, e penalidade para quadro de corte). Sem OpenCV ou sem detector de
rosto o módulo degrada para nitidez + brilho; sem nada disso o chamador cai no
quadro do meio do trecho. Falha aqui NUNCA derruba o render: a capa é um
acessório do clip.

O texto vai pelo mesmo caminho da legenda (filtro `ass` do ffmpeg, com o
diretório de trabalho na pasta do .ass), então a fonte e o escape se comportam
exatamente como no resto do projeto.

Limitação conhecida (ver README): a Shorts shelf do YouTube usa um quadro do
próprio vídeo, e `thumbnails.set` só vale para o vídeo na página de exibição.
A alternativa é `capa.frame_inicial`, que põe o quadro de título nos primeiros
~0,3 s do vídeo — o quadro que a plataforma tende a pegar como capa.
"""
import logging
import os
import re
import subprocess
import textwrap

log = logging.getLogger(__name__)

LARGURA_AMOSTRA = 384
ALTURA_AMOSTRA = 216
FPS_AMOSTRA = 4.0
MAX_AMOSTRAS = 160
MARGEM_BORDA_S = 0.4          # fade de abertura/fecho nunca vira capa

# Limiares em escala 0-255 (diferença média absoluta entre amostras vizinhas).
LIMIAR_CORTE = 28.0
BRILHO_MIN = 35.0
BRILHO_BOM = (60.0, 200.0)
NITIDEZ_REF = 250.0           # variância do Laplaciano que vale nota cheia

_SEGURO = re.compile(r"[^A-Za-z0-9_.-]")


class ErroCapa(Exception):
    """Não foi possível produzir a capa."""


# --- pontuação ----------------------------------------------------------------

def nitidez(cinza):
    """Variância do Laplaciano: o clássico detector de foco."""
    import cv2
    return float(cv2.Laplacian(cinza, cv2.CV_64F).var())


def _detector_de_rosto():
    """Função frame_bgr -> [(x, y, w, h)], ou None sem OpenCV/cascata."""
    try:
        import cv2
        caminho = os.path.join(cv2.data.haarcascades,
                               "haarcascade_frontalface_default.xml")
        cascata = cv2.CascadeClassifier(caminho)
        if cascata.empty():
            return None
    except Exception as e:      # sem cv2, sem a pasta de dados
        log.debug("Sem detector de rosto: %s", e)
        return None

    def detectar(frame):
        cinza = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        achados = cascata.detectMultiScale(
            cinza, scaleFactor=1.15, minNeighbors=5,
            minSize=(frame.shape[1] // 14, frame.shape[0] // 8))
        return [tuple(int(v) for v in a) for a in achados]
    return detectar


def pontuar_quadros(quadros, detectar_rosto=None):
    """Nota 0..~3 de cada quadro. Função pura sobre arrays.

    `quadros` é uma lista de dicts {t, bgr}. Devolve a MESMA lista com as
    chaves: nitidez, brilho, rosto (area relativa do maior rosto, 0 = nenhum),
    centro_rosto (x relativo ou None), corte (bool), nota.
    """
    import cv2
    import numpy as np

    if not quadros:
        return []
    cinzas = [cv2.cvtColor(q["bgr"], cv2.COLOR_BGR2GRAY) for q in quadros]
    nit = [nitidez(c) for c in cinzas]
    maior_nit = max(max(nit), 1e-6)

    saida = []
    for i, q in enumerate(quadros):
        cinza = cinzas[i]
        brilho = float(cinza.mean())
        vizinhos = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(cinzas):
                vizinhos.append(float(np.mean(np.abs(
                    cinza.astype("int16") - cinzas[j].astype("int16")))))
        corte = bool(vizinhos) and max(vizinhos) > LIMIAR_CORTE

        area, centro = 0.0, None
        if detectar_rosto is not None:
            h, w = cinza.shape[:2]
            rostos = detectar_rosto(q["bgr"])
            if rostos:
                x, y, rw, rh = max(rostos, key=lambda r: r[2] * r[3])
                area = (rw * rh) / float(w * h)
                centro = (x + rw / 2.0) / w

        # Nitidez relativa ao melhor quadro do TRECHO (um podcast com câmera
        # parada e um vídeo de ação não compartilham a mesma régua absoluta),
        # limitada pela régua absoluta para quadro todo borrado não ganhar
        # só por ser o "menos pior".
        n_rel = nit[i] / maior_nit
        n_abs = min(1.0, nit[i] / NITIDEZ_REF)
        pontos_nitidez = 0.5 * n_rel + 0.5 * n_abs

        if brilho < BRILHO_MIN:
            pontos_brilho = 0.0
        elif BRILHO_BOM[0] <= brilho <= BRILHO_BOM[1]:
            pontos_brilho = 1.0
        elif brilho < BRILHO_BOM[0]:
            pontos_brilho = (brilho - BRILHO_MIN) / (BRILHO_BOM[0] - BRILHO_MIN)
        else:
            pontos_brilho = max(0.0, (255.0 - brilho) / (255.0 - BRILHO_BOM[1]))

        pontos_rosto = min(1.0, area / 0.06) if area else 0.0
        nota = 1.2 * pontos_nitidez + 1.5 * pontos_rosto + 0.6 * pontos_brilho
        if corte:
            nota -= 2.0
        if brilho < BRILHO_MIN:
            nota -= 2.0

        saida.append({**q, "nitidez": nit[i], "brilho": brilho, "rosto": area,
                      "centro_rosto": centro, "corte": corte, "nota": nota})
    return saida


def escolher_melhor(pontuados):
    """O quadro de maior nota; empate fica com o mais cedo. None se vazio."""
    if not pontuados:
        return None
    return max(pontuados, key=lambda q: (round(q["nota"], 6), -q["t"]))


# --- amostragem ---------------------------------------------------------------

def amostrar(video_path, inicio_s, fim_s, ffmpeg="ffmpeg", executar=None,
             fps=None):
    """Quadros BGR pequenos do trecho (padrão FPS_AMOSTRA), via um pipe."""
    import numpy as np

    executar = executar or subprocess.run
    duracao = float(fim_s) - float(inicio_s)
    if duracao <= 0:
        return []
    fps = min(float(fps or FPS_AMOSTRA), MAX_AMOSTRAS / duracao)
    r = executar(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{inicio_s:.3f}",
         "-t", f"{duracao:.3f}", "-i", video_path, "-an",
         "-vf", (f"fps={fps:.4f},scale={LARGURA_AMOSTRA}:{ALTURA_AMOSTRA}:"
                 "force_original_aspect_ratio=decrease,"
                 f"pad={LARGURA_AMOSTRA}:{ALTURA_AMOSTRA}:(ow-iw)/2:(oh-ih)/2"),
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise ErroCapa(f"ffmpeg falhou ao amostrar quadros: "
                       f"{(r.stderr or b'')[-200:]!r}")
    tamanho = LARGURA_AMOSTRA * ALTURA_AMOSTRA * 3
    dados = r.stdout or b""
    n = len(dados) // tamanho
    quadros = []
    for i in range(n):
        bruto = dados[i * tamanho:(i + 1) * tamanho]
        quadros.append({
            "t": float(inicio_s) + (i + 0.5) / fps,
            "bgr": np.frombuffer(bruto, dtype="uint8").reshape(
                ALTURA_AMOSTRA, LARGURA_AMOSTRA, 3),
        })
    return quadros


def melhor_quadro(video_path, inicio_s, fim_s, ffmpeg="ffmpeg", executar=None,
                  detectar_rosto="auto"):
    """(instante_s, info) do melhor quadro; cai no meio do trecho sem amostra."""
    meio = (float(inicio_s) + float(fim_s)) / 2.0
    try:
        quadros = amostrar(video_path, inicio_s, fim_s, ffmpeg, executar)
    except Exception as e:
        log.warning("Amostragem de quadros falhou (%s); capa no meio do trecho.", e)
        return meio, {"motivo": "sem_amostra"}
    miolo = [q for q in quadros
             if inicio_s + MARGEM_BORDA_S <= q["t"] <= fim_s - MARGEM_BORDA_S]
    quadros = miolo or quadros
    if not quadros:
        return meio, {"motivo": "sem_amostra"}
    if detectar_rosto == "auto":
        detectar_rosto = _detector_de_rosto()
    melhor = escolher_melhor(pontuar_quadros(quadros, detectar_rosto))
    return melhor["t"], {
        "motivo": "pontuado", "nota": round(melhor["nota"], 3),
        "nitidez": round(melhor["nitidez"], 1), "rosto": round(melhor["rosto"], 4),
        "centro_rosto": melhor["centro_rosto"], "brilho": round(melhor["brilho"], 1),
    }


# --- texto --------------------------------------------------------------------

def quebrar_titulo(titulo, largura_px, maximo_linhas=4, tamanho_max=150,
                   tamanho_min=70, fator_largura=0.62, ocupacao=0.86):
    """(texto_com_\\N, tamanho_da_fonte) que cabe em `largura_px`.

    Tenta quebrar em largura de caracteres crescente e fica com a combinação
    de MAIOR fonte que ainda cabe em `maximo_linhas`. Função pura.
    """
    texto = " ".join(str(titulo or "").split()).upper()
    if not texto:
        return "", tamanho_min
    util = largura_px * ocupacao
    melhor = None
    for largura_car in range(8, 31):
        linhas = textwrap.wrap(texto, width=largura_car, break_long_words=True)
        if len(linhas) > maximo_linhas:
            continue
        maior = max(len(l) for l in linhas)
        tamanho = min(tamanho_max, util / (fator_largura * maior))
        if tamanho < tamanho_min:
            continue
        if melhor is None or tamanho > melhor[1] + 1e-6:
            melhor = (linhas, tamanho)
    if melhor is None:      # título longo demais: corta, mas nunca estoura a tela
        palavras = texto.split()
        while palavras and len(" ".join(palavras)) > maximo_linhas * 18:
            palavras.pop()
        linhas = textwrap.wrap(" ".join(palavras) or texto[:40], width=18)
        melhor = (linhas[:maximo_linhas], tamanho_min)
    return "\\N".join(melhor[0]), int(melhor[1])


def _sanear_titulo(texto):
    """Tira o que o ASS interpretaria como controle ({}, barra invertida)."""
    return (str(texto or "").replace("\\", " ").replace("{", "(")
            .replace("}", ")").replace("\n", " "))


def ass_do_titulo(titulo, largura, altura, config_capa, y_rel=0.20,
                  tamanho_max=None, maximo_linhas=4):
    """Documento .ass com o título grande, caixa escura e contorno grosso."""
    from editing.template import cor_ass

    texto, tamanho = quebrar_titulo(
        _sanear_titulo(titulo), largura, maximo_linhas=maximo_linhas,
        tamanho_max=tamanho_max or int(config_capa.get("tamanho_max", 130)),
        tamanho_min=60 * largura // 1080)
    cor = cor_ass(config_capa.get("cor", "#FFFFFF"))
    # BorderStyle 3 (caixa opaca): o libass pinta a caixa com a OutlineColour.
    caixa = cor_ass(config_capa.get("caixa_cor", "#000000B0"))
    sombra = cor_ass("#00000000")
    espessura = max(2, int(config_capa.get("contorno_espessura", 8)) * largura // 1080)
    fonte = str(config_capa.get("fonte", "Arial"))
    estilo = (
        f"Style: Capa,{fonte},{tamanho},{cor},{cor},{caixa},{sombra},-1,0,0,0,"
        f"100,100,0,0,3,{espessura},0,5,60,60,0,1"
    )
    pos = f"{{\\an5\\pos({largura // 2},{int(altura * y_rel)})}}"
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {largura}\nPlayResY: {altura}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{estilo}\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:10.00,Capa,,0,0,0,,{pos}{texto}\n"
    )


# --- imagem -------------------------------------------------------------------

def _filtro_recorte(largura, altura, centro_rosto, ancora):
    """Recorte que cobre largura x altura, centrado no rosto quando há."""
    proporcao = largura / float(altura)
    cx = centro_rosto if centro_rosto is not None else ancora
    cx = min(max(float(cx), 0.0), 1.0)
    return (
        f"crop=w='min(iw,ih*{proporcao:.6f})':h='min(ih,iw/{proporcao:.6f})':"
        f"x='clip({cx:.4f}*iw-ow/2,0,iw-ow)':y='(ih-oh)/2',"
        f"scale={largura}:{altura},setsar=1"
    )


def _renderizar_png(video_path, t, largura, altura, titulo, config_capa,
                    saida_png, centro_rosto, ancora, ffmpeg, executar,
                    **estilo_titulo):
    pasta = os.path.dirname(os.path.abspath(saida_png))
    nome_ass = os.path.splitext(os.path.basename(saida_png))[0] + ".ass"
    with open(os.path.join(pasta, nome_ass), "w", encoding="utf-8") as f:
        f.write(ass_do_titulo(titulo, largura, altura, config_capa,
                              **estilo_titulo))
    try:
        filtro = _filtro_recorte(largura, altura, centro_rosto, ancora)
        if titulo:
            filtro += f",ass={nome_ass}"
        r = executar(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", video_path, "-frames:v", "1",
             "-vf", filtro, "-update", "1", os.path.abspath(saida_png)],
            capture_output=True, text=True, cwd=pasta,
        )
    finally:
        try:
            os.remove(os.path.join(pasta, nome_ass))
        except OSError:
            pass
    if r.returncode != 0 or not os.path.exists(saida_png):
        ultima = (r.stderr or "").strip().splitlines()
        raise ErroCapa(f"ffmpeg falhou na capa {largura}x{altura}: "
                       f"{ultima[-1] if ultima else 'sem stderr'}")
    return saida_png


def titulo_do_clip(linha, limite_palavras=7):
    """Título curto para a capa: hook do LLM; senão motivo; senão título-fonte."""
    for chave in ("hook_text", "motivo", "titulo"):
        try:
            valor = (linha[chave] or "").strip()
        except (KeyError, IndexError):
            valor = ""
        if valor:
            palavras = valor.split()
            return " ".join(palavras[:limite_palavras])
    return ""


def pasta_da_capa(destino_dir, nome):
    return os.path.join(destino_dir, f"{_SEGURO.sub('_', nome)}_capa")


def gerar(config, video_path, inicio_s, fim_s, titulo, destino_dir, nome,
          ffmpeg="ffmpeg", executar=None, detectar_rosto="auto"):
    """Escolhe o quadro e escreve as duas capas.

    Devolve {vertical, horizontal, instante_s, pontuacao}; levanta ErroCapa.
    O chamador decide se a falha é fatal (nunca é).
    """
    executar = executar or subprocess.run
    cfg = config.get("capa", {})
    saida = config["saida"]
    ancora = float(config.get("reframe", {}).get("ancora_horizontal", 0.5))
    pasta = pasta_da_capa(destino_dir, nome)
    os.makedirs(pasta, exist_ok=True)

    t, info = melhor_quadro(video_path, inicio_s, fim_s, ffmpeg, executar,
                            detectar_rosto)
    vertical = _renderizar_png(
        video_path, t, int(saida["largura"]), int(saida["altura"]), titulo, cfg,
        os.path.join(pasta, f"capa_{saida['largura']}x{saida['altura']}.png"),
        info.get("centro_rosto"), ancora, ffmpeg, executar)
    horizontal = _renderizar_png(
        video_path, t, 1280, 720, titulo, cfg,
        os.path.join(pasta, "capa_1280x720.png"),
        None, 0.5, ffmpeg, executar,
        y_rel=0.80, tamanho_max=96, maximo_linhas=3)
    log.info("Capa de %s: quadro em %.2fs (%s).", nome, t, info.get("motivo"))
    return {"vertical": vertical, "horizontal": horizontal,
            "instante_s": t, "pontuacao": info}
