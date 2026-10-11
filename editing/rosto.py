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
