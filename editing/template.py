"""Carrega e valida o template_config.json.

Uma decisão importante do arquivo: as chaves são validadas na CARGA, não no
uso. Um `tamanho: "76"` (string) ou um alinhamento fora de 1–9 só apareceria
depois de baixar, transcrever, chamar o LLM e começar a renderizar — quatro
etapas caras depois do erro. Aqui ele aparece antes da primeira.

Chaves começadas por `_` são comentário do próprio JSON (o formato não tem
comentário) e são ignoradas em todos os níveis.
"""
import copy
import json
import logging
import os

import settings

log = logging.getLogger(__name__)


class TemplateInvalido(Exception):
    """template_config.json ausente, malformado ou com valor impossível."""


# Só o que o render REALMENTE precisa. Não é um espelho do arquivo: chaves a
# mais no JSON são permitidas (é como a etapa 4 vai acrescentar SFX sem mexer
# aqui), chaves a menos caem nestes defaults.
PADRAO = {
    "versao": "0",
    "saida": {
        "largura": 1080, "altura": 1920, "fps": 30,
        "codec_video": "libx264", "preset": "medium", "crf": 20,
        "codec_audio": "aac", "bitrate_audio": "192k",
    },
    "reframe": {"modo": "corte", "ancora_horizontal": 0.5, "desfoque_sigma": 30},
    "zoom": {"ativo": False, "fator_final": 1.08},
    "legenda": {
        "ativa": True, "fonte": "Arial", "tamanho": 76, "negrito": True,
        "cor": "#FFFFFF", "cor_destaque": "#FFD400", "contorno_cor": "#000000",
        "contorno_espessura": 6, "sombra": 2, "margem_vertical": 420,
        "alinhamento": 2, "palavras_por_linha": 3, "maiusculas": True,
    },
    "hook": {
        "ativo": True, "duracao_s": 1.0, "fonte": "Arial", "tamanho": 96,
        "negrito": True, "cor": "#FFFFFF", "contorno_cor": "#000000",
        "contorno_espessura": 8, "sombra": 2, "margem_vertical": 40,
        "alinhamento": 5, "maiusculas": True,
    },
    "watermark": {
        "ativo": False, "texto": "", "fonte": "Arial", "tamanho": 40,
        "negrito": False, "cor": "#FFFFFFB0", "contorno_cor": "#00000080",
        "contorno_espessura": 2, "sombra": 0, "margem_vertical": 60,
        "alinhamento": 8,
    },
    # Layout A/B. "full" é o clip tela cheia de sempre; "split" põe o clip em
    # cima e um vídeo de preenchimento embaixo (ver editing/layout.py).
    "layout": "full",
    "split": {
        "diretorio": "assets/filler", "categoria": "", "proporcao_clip": 0.6,
        "legenda": {"alinhamento": 2, "margem_vertical": 720},
        "separador": {"ativo": True, "espessura": 6, "cor": "#000000"},
    },
    # {"full": 1, "split:parkour": 1}: sorteio ponderado e estável por clip.
    # Vazio = todo clip usa `layout`.
    "variantes": {"pesos": {}, "semente": "ab1"},
    "sfx": {
        "ativo": False, "diretorio": "assets/sfx", "volume": 0.6,
        "espacamento_minimo_s": 1.5, "maximo_por_clip": 8,
        "eventos": {},
        "na_abertura": True, "no_fim_do_hook": True,
        "exclamacao_conta": True, "palavras_chave": [],
    },
    # Capa (thumbnail) do clip e quadro de título nos primeiros instantes.
    "capa": {
        "ativa": False, "fonte": "Arial", "tamanho_max": 130, "cor": "#FFFFFF",
        "caixa_cor": "#000000B0", "contorno_espessura": 8,
        "frame_inicial": {"ativo": False, "duracao_s": 0.3},
    },
    # Modo gráfico (ver editing/grafico.py): slide/gráfico/tabela da fonte
    # aparece inteiro, sobre fundo preenchido, em vez do crop 9:16 central.
    "grafico": {
        "ativo": False, "fundo": "desfoque", "cor_solida": "#101418",
        "desfoque_sigma": 30, "transicao_s": 0.25, "min_duracao_s": 1.5,
        "maximo_por_clip": 3, "amostras_por_s": 2.0, "largura_minima": 0.70,
    },
    # Efeitos de edição (ver editing/efeitos.py). Tudo desligado: com as flags
    # em false o filtergraph é o mesmo de antes de a seção existir.
    "efeitos": {
        "zoom": {
            "ativo": False, "modo": "picos", "fator": 1.08, "duracao_s": 0.6,
            "intervalo_s": 4.0, "espacamento_minimo_s": 2.0,
            "maximo_por_clip": 8,
        },
        "flash": {
            "ativo": False, "intensidade": 0.35, "duracao_s": 0.18,
            "na_abertura": True, "no_fim_do_hook": True,
        },
        "vinheta": {"ativo": False, "angulo": 0.6},
        "barra": {"ativo": False, "altura": 8, "cor": "#FFD400",
                  "posicao": "base"},
        "voz": {"ativo": False, "lufs": -16.0},
        "ducking": {"ativo": False, "limiar": 0.03, "razao": 8.0,
                    "ataque_ms": 5.0, "liberacao_ms": 250.0},
        "pad": {"ativo": False, "arquivo": "pad_{categoria}.wav",
                "volume": 1.0},
        "teto_sfx_abaixo_da_voz_db": 8.0,
    },
}

MODOS_REFRAME = ("corte", "desfoque")
LAYOUTS = ("full", "split")

# Gatilhos que editing/sfx.py sabe posicionar. Um som com gatilho fora desta
# lista é erro de configuração, não um som que simplesmente nunca toca: o
# segundo modo de falha é invisível e só apareceria ao assistir o clip.
GATILHOS_SFX = ("transicao", "pico", "palavra_chave")


def _sem_comentarios(valor):
    if isinstance(valor, dict):
        return {
            k: _sem_comentarios(v) for k, v in valor.items()
            if not k.startswith("_")
        }
    return valor


def _mesclar(padrao, informado):
    """Informado vence, chave a chave, em profundidade."""
    saida = copy.deepcopy(padrao)
    for chave, valor in informado.items():
        if isinstance(valor, dict) and isinstance(saida.get(chave), dict):
            saida[chave] = _mesclar(saida[chave], valor)
        else:
            saida[chave] = valor
    return saida


def _exigir_numero(config, secao, chave, minimo=None, maximo=None):
    valor = config[secao][chave]
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise TemplateInvalido(
            f"{secao}.{chave} precisa ser número, veio {valor!r}"
        )
    if minimo is not None and valor < minimo:
        raise TemplateInvalido(f"{secao}.{chave} precisa ser >= {minimo}, veio {valor}")
    if maximo is not None and valor > maximo:
        raise TemplateInvalido(f"{secao}.{chave} precisa ser <= {maximo}, veio {valor}")


def validar(config):
    """Levanta TemplateInvalido no primeiro problema. Devolve o config."""
    _exigir_numero(config, "saida", "largura", minimo=16)
    _exigir_numero(config, "saida", "altura", minimo=16)
    _exigir_numero(config, "saida", "fps", minimo=1)
    _exigir_numero(config, "saida", "crf", minimo=0, maximo=51)

    if config["reframe"]["modo"] not in MODOS_REFRAME:
        raise TemplateInvalido(
            f"reframe.modo precisa ser um de {MODOS_REFRAME}, "
            f"veio {config['reframe']['modo']!r}"
        )
    _exigir_numero(config, "reframe", "ancora_horizontal", minimo=0.0, maximo=1.0)
    _exigir_numero(config, "zoom", "fator_final", minimo=1.0)

    for secao in ("legenda", "hook", "watermark"):
        _exigir_numero(config, secao, "tamanho", minimo=1)
        _exigir_numero(config, secao, "alinhamento", minimo=1, maximo=9)
        _exigir_numero(config, secao, "contorno_espessura", minimo=0)
        for chave in ("cor", "contorno_cor"):
            cor_ass(config[secao][chave])       # levanta se o formato for inválido
    cor_ass(config["legenda"]["cor_destaque"])

    _exigir_numero(config, "legenda", "palavras_por_linha", minimo=1)
    _exigir_numero(config, "hook", "duracao_s", minimo=0.0)

    if config["watermark"]["ativo"] and not str(config["watermark"]["texto"]).strip():
        raise TemplateInvalido("watermark.ativo mas watermark.texto está vazio")

    _validar_layout(config)
    _validar_capa(config["capa"])
    _validar_grafico(config["grafico"])
    _validar_sfx(config["sfx"])
    _validar_efeitos(config["efeitos"])
    return config


def _validar_layout(config):
    layout = config["layout"]
    if layout not in LAYOUTS:
        raise TemplateInvalido(f"layout precisa ser um de {LAYOUTS}, veio {layout!r}")

    split = config["split"]
    _exigir_numero(config, "split", "proporcao_clip", minimo=0.2, maximo=0.8)
    for chave in ("diretorio", "categoria"):
        if not isinstance(split.get(chave), str):
            raise TemplateInvalido(f"split.{chave} precisa ser texto")
    leg = split["legenda"]
    for chave, minimo, maximo in (("alinhamento", 1, 9), ("margem_vertical", 0, None)):
        valor = leg.get(chave)
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise TemplateInvalido(f"split.legenda.{chave} precisa ser número, veio {valor!r}")
        if valor < minimo or (maximo is not None and valor > maximo):
            raise TemplateInvalido(f"split.legenda.{chave} fora da faixa, veio {valor}")
    sep = split["separador"]
    valor = sep.get("espessura")
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor < 0:
        raise TemplateInvalido(f"split.separador.espessura precisa ser número >= 0, veio {valor!r}")
    cor_ass(sep["cor"])

    variantes = config["variantes"]
    pesos = variantes.get("pesos")
    if not isinstance(pesos, dict):
        raise TemplateInvalido("variantes.pesos precisa ser um objeto")
    for nome, peso in pesos.items():
        layout_v, _, categoria = str(nome).partition(":")
        if layout_v not in LAYOUTS:
            raise TemplateInvalido(
                f"variantes.pesos[{nome!r}]: layout precisa ser um de {LAYOUTS}"
            )
        if layout_v == "full" and categoria:
            raise TemplateInvalido(f"variantes.pesos[{nome!r}]: 'full' não tem categoria")
        if layout_v == "split" and not (categoria or split["categoria"]):
            raise TemplateInvalido(
                f"variantes.pesos[{nome!r}]: sem categoria (use 'split:<categoria>' "
                "ou defina split.categoria)"
            )
        if isinstance(peso, bool) or not isinstance(peso, (int, float)) or peso < 0:
            raise TemplateInvalido(
                f"variantes.pesos[{nome!r}] precisa ser número >= 0, veio {peso!r}"
            )
    if pesos and not any(p > 0 for p in pesos.values()):
        raise TemplateInvalido("variantes.pesos: todos os pesos são zero")
    if layout == "split" and not pesos and not split["categoria"]:
        raise TemplateInvalido("layout 'split' exige split.categoria")


def _validar_capa(capa):
    if not isinstance(capa.get("ativa"), bool):
        raise TemplateInvalido(
            f"capa.ativa precisa ser true ou false, veio {capa.get('ativa')!r}")
    valor = capa.get("tamanho_max")
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor < 20:
        raise TemplateInvalido(
            f"capa.tamanho_max precisa ser número >= 20, veio {valor!r}")
    valor = capa.get("contorno_espessura")
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor < 0:
        raise TemplateInvalido(
            f"capa.contorno_espessura precisa ser número >= 0, veio {valor!r}")
    for chave in ("cor", "caixa_cor"):
        cor_ass(capa[chave])
    if not isinstance(capa.get("fonte"), str) or not capa["fonte"].strip():
        raise TemplateInvalido("capa.fonte precisa ser texto")
    frame = capa.get("frame_inicial")
    if not isinstance(frame, dict) or not isinstance(frame.get("ativo"), bool):
        raise TemplateInvalido(
            "capa.frame_inicial.ativo precisa ser true ou false")
    dur = frame.get("duracao_s")
    if isinstance(dur, bool) or not isinstance(dur, (int, float)) \
            or not 0.0 < dur <= 1.0:
        raise TemplateInvalido(
            f"capa.frame_inicial.duracao_s precisa estar em (0, 1], veio {dur!r}")


def _validar_grafico(g):
    if not isinstance(g.get("ativo"), bool):
        raise TemplateInvalido(
            f"grafico.ativo precisa ser true ou false, veio {g.get('ativo')!r}")
    if g.get("fundo") not in ("desfoque", "solido"):
        raise TemplateInvalido(
            f"grafico.fundo precisa ser 'desfoque' ou 'solido', "
            f"veio {g.get('fundo')!r}")
    cor_ass(g["cor_solida"])
    for chave, minimo, maximo in (
            ("desfoque_sigma", 0.0, 200.0), ("transicao_s", 0.05, 1.0),
            ("min_duracao_s", 0.5, 60.0), ("maximo_por_clip", 1, 10),
            ("amostras_por_s", 0.5, 8.0), ("largura_minima", 0.1, 1.0)):
        valor = g.get(chave)
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise TemplateInvalido(
                f"grafico.{chave} precisa ser número, veio {valor!r}")
        if valor < minimo or valor > maximo:
            raise TemplateInvalido(
                f"grafico.{chave} precisa estar em [{minimo}, {maximo}], "
                f"veio {valor}")


def _validar_sfx(sfx):
    for chave, minimo in (("volume", 0.0), ("espacamento_minimo_s", 0.0),
                          ("maximo_por_clip", 0)):
        valor = sfx.get(chave)
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise TemplateInvalido(f"sfx.{chave} precisa ser número, veio {valor!r}")
        if valor < minimo:
            raise TemplateInvalido(f"sfx.{chave} precisa ser >= {minimo}, veio {valor}")

    eventos = sfx.get("eventos")
    if not isinstance(eventos, dict):
        raise TemplateInvalido("sfx.eventos precisa ser um objeto")

    for nome, evento in eventos.items():
        if not isinstance(evento, dict):
            raise TemplateInvalido(f"sfx.eventos.{nome} precisa ser um objeto")
        if evento.get("gatilho") not in GATILHOS_SFX:
            raise TemplateInvalido(
                f"sfx.eventos.{nome}.gatilho precisa ser um de {GATILHOS_SFX}, "
                f"veio {evento.get('gatilho')!r}"
            )
        if evento.get("ativo") and not str(evento.get("arquivo") or "").strip():
            raise TemplateInvalido(f"sfx.eventos.{nome} está ativo mas sem arquivo")

    if sfx.get("ativo") and not any(e.get("ativo") for e in eventos.values()):
        raise TemplateInvalido(
            "sfx.ativo mas nenhum evento está ativo — nada tocaria"
        )
    if not isinstance(sfx.get("palavras_chave"), list):
        raise TemplateInvalido("sfx.palavras_chave precisa ser uma lista")


def _numero(secao, nome, chave, minimo=None, maximo=None, exclusivo=False):
    valor = secao.get(chave)
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise TemplateInvalido(f"efeitos.{nome}.{chave} precisa ser número, veio {valor!r}")
    if minimo is not None and (valor <= minimo if exclusivo else valor < minimo):
        raise TemplateInvalido(
            f"efeitos.{nome}.{chave} precisa ser {'>' if exclusivo else '>='} "
            f"{minimo}, veio {valor}"
        )
    if maximo is not None and valor > maximo:
        raise TemplateInvalido(f"efeitos.{nome}.{chave} precisa ser <= {maximo}, veio {valor}")


def _validar_efeitos(ef):
    for nome in ("zoom", "flash", "vinheta", "barra", "voz", "ducking", "pad"):
        secao = ef.get(nome)
        if not isinstance(secao, dict):
            raise TemplateInvalido(f"efeitos.{nome} precisa ser um objeto")
        if not isinstance(secao.get("ativo"), bool):
            raise TemplateInvalido(
                f"efeitos.{nome}.ativo precisa ser true ou false, veio {secao.get('ativo')!r}"
            )

    zoom = ef["zoom"]
    if zoom["modo"] not in ("picos", "intervalo"):
        raise TemplateInvalido(
            f"efeitos.zoom.modo precisa ser 'picos' ou 'intervalo', veio {zoom['modo']!r}"
        )
    _numero(zoom, "zoom", "fator", minimo=1.0)
    _numero(zoom, "zoom", "fator", maximo=1.5)
    _numero(zoom, "zoom", "duracao_s", minimo=0.0, exclusivo=True)
    _numero(zoom, "zoom", "intervalo_s", minimo=0.0, exclusivo=True)
    _numero(zoom, "zoom", "espacamento_minimo_s", minimo=0.0)
    _numero(zoom, "zoom", "maximo_por_clip", minimo=0)

    flash = ef["flash"]
    _numero(flash, "flash", "intensidade", minimo=0.0, maximo=1.0)
    _numero(flash, "flash", "duracao_s", minimo=0.0, exclusivo=True)
    _numero(ef["vinheta"], "vinheta", "angulo", minimo=0.0, maximo=1.5707)
    barra = ef["barra"]
    _numero(barra, "barra", "altura", minimo=1)
    cor_ass(barra["cor"])
    if barra["posicao"] not in ("base", "topo"):
        raise TemplateInvalido(
            f"efeitos.barra.posicao precisa ser 'base' ou 'topo', veio {barra['posicao']!r}"
        )

    _numero(ef["voz"], "voz", "lufs", minimo=-40.0, maximo=-5.0)
    duck = ef["ducking"]
    _numero(duck, "ducking", "limiar", minimo=0.0, exclusivo=True, maximo=1.0)
    _numero(duck, "ducking", "razao", minimo=1.0)
    _numero(duck, "ducking", "ataque_ms", minimo=0.01)
    _numero(duck, "ducking", "liberacao_ms", minimo=0.01)
    pad = ef["pad"]
    _numero(pad, "pad", "volume", minimo=0.0)
    if "{categoria}" not in str(pad.get("arquivo", "")) and not str(pad.get("arquivo", "")).strip():
        raise TemplateInvalido("efeitos.pad.arquivo está vazio")
    valor = ef.get("teto_sfx_abaixo_da_voz_db")
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor < 0:
        raise TemplateInvalido(
            f"efeitos.teto_sfx_abaixo_da_voz_db precisa ser número >= 0, veio {valor!r}"
        )


def carregar(caminho=None):
    """Lê, mescla com os defaults e valida."""
    caminho = caminho or settings.TEMPLATE_CONFIG_PATH
    if not os.path.exists(caminho):
        raise TemplateInvalido(
            f"{caminho} não existe. É ele que define o visual do clip — sem "
            "ele o render não tem template para aplicar."
        )
    with open(caminho, encoding="utf-8") as f:
        try:
            bruto = json.load(f)
        except json.JSONDecodeError as e:
            raise TemplateInvalido(f"{caminho} não é JSON válido: {e}") from e

    if not isinstance(bruto, dict):
        raise TemplateInvalido(f"{caminho} precisa conter um objeto JSON.")

    config = validar(_mesclar(PADRAO, _sem_comentarios(bruto)))
    log.debug("Template versão %s carregado de %s.", config["versao"], caminho)
    return config


def cor_ass(valor):
    """#RRGGBB ou #RRGGBBAA -> &HAABBGGRR, o formato de cor do ASS.

    Duas armadilhas do ASS, e é por isso que esta conversão existe em vez de a
    cor ir crua para o arquivo:
      - a ordem dos canais é BGR, não RGB. Escrever #FF0000 direto pinta AZUL.
      - o canal alfa é INVERTIDO: 00 é opaco, FF é transparente. No JSON o
        alfa é o intuitivo (FF = opaco), e a inversão acontece aqui.
    """
    if not isinstance(valor, str):
        raise TemplateInvalido(f"cor precisa ser string, veio {valor!r}")
    texto = valor.strip().lstrip("#")
    if len(texto) not in (6, 8):
        raise TemplateInvalido(f"cor precisa ser #RRGGBB ou #RRGGBBAA, veio {valor!r}")
    try:
        int(texto, 16)
    except ValueError:
        raise TemplateInvalido(f"cor não é hexadecimal: {valor!r}") from None

    r, g, b = texto[0:2], texto[2:4], texto[4:6]
    opacidade = int(texto[6:8], 16) if len(texto) == 8 else 255
    return f"&H{255 - opacidade:02X}{b}{g}{r}".upper()
