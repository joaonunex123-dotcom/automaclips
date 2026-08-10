"""Leitura de sourcing/canais.json — a lista de canais monitorados.

Arquivo separado do settings.py porque é a única configuração que o próprio
sistema reescreve: a etapa 7 (analytics/recalibrate) desativa canais cujos
clips performam mal. Configuração mutável não cabe em variável de ambiente.
"""
import json
import os

import settings


class CanaisInvalidos(Exception):
    """canais.json ausente ou malformado — erro de operação, não de programação."""


def carregar(caminho=None, somente_ativos=True):
    """Devolve a lista de canais. Falha com mensagem acionável, nunca em silêncio.

    Um canais.json ausente é o estado normal de quem acabou de clonar o repo,
    então a mensagem diz exatamente o que copiar. Devolver lista vazia aqui
    faria a varredura "funcionar" sem olhar canal nenhum e reportar zero
    vídeos, que é o modo de falha mais caro de diagnosticar.
    """
    caminho = caminho or settings.CANAIS_PATH
    if not os.path.exists(caminho):
        exemplo = os.path.join(os.path.dirname(caminho), "canais.exemplo.json")
        raise CanaisInvalidos(
            f"{caminho} não existe. Copie {exemplo} para lá e preencha os canais."
        )

    with open(caminho, encoding="utf-8") as f:
        try:
            dados = json.load(f)
        except json.JSONDecodeError as e:
            raise CanaisInvalidos(f"{caminho} não é JSON válido: {e}") from e

    canais = dados.get("canais")
    if not isinstance(canais, list):
        raise CanaisInvalidos(f"{caminho} precisa ter uma lista em 'canais'.")

    saida = []
    for i, canal in enumerate(canais):
        if not isinstance(canal, dict) or not canal.get("id"):
            raise CanaisInvalidos(
                f"{caminho}: canal na posição {i} não tem 'id'."
            )
        # ativo ausente = ativo. Só a etapa 7 escreve false aqui, então a
        # ausência significa "nunca foi avaliado", que deve ser varrido.
        if somente_ativos and not canal.get("ativo", True):
            continue
        saida.append(
            {
                "id": canal["id"],
                "nome": canal.get("nome", ""),
                "plataforma": canal.get("plataforma", "youtube"),
            }
        )
    return saida
