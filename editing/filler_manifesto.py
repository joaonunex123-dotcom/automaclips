"""Manifesto, validador e rotação dos vídeos de preenchimento (layout split).

Três responsabilidades, todas locais (nada aqui baixa ou publica):

* **Manifesto.** assets/filler/MANIFESTO.json diz de onde veio cada arquivo
  (URL, canal, licença...). É o que torna a pasta auditável.
* **Validador.** Com manifesto presente, arquivo sem entrada, sem URL ou sem
  categoria é recusado — não entra no sorteio. Licença "padrao" passa, mas com
  aviso de risco, e pode ser desligada por FILLER_PERMITIR_LICENCA_PADRAO.
* **Rotação.** O trecho é sorteado com semente e evita repetir o mesmo trecho
  em clips consecutivos do mesmo canal (histórico em JSON, por canal).

Sem MANIFESTO.json a pasta se comporta como antes (arquivos do usuário).
"""
import json
import logging
import os

log = logging.getLogger(__name__)

NOME_MANIFESTO = "MANIFESTO.json"
NOME_HISTORICO = ".rotacao.json"
LICENCA_PADRAO = "padrao"
HISTORICO_MAX = 20          # entradas guardadas por canal
EVITAR_ULTIMOS = 2          # trechos recentes que o próximo clip não pode tocar
_TENTATIVAS = 25


class ErroManifesto(Exception):
    """MANIFESTO.json ilegível ou com formato inválido."""


# --- manifesto ----------------------------------------------------------------

def caminho_manifesto(raiz):
    return os.path.join(raiz, NOME_MANIFESTO)


def carregar(raiz):
    """{arquivo_relativo: entrada} ou None quando não há manifesto.

    Levanta ErroManifesto se o arquivo existe mas não é legível: manifesto
    corrompido não pode ser tratado como "sem manifesto" (liberaria tudo).
    """
    caminho = caminho_manifesto(raiz)
    if not os.path.isfile(caminho):
        return None
    try:
        with open(caminho, encoding="utf-8") as f:
            dados = json.load(f)
        entradas = dados["arquivos"]
        if not isinstance(entradas, list):
            raise TypeError("'arquivos' deve ser lista")
        return {str(e["arquivo"]).replace("\\", "/"): e for e in entradas}
    except (OSError, ValueError, KeyError, TypeError) as erro:
        raise ErroManifesto(f"{caminho} inválido: {erro}") from erro


def _relativo(caminho, raiz):
    return os.path.relpath(os.path.abspath(caminho),
                           os.path.abspath(raiz)).replace(os.sep, "/")


def validar_arquivo(caminho, raiz, manifesto, permitir_padrao=True):
    """(ok, motivo, aviso). `motivo` explica a recusa; `aviso` é risco aceito."""
    relativo = _relativo(caminho, raiz)
    entrada = manifesto.get(relativo)
    if entrada is None:
        return False, f"{relativo}: sem entrada no manifesto", ""
    if not str(entrada.get("categoria") or "").strip():
        return False, f"{relativo}: entrada sem categoria", ""
    pasta = os.path.basename(os.path.dirname(os.path.abspath(caminho)))
    if entrada["categoria"] != pasta:
        return False, (f"{relativo}: categoria {entrada['categoria']!r} não "
                       f"bate com a pasta {pasta!r}"), ""
    if not str(entrada.get("url") or "").strip():
        return False, f"{relativo}: entrada sem URL de origem", ""
    if str(entrada.get("licenca") or "") == LICENCA_PADRAO:
        if not permitir_padrao:
            return False, (f"{relativo}: licença 'padrao' recusada "
                           "(FILLER_PERMITIR_LICENCA_PADRAO desligado)"), ""
        return True, "", (f"{relativo}: licença 'padrao' — direitos reservados "
                          "do autor; risco de strike/bloqueio ao publicar")
    return True, "", ""


def filtrar(arquivos, raiz, permitir_padrao=True):
    """Mantém só os arquivos válidos. Sem manifesto, devolve tudo (legado).

    Cada recusa e cada aviso de risco vão para o log (uma linha por arquivo).
    """
    manifesto = carregar(raiz)
    if manifesto is None:
        return list(arquivos)
    aceitos = []
    for caminho in arquivos:
        ok, motivo, aviso = validar_arquivo(caminho, raiz, manifesto,
                                            permitir_padrao)
        if not ok:
            log.warning("Filler recusado: %s", motivo)
            continue
        if aviso:
            log.warning("Filler com risco: %s", aviso)
        aceitos.append(caminho)
    return aceitos


# --- rotação ------------------------------------------------------------------

def _sobrepoe(a, b):
    return (a["caminho"] == b["caminho"]
            and a["inicio_s"] < b["fim_s"] and b["inicio_s"] < a["fim_s"])


def sortear_trecho(rng, candidatos, duracao_clip_s, recentes=()):
    """Sorteia {'caminho','inicio_s','fim_s','loop'} fora dos trechos recentes.

    `candidatos` é [(caminho, duracao_s)]. Tenta até _TENTATIVAS sorteios; se
    todos colidem (poucos arquivos curtos), devolve o último e marca
    'repetido': melhor repetir do que ficar sem preenchimento.
    """
    recentes = list(recentes)[-EVITAR_ULTIMOS:]
    plano = None
    for _ in range(_TENTATIVAS):
        caminho, duracao = rng.choice(candidatos)
        if duracao <= duracao_clip_s:
            inicio, fim, loop = 0.0, float(duracao), True
        else:
            inicio = round(rng.uniform(0.0, duracao - duracao_clip_s), 3)
            fim, loop = inicio + duracao_clip_s, False
        plano = {"caminho": caminho, "inicio_s": inicio, "fim_s": fim,
                 "loop": loop, "repetido": False}
        if not any(_sobrepoe(plano, r) for r in recentes):
            return plano
    plano["repetido"] = True
    return plano


def caminho_historico(raiz):
    return os.path.join(raiz, NOME_HISTORICO)


def ler_historico(raiz, canal):
    """Trechos já usados por este canal, do mais antigo ao mais recente."""
    try:
        with open(caminho_historico(raiz), encoding="utf-8") as f:
            return list(json.load(f).get(canal, []))
    except (OSError, ValueError, AttributeError):
        return []


def registrar(raiz, canal, clip_id, plano):
    """Grava o trecho no histórico do canal. Falha de disco só vira log."""
    caminho = caminho_historico(raiz)
    try:
        try:
            with open(caminho, encoding="utf-8") as f:
                todos = json.load(f)
        except (OSError, ValueError):
            todos = {}
        itens = [i for i in todos.get(canal, []) if i.get("clip_id") != clip_id]
        itens.append({"clip_id": clip_id, "caminho": plano["caminho"],
                      "inicio_s": plano["inicio_s"], "fim_s": plano["fim_s"],
                      "loop": plano["loop"]})
        todos[canal] = itens[-HISTORICO_MAX:]
        temporario = caminho + ".tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=1)
        os.replace(temporario, caminho)
    except OSError as erro:
        log.warning("Histórico de rotação não gravado (%s): %s", caminho, erro)
