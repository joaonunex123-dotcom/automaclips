"""Decide QUANDO cada clip é publicado.

O spec pede 3–4 posts por dia em horários definidos pelo histórico de
engajamento, com horários padrão configuráveis como fallback. Hoje o fallback
é o caminho real, porque não existe histórico: nenhum clip foi publicado
ainda. `pesos_do_historico` já existe e devolve vazio — a etapa 7 a preenche,
e a ordenação por peso passa a valer sem que nada mais mude.

Duas regras que evitam desperdiçar clip bom:

* **Intervalo mínimo entre posts da mesma plataforma.** Dois clips seguidos
  competem entre si pela mesma audiência no mesmo feed: o segundo pega o
  público que o primeiro acabou de consumir.
* **Teto de dias à frente.** Sem ele, uma fila grande marcaria posts para
  daqui a meses, e clip de assunto quente não sobrevive a isso. É melhor
  deixar o excedente sem agendar e reavaliar amanhã, quando ele pode ter sido
  superado por material mais novo.

Todo horário é string ISO local no formato do SQLite ('YYYY-MM-DD HH:MM:SS'),
para comparar direto com `datetime('now','localtime')` sem conversão.
"""
import logging
from datetime import datetime, timedelta

import settings
from db import repositorio

log = logging.getLogger(__name__)

FORMATO = "%Y-%m-%d %H:%M:%S"


class HorarioInvalido(Exception):
    """HORARIOS_PADRAO com entrada que não é HH:MM."""


def formatar(momento):
    return momento.strftime(FORMATO)


def analisar(texto):
    return datetime.strptime(texto, FORMATO)


def horarios_configurados(horarios=None):
    """['12:00', '18:00'] -> [(12, 0), (18, 0)], ordenados e sem repetição."""
    horarios = settings.HORARIOS_PADRAO if horarios is None else horarios
    saida = set()
    for bruto in horarios:
        texto = str(bruto).strip()
        try:
            hora, minuto = texto.split(":")
            hora, minuto = int(hora), int(minuto)
        except ValueError:
            raise HorarioInvalido(
                f"horário {bruto!r} não está no formato HH:MM"
            ) from None
        if not (0 <= hora <= 23 and 0 <= minuto <= 59):
            raise HorarioInvalido(f"horário {bruto!r} fora do relógio")
        saida.add((hora, minuto))
    if not saida:
        raise HorarioInvalido("nenhum horário configurado — ver HORARIOS_PADRAO")
    return sorted(saida)


def pesos_do_historico(conn):
    """{(hora, minuto): peso} a partir do engajamento observado.

    Vazio enquanto não houver posts medidos suficientes, e vazio é o
    comportamento correto: sem dado, todos os horários empatam e a agenda sai
    na ordem natural do dia. Inventar um peso seria apresentar palpite como
    medição.

    O import é local porque o analytics é consumidor do publish (mede o que foi
    publicado); trazê-lo para o topo deste módulo criaria a dependência na
    direção errada.
    """
    from analytics import recalibrate

    try:
        return recalibrate.pesos_por_horario(conn)
    except Exception as e:
        # Peso é otimização, não requisito: sem ele a agenda continua saindo
        # na ordem do relógio. Derrubar o agendamento por causa disso pararia
        # a publicação inteira.
        log.warning("Pesos de horário indisponíveis (%s); usando a ordem do "
                    "relógio.", e)
        return {}


def ordenar_por_peso(horarios, pesos):
    """Melhores horários primeiro; empate mantém a ordem do relógio.

    Estável de propósito: sem histórico todos empatam, e a agenda sai na
    ordem natural do dia em vez de embaralhada.
    """
    return sorted(horarios, key=lambda h: (-float(pesos.get(h, 0.0)),
                                           h[0], h[1]))


def candidatos(agora, horarios, dias_max):
    """Todos os horários futuros dentro da janela, em ordem cronológica."""
    for dia in range(dias_max + 1):
        data = (agora + timedelta(days=dia)).date()
        for hora, minuto in horarios:
            momento = datetime.combine(data, datetime.min.time()).replace(
                hour=hora, minute=minuto
            )
            if momento > agora:
                yield momento


def _muito_perto(momento, ocupados, intervalo_min):
    limite = timedelta(minutes=intervalo_min)
    return any(abs(momento - o) < limite for o in ocupados)


def proximos_slots(conn, plataforma, quantidade, agora=None, horarios=None,
                   dias_max=None, intervalo_min=None, pesos=None):
    """Os próximos `quantidade` horários livres da plataforma.

    Pode devolver menos do que o pedido — é o que acontece quando a janela de
    dias acaba antes da fila, e é a resposta certa: o excedente fica sem
    agendar e concorre de novo amanhã.
    """
    agora = agora or datetime.now()
    dias_max = settings.AGENDAMENTO_MAX_DIAS if dias_max is None else dias_max
    if intervalo_min is None:
        intervalo_min = settings.INTERVALO_MINIMO_MIN
    pesos = pesos_do_historico(conn) if pesos is None else pesos

    do_dia = ordenar_por_peso(horarios_configurados(horarios), pesos)

    ocupados = []
    for texto in repositorio.horarios_agendados(conn, plataforma,
                                                desde=formatar(agora)):
        try:
            ocupados.append(analisar(texto))
        except (TypeError, ValueError):
            log.warning("Horário agendado ilegível no banco: %r", texto)

    escolhidos = []
    for momento in candidatos(agora, do_dia, dias_max):
        if len(escolhidos) >= quantidade:
            break
        # A comparação é contra os já ocupados NO BANCO e contra os escolhidos
        # nesta mesma rodada: sem a segunda metade, dois clips da mesma
        # execução cairiam em horários vizinhos.
        if not _muito_perto(momento, ocupados + escolhidos, intervalo_min):
            escolhidos.append(momento)

    escolhidos.sort()
    return [formatar(m) for m in escolhidos]


def agenda_do_dia(conn, plataforma, agora=None):
    """Quantos posts já estão marcados para hoje — usado no resumo."""
    agora = agora or datetime.now()
    hoje = agora.date().isoformat()
    return sum(
        1 for texto in repositorio.horarios_agendados(conn, plataforma)
        if str(texto).startswith(hoje)
    )
