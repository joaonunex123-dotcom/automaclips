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
    "sfx": {
        "ativo": False, "diretorio": "assets/sfx", "volume": 0.6,
        "espacamento_minimo_s": 1.5, "maximo_por_clip": 8,
        "eventos": {},
        "na_abertura": True, "no_fim_do_hook": True,
        "exclamacao_conta": True, "palavras_chave": [],
    },
}

MODOS_REFRAME = ("corte", "desfoque")

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

    _validar_sfx(config["sfx"])
    return config


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
