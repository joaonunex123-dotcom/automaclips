"""Reframe 16:9 -> 9:16 que segue o rosto, em vez de cortar sempre no centro.

Três etapas, separadas para que só a primeira precise de OpenCV e de vídeo:

1. `amostrar` — lê o trecho do vídeo-fonte e devolve, a `amostras_por_s`, a
   posição horizontal do maior rosto (fração 0-1 da largura) ou None.
2. `planejar` — função pura: das amostras tira uma trilha de poucos
   movimentos, em coordenadas de âncora (a mesma semântica de
   `reframe.ancora_horizontal`: 0 = janela à esquerda, 1 = à direita).
3. `aplicar` — devolve uma cópia do config com a trilha em `reframe.trilha`;
   é o que `render.filtro_reframe` lê. Nenhuma assinatura de render muda.

Duas decisões contra o tremor, que é o defeito clássico deste recurso:

* **Zona morta.** A janela só se move quando o rosto sai de dentro dela por
  mais que `zona_morta` (fração da largura da janela). Falar balançando a
  cabeça não vira câmera balançando.
* **Poucos movimentos, e suaves.** Cada movimento é uma rampa de
  `transicao_s`, e o total é limitado por `max_movimentos` para a expressão do
  ffmpeg ficar curta.

Sem rosto confiável (menos de `minimo_deteccoes` das amostras), nada muda: o
clip sai com a âncora fixa do template, exatamente como sem o recurso.
"""
import copy
import logging
import statistics

log = logging.getLogger(__name__)

_LARGURA_DETECCAO = 480


class ErroRosto(Exception):
    """OpenCV ausente ou vídeo ilegível."""


def amostrar(video_path, inicio_s, fim_s, amostras_por_s=2.0, abrir=None):
    """([(t_relativo_ao_clip, centro_x_0a1 | None), ...], (largura, altura)).

    `abrir` injeta o capture (testes). O padrão usa cv2.VideoCapture e o
    classificador frontal que vem dentro do pacote do OpenCV — nada é baixado.
    """
    try:
        import cv2
    except ImportError as e:
        raise ErroRosto(
            "reframe.rosto.ativo exige opencv-python-headless "
            "(pip install 'opencv-python-headless<5')"
        ) from e

    captura = (abrir or cv2.VideoCapture)(video_path)
    if not captura.isOpened():
        raise ErroRosto(f"não consegui abrir {video_path}")
    try:
        largura = int(captura.get(cv2.CAP_PROP_FRAME_WIDTH))
        altura = int(captura.get(cv2.CAP_PROP_FRAME_HEIGHT))
        detector = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
        if detector.empty():
            raise ErroRosto("classificador de rosto não carregou")

        passo = 1.0 / max(0.1, float(amostras_por_s))
        amostras = []
        t = 0.0
        while t < fim_s - inicio_s:
            captura.set(cv2.CAP_PROP_POS_MSEC, (inicio_s + t) * 1000.0)
            ok, quadro = captura.read()
            if not ok:
                break
            amostras.append((round(t, 3), _centro_do_rosto(cv2, detector, quadro)))
            t += passo
        return amostras, (largura, altura)
    finally:
        captura.release()


def _centro_do_rosto(cv2, detector, quadro):
    altura, largura = quadro.shape[:2]
    escala = min(1.0, _LARGURA_DETECCAO / float(largura))
    if escala < 1.0:
        quadro = cv2.resize(quadro, (int(largura * escala), int(altura * escala)))
    cinza = cv2.cvtColor(quadro, cv2.COLOR_BGR2GRAY)
    caixas = detector.detectMultiScale(
        cinza, scaleFactor=1.1, minNeighbors=5,
        minSize=(int(cinza.shape[1] * 0.04),) * 2)
    if len(caixas) == 0:
        return None
    x, _, w, _ = max(caixas, key=lambda c: c[2] * c[3])     # o maior = o locutor
    return (x + w / 2.0) / cinza.shape[1]


def ancora_para_centro(centro, largura, altura, proporcao):
    """Âncora (0-1) da janela 9:16 que deixa `centro` (0-1 da largura) no meio.

    A janela tem a largura de `min(iw, ih*proporcao)`; a âncora do crop é
    x / (iw - ow), então janela cheia (iw == ow) não tem para onde andar.
    """
    janela = min(float(largura), altura * proporcao)
    livre = largura - janela
    if livre <= 0:
        return 0.5
    x = centro * largura - janela / 2.0
    return min(1.0, max(0.0, x / livre))


def planejar(amostras, dimensoes, config):
    """Trilha [(t, ancora), ...] com a âncora inicial em t=0, ou None.

    None = sem rosto suficiente; o chamador mantém a âncora do template.
    """
    cfg = config["reframe"]["rosto"]
    largura, altura = dimensoes
    saida = config["saida"]
    proporcao = float(saida["largura"]) / float(saida["altura"])

    validas = [(t, c) for t, c in amostras if c is not None]
    if not amostras or len(validas) / len(amostras) < float(cfg["minimo_deteccoes"]):
        return None

    # Âncora de cada instante; buraco (rosto não visto) herda o último valor.
    ancoras, ultimo = [], None
    for t, c in amostras:
        if c is not None:
            ultimo = ancora_para_centro(c, largura, altura, proporcao)
        ancoras.append((t, ultimo))
    primeiro = next(a for _, a in ancoras if a is not None)
    ancoras = [(t, primeiro if a is None else a) for t, a in ancoras]

    # Mediana móvel (3) contra detecções espúrias isoladas.
    suave = []
    for i, (t, _) in enumerate(ancoras):
        janela = [a for _, a in ancoras[max(0, i - 1): i + 2]]
        suave.append((t, statistics.median(janela)))

    zona = float(cfg["zona_morta"])
    atual = suave[0][1]
    trilha = [(0.0, atual)]
    for t, a in suave[1:]:
        if abs(a - atual) > zona:
            trilha.append((t, a))
            atual = a

    limite = int(cfg["max_movimentos"]) + 1
    if len(trilha) > limite:
        # Fica com os saltos maiores, em ordem de tempo.
        saltos = sorted(
            range(1, len(trilha)),
            key=lambda i: abs(trilha[i][1] - trilha[i - 1][1]), reverse=True,
        )[: limite - 1]
        trilha = [trilha[0]] + [trilha[i] for i in sorted(saltos)]
    return trilha


def expressao_ancora(trilha, transicao_s):
    """Expressão do ffmpeg (variável `t`) para a âncora ao longo do clip.

    a0 + soma de (delta_i * clip((t - t_i)/transicao, 0, 1)): cada movimento é
    uma rampa linear, e as rampas somam.
    """
    termos = [f"{trilha[0][1]:.4f}"]
    for (_, anterior), (t, a) in zip(trilha, trilha[1:]):
        delta = a - anterior
        if abs(delta) < 1e-4:
            continue
        termos.append(
            f"({delta:+.4f})*clip((t-{t:.3f})/{max(0.05, transicao_s):.3f},0,1)")
    return "+".join(termos)


def aplicar(config, video_path, inicio_s, fim_s, amostrar_fn=None):
    """Config com `reframe.trilha` preenchida, ou o próprio config.

    Só age com reframe.modo == "corte" e reframe.rosto.ativo. Falha do
    detector degrada para o corte fixo, com aviso: render sem seguir o rosto é
    melhor do que clip nenhum.
    """
    reframe = config["reframe"]
    cfg = reframe.get("rosto") or {}
    if reframe["modo"] != "corte" or not cfg.get("ativo"):
        return config
    try:
        amostras, dimensoes = (amostrar_fn or amostrar)(
            video_path, inicio_s, fim_s, cfg["amostras_por_s"])
    except ErroRosto as e:
        log.warning("Reframe com rosto desligado neste clip: %s", e)
        return config
    except Exception as e:      # cv2.error, quadro corrompido: nunca derruba o render
        log.warning("Reframe com rosto desligado neste clip (falha do detector): %r", e)
        return config
    trilha = planejar(amostras, dimensoes, config)
    if trilha is None:
        log.info("Sem rosto confiável em %s: âncora fixa do template.", video_path)
        return config
    efetivo = copy.deepcopy(config)
    efetivo["reframe"]["trilha"] = trilha
    return efetivo


# --- várias pessoas no quadro ---------------------------------------------------
#
# Com 2+ pessoas distantes, nenhum crop 9:16 as contém: a janela cai no vão e
# corta as duas. Nesses trechos o render mostra o quadro INTEIRO (o mesmo
# fit/contain do modo gráfico, em editing/grafico.py). Três etapas, as duas
# últimas puras:
#
# 1. `amostrar_multi` — TODOS os rostos confiáveis por amostra, caixas 0-1.
# 2. `classificar` — por amostra: caixa envolvente cabe na janela? "single"
#    (com o centro da caixa) ou "multi".
# 3. `histerese` + `segmentos` — o modo só troca depois de N amostras seguidas,
#    para não piscar entre os dois enquadramentos.

_LARGURA_MULTI = 640
_CASCATAS = (
    # (arquivo, aceita sozinha, espelhar). O perfil do OpenCV só vê rostos
    # virados para um lado: roda também no quadro espelhado.
    ("haarcascade_frontalface_alt2.xml", True, False),
    ("haarcascade_frontalface_default.xml", False, False),
    ("haarcascade_profileface.xml", False, True),
)
_MARGEM_ROSTO = 0.4     # de cada lado da caixa envolvente, em larguras de rosto
_IOU_FUSAO = 0.3


def _iou(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = min(ax1, bx1) - max(ax0, bx0)
    ih = min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / ((ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter)


def fundir_deteccoes(por_detector, aceita_sozinha):
    """Caixas [(x0,y0,x1,y1), ...] confiáveis de várias listas de detecção.

    Uma caixa conta quando a cascata que a achou é precisa o bastante para
    valer sozinha, ou quando outra cascata a confirma (IoU >= 0.3). Caixas que
    se sobrepõem viram uma só (a maior). Função pura.
    """
    candidatas = []     # (caixa, idx_detector)
    for i, caixas in enumerate(por_detector):
        candidatas += [(tuple(c), i) for c in caixas]
    confiaveis = []
    for caixa, i in candidatas:
        apoios = {j for c, j in candidatas if j != i and _iou(caixa, c) >= _IOU_FUSAO}
        if aceita_sozinha[i] or apoios:
            confiaveis.append(caixa)
    confiaveis.sort(key=lambda c: -(c[2] - c[0]) * (c[3] - c[1]))
    saida = []
    for caixa in confiaveis:
        if all(_iou(caixa, s) < _IOU_FUSAO for s in saida):
            saida.append(caixa)
    return sorted(saida)


def amostrar_multi(video_path, inicio_s, fim_s, amostras_por_s=2.0,
                   min_rosto=0.06, abrir=None):
    """([(t, [caixa 0-1, ...]), ...], (largura, altura)) com TODOS os rostos.

    Só entram rostos com altura >= `min_rosto` do quadro: rosto de fundo e de
    plateia não decide o enquadramento. Usa só as cascatas Haar que vêm no
    pacote do OpenCV (nada é baixado).
    """
    try:
        import cv2
    except ImportError as e:
        raise ErroRosto(
            "layout multi exige opencv-python-headless "
            "(pip install 'opencv-python-headless<5')") from e

    detectores = []
    for arquivo, _sozinha, espelhar in _CASCATAS:
        det = cv2.CascadeClassifier(cv2.data.haarcascades + arquivo)
        if det.empty():
            raise ErroRosto(f"classificador {arquivo} não carregou")
        detectores.append((det, espelhar))
    aceita = [c[1] for c in _CASCATAS]

    captura = (abrir or cv2.VideoCapture)(video_path)
    if not captura.isOpened():
        raise ErroRosto(f"não consegui abrir {video_path}")
    try:
        largura = int(captura.get(cv2.CAP_PROP_FRAME_WIDTH))
        altura = int(captura.get(cv2.CAP_PROP_FRAME_HEIGHT))
        passo = 1.0 / max(0.1, float(amostras_por_s))
        amostras, t = [], 0.0
        while t < fim_s - inicio_s:
            captura.set(cv2.CAP_PROP_POS_MSEC, (inicio_s + t) * 1000.0)
            ok, quadro = captura.read()
            if not ok:
                break
            amostras.append(
                (round(t, 3), _rostos_do_quadro(cv2, detectores, aceita,
                                                quadro, min_rosto)))
            t += passo
        return amostras, (largura, altura)
    finally:
        captura.release()


def _rostos_do_quadro(cv2, detectores, aceita, quadro, min_rosto):
    altura, largura = quadro.shape[:2]
    escala = min(1.0, _LARGURA_MULTI / float(largura))
    if escala < 1.0:
        quadro = cv2.resize(quadro, (int(largura * escala), int(altura * escala)))
    cinza = cv2.equalizeHist(cv2.cvtColor(quadro, cv2.COLOR_BGR2GRAY))
    h, w = cinza.shape[:2]
    minimo = max(20, int(h * float(min_rosto)))
    listas = []
    for det, espelhar in detectores:
        imagem = cv2.flip(cinza, 1) if espelhar else cinza
        caixas = det.detectMultiScale(
            imagem, scaleFactor=1.1, minNeighbors=6, minSize=(minimo, minimo))
        normalizadas = []
        for x, y, bw, bh in caixas:
            if espelhar:
                x = w - x - bw
            normalizadas.append((x / w, y / h, (x + bw) / w, (y + bh) / h))
        listas.append(normalizadas)
    caixas = fundir_deteccoes(listas, aceita)
    return [c for c in caixas if (c[3] - c[1]) >= float(min_rosto)]


def classificar(amostras, dimensoes, proporcao, margem=_MARGEM_ROSTO):
    """Uma entrada por amostra: dict(t, modo, centro, n, caixa) ou modo None.

    modo "single" = a caixa envolvente (rostos + margem) cabe na janela 9:16;
    `centro` é o centro dela (0-1 da largura) e é onde a janela deve ficar —
    nunca a média entre pessoas distantes. modo "multi" = não cabe. Sem rosto
    nenhum: modo None (não vota na histerese). Função pura.
    """
    largura, altura = dimensoes
    janela = min(float(largura), float(altura) * float(proporcao)) / float(largura)
    saida = []
    for t, caixas in amostras:
        if not caixas:
            saida.append({"t": t, "modo": None, "centro": None, "n": 0,
                          "caixa": None})
            continue
        larg_media = sum(c[2] - c[0] for c in caixas) / len(caixas)
        x0 = max(0.0, min(c[0] for c in caixas) - margem * larg_media)
        x1 = min(1.0, max(c[2] for c in caixas) + margem * larg_media)
        y0 = min(c[1] for c in caixas)
        y1 = max(c[3] for c in caixas)
        modo = "single" if (x1 - x0) <= janela + 1e-9 else "multi"
        saida.append({"t": t, "modo": modo, "centro": (x0 + x1) / 2.0,
                      "n": len(caixas), "caixa": (x0, y0, x1, y1)})
    return saida


def histerese(modos, entrada=3, saida=4):
    """Modo estável por amostra, a partir dos modos instantâneos.

    Só entra em "multi" depois de `entrada` amostras "multi" e só volta a
    "single" depois de `saida` amostras "single", sem nenhuma do modo oposto no
    meio. Amostra sem rosto (None) é neutra: não conta nem interrompe. A troca
    é retroativa: vale desde a primeira amostra da sequência que a confirmou.
    Função pura.
    """
    entrada, saida = max(1, int(entrada)), max(1, int(saida))
    estado, corrida, inicio = "single", 0, 0
    resultado = []
    for i, m in enumerate(modos):
        alvo = "multi" if estado == "single" else "single"
        if m == alvo:
            if corrida == 0:
                inicio = i
            corrida += 1
            if corrida >= (entrada if alvo == "multi" else saida):
                estado = alvo
                for k in range(inicio, i):
                    resultado[k] = alvo
                corrida = 0
        elif m == estado:
            corrida = 0
        resultado.append(estado)
    return resultado


def segmentos(classes, estados, passo_s, duracao_s, minimo_s=1.0):
    """Segmentos contíguos [{inicio_s, fim_s, modo, ancora_x, n_rostos, caixa}].

    `ancora_x` é o centro mediano dos rostos (0-1 da largura) nos trechos
    "single"; "multi" leva a união das caixas, que alimenta o recorte de
    ampliação do modo gráfico. Trechos "multi" curtos demais (< minimo_s)
    voltam a "single". Função pura.
    """
    n = len(classes)
    if n == 0:
        return []
    estados = list(estados)
    i = 0
    while i < n:
        j = i
        while j < n and estados[j] == estados[i]:
            j += 1
        if estados[i] == "multi" and (j - i) * passo_s < minimo_s:
            estados[i:j] = ["single"] * (j - i)
        i = j

    saida, i = [], 0
    while i < n:
        j = i
        while j < n and estados[j] == estados[i]:
            j += 1
        inicio = 0.0 if i == 0 else classes[i]["t"] - passo_s / 2.0
        fim = duracao_s if j == n else classes[j - 1]["t"] + passo_s / 2.0
        trecho = classes[i:j]
        caixas = [c["caixa"] for c in trecho if c["caixa"]]
        centros = sorted(c["centro"] for c in trecho
                         if c["centro"] is not None and c["modo"] == "single")
        saida.append({
            "inicio_s": round(max(0.0, inicio), 3),
            "fim_s": round(min(duracao_s, fim), 3),
            "modo": estados[i],
            "ancora_x": (statistics.median(centros)
                         if estados[i] == "single" and centros else None),
            "n_rostos": max((c["n"] for c in trecho), default=0),
            "caixa": ((min(c[0] for c in caixas), min(c[1] for c in caixas),
                       max(c[2] for c in caixas), max(c[3] for c in caixas))
                      if estados[i] == "multi" and caixas else None),
        })
        i = j
    return saida


def detectar_multi(video_path, inicio_s, fim_s, config, amostrar_fn=None):
    """Análise de várias pessoas. Nunca levanta.

    Devolve dict(segmentos, trilha, amostras, dimensoes) ou None quando nada
    é confiável (sem OpenCV, falha do detector, sem rosto algum): o chamador
    renderiza como antes.
    """
    cfg = config["multi"]
    saida = config["saida"]
    proporcao = float(saida["largura"]) / float(saida["altura"])
    duracao = float(fim_s) - float(inicio_s)
    if duracao <= 0:
        return None
    try:
        amostras, dimensoes = (amostrar_fn or amostrar_multi)(
            video_path, inicio_s, fim_s, cfg["amostras_por_s"], cfg["min_rosto"])
    except ErroRosto as e:
        log.warning("Modo multi desligado neste clip: %s", e)
        return None
    except Exception as e:      # cv2.error, quadro corrompido: nunca derruba o render
        log.warning("Modo multi desligado neste clip (falha do detector): %r", e)
        return None
    if not amostras or not any(caixas for _, caixas in amostras):
        log.info("Sem rosto confiável em %s: modo multi não age.", video_path)
        return None

    classes = classificar(amostras, dimensoes, proporcao)
    estados = histerese([c["modo"] for c in classes],
                        cfg["histerese_entrada"], cfg["histerese_saida"])
    passo = 1.0 / float(cfg["amostras_por_s"])
    segs = segmentos(classes, estados, passo, duracao)
    trilha = _trilha_single(classes, estados, dimensoes, config)
    return {"segmentos": segs, "trilha": trilha, "dimensoes": dimensoes,
            "amostras": [{"t": c["t"], "n": c["n"], "modo": c["modo"],
                          "estado": e, "centro": c["centro"]}
                         for c, e in zip(classes, estados)]}


def _trilha_single(classes, estados, dimensoes, config):
    """Trilha de âncora do crop, só com os instantes "single" (ver `planejar`)."""
    cfg = (config.get("reframe") or {}).get("rosto")
    if not cfg or config["reframe"]["modo"] != "corte":
        return None
    amostras = [(c["t"], c["centro"] if e == "single" else None)
                for c, e in zip(classes, estados)]
    return planejar(amostras, dimensoes, config)


def trocas_de_modo(segs):
    return max(0, len(segs) - 1)
