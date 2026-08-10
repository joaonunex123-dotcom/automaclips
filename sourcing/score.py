"""A fórmula do score. Função pura: sem banco, sem rede, sem relógio implícito.

    score = views ganhas / horas desde a publicação

"Views GANHAS", não total de views, e a diferença é o ponto todo da métrica.
Ranquear por total premia arquivo: um vídeo com 5 milhões de views acumuladas
em dois anos ganha de qualquer coisa publicada ontem, e o que interessa para
recortar clip é justamente o que está acelerando agora.

Ganho é medido contra a observação ANTERIOR do mesmo vídeo (o histórico em
observacoes_video). Na primeira vez que um vídeo é visto não existe observação
anterior, e a referência implícita passa a ser a própria publicação, com zero
views — então a primeira pontuação é `total / idade`, a velocidade média desde
que o vídeo saiu. Da segunda observação em diante o numerador vira o
incremento real da janela.

O denominador é sempre a idade desde a PUBLICAÇÃO, não o intervalo entre as
duas observações. É intencional e é o que dá o decaimento: o mesmo ganho de
10 mil views vale mais num vídeo de 6 h do que num de 60 h, porque no segundo
caso a janela de oportunidade para surfar o assunto já está fechando.
"""
from collections import namedtuple
from datetime import datetime, timezone

import settings

# ganho        views ganhas na janela (o numerador)
# score        ganho / horas (o número que ranqueia)
# idade_horas  idade REAL do vídeo — sem o piso aplicado ao denominador, para
#              que o filtro de idade em descobrir.py julgue o vídeo de verdade
#              e não a versão amortecida dele.
Pontuacao = namedtuple("Pontuacao", "ganho score idade_horas")


def parse_publicado_em(valor):
    """ISO 8601 -> datetime ciente de fuso, em UTC.

    A API do YouTube devolve RFC 3339 terminando em 'Z'; fromisoformat só
    aceita o 'Z' a partir do Python 3.11, então a troca por '+00:00' é o que
    mantém o módulo rodando nas versões anteriores.

    String sem fuso é tratada como UTC — é o que o schema documenta que
    publicado_em guarda. Assumir o fuso local aqui faria o mesmo vídeo ter
    idades diferentes conforme a máquina que rodou a varredura.
    """
    if isinstance(valor, datetime):
        momento = valor
    else:
        texto = str(valor).strip()
        if texto.endswith(("Z", "z")):
            texto = texto[:-1] + "+00:00"
        momento = datetime.fromisoformat(texto)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


def horas_desde_publicacao(publicado_em, agora=None):
    """Idade do vídeo em horas. Negativa se a publicação está no futuro.

    Futuro acontece de verdade: estreia agendada tem publishedAt à frente, e
    relógio de máquina desalinhado também produz isso. Devolver o negativo em
    vez de zerar é o que permite ao chamador reconhecer o caso e descartar o
    vídeo, em vez de tratá-lo como recém-publicado.
    """
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=timezone.utc)
    delta = agora.astimezone(timezone.utc) - parse_publicado_em(publicado_em)
    return delta.total_seconds() / 3600.0


def calcular(views_atuais, views_anteriores, publicado_em, agora=None,
             idade_minima_horas=None):
    """Pontua uma observação. `views_anteriores=None` = primeira vez que é visto.

    idade_minima_horas é o piso do DENOMINADOR, não um filtro. Sem ele, um
    vídeo publicado há 3 minutos com 20 views marcaria 400 views/h e passaria
    na frente de um com 30 mil views em 24 h (1250/h só depois de acumular
    material de verdade) — o score viraria medida do erro de amostragem de uma
    janela curta demais, não de tração.
    """
    if idade_minima_horas is None:
        idade_minima_horas = settings.IDADE_MINIMA_HORAS

    idade_horas = horas_desde_publicacao(publicado_em, agora)

    if views_anteriores is None:
        ganho = max(0, int(views_atuais))
    else:
        # Plataforma revisa contagem para baixo (limpeza de views de bot, por
        # exemplo). Ganho negativo não é "momento negativo", é ruído de
        # correção: vira zero, e a próxima janela mede a partir do novo valor.
        ganho = max(0, int(views_atuais) - int(views_anteriores))

    denominador = max(idade_horas, idade_minima_horas)
    return Pontuacao(ganho=ganho, score=ganho / denominador, idade_horas=idade_horas)
