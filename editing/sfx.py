"""Decide QUANDO cada efeito sonoro toca. Nenhum áudio é aberto aqui.

O módulo produz um plano — uma lista de (instante, arquivo, volume) — e o
`render.py` transforma isso em entradas e filtros do ffmpeg. A separação é o
que torna a parte com regra de verdade (prioridade, espaçamento, teto)
testável sem um único arquivo .wav.

Os três gatilhos vêm do spec, e cada um responde a um dado que o pipeline já
produziu antes:

* **transicao** — abertura do clip e virada do hook para o conteúdo. São os
  únicos "cortes" que este template tem: o clip é um trecho contínuo, então
  não há corte interno para marcar.
* **pico** — os picos de energia medidos na etapa 2, já gravados por clip e
  relativos ao início dele.
* **palavra_chave** — palavras da lista do template, mais exclamação na fala,
  lidas dos timestamps de palavra da transcrição.

Regra que evita o defeito mais comum deste tipo de edição: uma gargalhada
produz vários picos seguidos e várias palavras marcadas ao mesmo tempo. Sem
espaçamento mínimo e teto por clip, o resultado é metralhadora de efeito em
cima de três segundos de áudio.
"""
import logging
import os
import unicodedata

log = logging.getLogger(__name__)

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Em disputa por espaço, o efeito de menor número vence. A transição é
# estrutural — marca o corte, e sem ela o clip começa "seco"; pico e palavra
# são realce, e perder um não custa nada.
PRIORIDADE = {"transicao": 0, "pico": 1, "palavra_chave": 2}


class ErroSFX(Exception):
    """Biblioteca de efeitos ausente ou incompleta."""


def carregar_biblioteca(config, base_dir=None):
    """{nome_do_evento: caminho}. Vazio quando o SFX está desligado.

    Falha se um arquivo declarado não existir, em vez de pular o efeito em
    silêncio: um clip renderizado sem o som que o template mandava é um defeito
    que só aparece assistindo, muito depois de a fila inteira ter rodado.
    """
    sfx = config["sfx"]
    if not sfx.get("ativo"):
        return {}

    raiz = sfx.get("diretorio") or ""
    if not os.path.isabs(raiz):
        raiz = os.path.join(base_dir or _RAIZ, raiz)

    biblioteca, faltando = {}, []
    for nome, evento in sfx.get("eventos", {}).items():
        if not evento.get("ativo"):
            continue
        caminho = os.path.join(raiz, evento["arquivo"])
        if os.path.exists(caminho):
            biblioteca[nome] = caminho
        else:
            faltando.append(caminho)

    if faltando:
        raise ErroSFX(
            "arquivos de efeito não encontrados: " + ", ".join(faltando) +
            ". Ver assets/sfx/README.md, ou desligue sfx.ativo no template."
        )
    return biblioteca


def _normalizar(texto):
    """Minúsculas, sem acento e sem pontuação — para comparar palavra falada.

    Sem tirar o acento, "inacreditável" no template não casaria com
    "inacreditavel" transcrito (e vice-versa), e a lista de palavras-chave
    viraria uma loteria de grafia.
    """
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", str(texto))
        if not unicodedata.combining(c)
    )
    limpo = "".join(c if c.isalnum() or c.isspace() else " " for c in sem_acento)
    return " ".join(limpo.lower().split())


def instantes_de_transicao(config, duracao_s):
    """Abertura do clip e virada do hook para o conteúdo."""
    sfx = config["sfx"]
    instantes = []
    if sfx.get("na_abertura"):
        instantes.append(0.0)

    hook = config.get("hook", {})
    if sfx.get("no_fim_do_hook") and hook.get("ativo"):
        fim = float(hook.get("duracao_s") or 0.0)
        # Só marca se a virada acontece DENTRO do clip; um hook tão longo
        # quanto o clip não tem virada para marcar.
        if 0 < fim < duracao_s:
            instantes.append(fim)
    return instantes


def instantes_de_palavra_chave(config, palavras):
    """Instantes das palavras da lista, e das exclamações se configurado.

    `palavras` vem de legendas.palavras_do_trecho, já relativo ao clip.
    """
    sfx = config["sfx"]
    chaves = {
        _normalizar(k) for k in (sfx.get("palavras_chave") or [])
        if _normalizar(k)
    }
    conta_exclamacao = bool(sfx.get("exclamacao_conta"))
    if not chaves and not conta_exclamacao:
        return []

    # Expressões de mais de uma palavra ("meu deus") precisam de janela.
    maior = max((len(k.split()) for k in chaves), default=1)
    normalizadas = [_normalizar(p.get("palavra", "")) for p in palavras]

    instantes = []
    for i, palavra in enumerate(palavras):
        if conta_exclamacao and "!" in str(palavra.get("palavra", "")):
            instantes.append(float(palavra["inicio"]))
            continue
        for tamanho in range(1, maior + 1):
            if i + tamanho > len(palavras):
                break
            frase = " ".join(x for x in normalizadas[i:i + tamanho] if x)
            if frase and frase in chaves:
                instantes.append(float(palavra["inicio"]))
                break
    return instantes


def _candidatos(config, duracao_s, picos, palavras, biblioteca):
    sfx = config["sfx"]
    por_gatilho = {
        "transicao": lambda: instantes_de_transicao(config, duracao_s),
        "pico": lambda: list(picos or []),
        "palavra_chave": lambda: instantes_de_palavra_chave(config, palavras or []),
    }

    candidatos = []
    for nome, evento in sfx.get("eventos", {}).items():
        if not evento.get("ativo") or nome not in biblioteca:
            continue
        gatilho = evento.get("gatilho")
        volume = float(evento.get("volume", sfx.get("volume", 1.0)))
        for instante in por_gatilho.get(gatilho, list)():
            if 0.0 <= instante < duracao_s:
                candidatos.append(
                    {
                        "instante_s": float(instante),
                        "evento": nome,
                        "gatilho": gatilho,
                        "caminho": biblioteca[nome],
                        "volume": volume,
                    }
                )
    return candidatos


def planejar(config, duracao_s, picos=None, palavras=None, biblioteca=None):
    """Plano final de efeitos, ordenado no tempo.

    Seleção gulosa por (prioridade, instante): entre dois efeitos próximos
    demais fica o de gatilho mais importante, e em empate o mais cedo.
    """
    sfx = config["sfx"]
    if not sfx.get("ativo") or duracao_s <= 0:
        return []

    biblioteca = carregar_biblioteca(config) if biblioteca is None else biblioteca
    if not biblioteca:
        return []

    candidatos = _candidatos(config, duracao_s, picos, palavras, biblioteca)
    candidatos.sort(key=lambda c: (PRIORIDADE.get(c["gatilho"], 99), c["instante_s"]))

    espacamento = float(sfx.get("espacamento_minimo_s", 0.0))
    teto = int(sfx.get("maximo_por_clip", 0))

    aceitos = []
    for candidato in candidatos:
        if teto and len(aceitos) >= teto:
            break
        perto = any(
            abs(candidato["instante_s"] - a["instante_s"]) < espacamento
            for a in aceitos
        )
        if not perto:
            aceitos.append(candidato)

    aceitos.sort(key=lambda c: c["instante_s"])
    log.debug("%d efeitos de %d candidatos.", len(aceitos), len(candidatos))
    return aceitos
