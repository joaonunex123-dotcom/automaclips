"""Atribuição de horários de publicação."""
from datetime import datetime

import pytest

import settings
from db import repositorio
from publish import scheduler

AGORA = datetime(2026, 8, 11, 9, 0, 0)      # terça, 9h
HORARIOS = ["12:00", "18:00", "21:00"]


# --- leitura da configuração --------------------------------------------------

def test_horarios_sao_normalizados_e_ordenados():
    assert scheduler.horarios_configurados(["21:00", "12:00", "12:00"]) == [
        (12, 0), (21, 0)
    ]


@pytest.mark.parametrize("ruim", ["12h", "25:00", "12:99", "", "meio-dia"])
def test_horario_invalido_falha_cedo(ruim):
    # Um horário torto só apareceria na hora de agendar, muito depois de
    # baixar, transcrever, chamar o LLM e renderizar.
    with pytest.raises(scheduler.HorarioInvalido):
        scheduler.horarios_configurados([ruim])


def test_lista_vazia_e_erro():
    with pytest.raises(scheduler.HorarioInvalido, match="nenhum horário"):
        scheduler.horarios_configurados([])


# --- histórico de engajamento -------------------------------------------------

def test_sem_historico_o_peso_e_vazio(conn):
    # Sem clip publicado não existe engajamento para medir, e inventar um peso
    # seria apresentar palpite como dado. A etapa 7 preenche.
    assert scheduler.pesos_do_historico(conn) == {}


def test_empate_de_peso_mantem_a_ordem_do_relogio():
    horarios = [(12, 0), (18, 0), (21, 0)]
    assert scheduler.ordenar_por_peso(horarios, {}) == horarios


def test_peso_maior_vem_primeiro():
    horarios = [(12, 0), (18, 0), (21, 0)]
    pesos = {(21, 0): 5.0, (12, 0): 1.0}
    assert scheduler.ordenar_por_peso(horarios, pesos) == [
        (21, 0), (12, 0), (18, 0)
    ]


# --- geração de slots ---------------------------------------------------------

def test_so_horarios_futuros(conn):
    slots = scheduler.proximos_slots(
        conn, "youtube", 3, agora=AGORA, horarios=HORARIOS, intervalo_min=0
    )
    assert slots == ["2026-08-11 12:00:00", "2026-08-11 18:00:00",
                     "2026-08-11 21:00:00"]


def test_horario_que_ja_passou_hoje_cai_para_amanha(conn):
    tarde = datetime(2026, 8, 11, 19, 0, 0)
    slots = scheduler.proximos_slots(
        conn, "youtube", 2, agora=tarde, horarios=HORARIOS, intervalo_min=0
    )
    assert slots == ["2026-08-11 21:00:00", "2026-08-12 12:00:00"]


def test_intervalo_minimo_afasta_posts_da_mesma_plataforma(conn):
    # Dois clips seguidos competem entre si pela mesma audiência no feed.
    slots = scheduler.proximos_slots(
        conn, "youtube", 3, agora=AGORA, horarios=["12:00", "12:30", "18:00"],
        intervalo_min=120,
    )
    assert slots == ["2026-08-11 12:00:00", "2026-08-11 18:00:00",
                     "2026-08-12 12:00:00"]


def test_horario_ja_ocupado_no_banco_e_pulado(conn, clip_publicavel):
    clip_id = clip_publicavel()
    repositorio.agendar_publicacao(
        conn, clip_id, "youtube", "2026-08-11 12:00:00"
    )
    slots = scheduler.proximos_slots(
        conn, "youtube", 1, agora=AGORA, horarios=HORARIOS, intervalo_min=1
    )
    assert slots == ["2026-08-11 18:00:00"]


def test_ocupacao_e_por_plataforma(conn, clip_publicavel):
    clip_id = clip_publicavel()
    repositorio.agendar_publicacao(
        conn, clip_id, "youtube", "2026-08-11 12:00:00"
    )
    # O Instagram é outra conta e outro feed: o mesmo horário está livre lá.
    slots = scheduler.proximos_slots(
        conn, "instagram", 1, agora=AGORA, horarios=HORARIOS, intervalo_min=1
    )
    assert slots == ["2026-08-11 12:00:00"]


def test_teto_de_dias_limita_a_fila(conn):
    # Sem teto, uma fila grande marcaria posts para daqui a meses — e clip de
    # assunto quente não sobrevive a isso.
    slots = scheduler.proximos_slots(
        conn, "youtube", 50, agora=AGORA, horarios=HORARIOS, dias_max=1,
        intervalo_min=0,
    )
    # São 9h, então os três horários de hoje ainda contam: 3 hoje + 3 amanhã.
    # O pedido era 50 — a janela é que limita, e é esse o ponto.
    assert len(slots) == 6
    assert all(s < "2026-08-13" for s in slots)


def test_pode_devolver_menos_do_que_o_pedido(conn):
    slots = scheduler.proximos_slots(
        conn, "youtube", 10, agora=AGORA, horarios=["23:00"], dias_max=0,
        intervalo_min=0,
    )
    assert len(slots) == 1


def test_slots_da_mesma_execucao_nao_colidem(conn):
    # A comparação precisa incluir os escolhidos nesta rodada; sem isso, dois
    # clips da mesma execução cairiam em horários vizinhos.
    slots = scheduler.proximos_slots(
        conn, "youtube", 2, agora=AGORA, horarios=["12:00", "12:10"],
        intervalo_min=60,
    )
    assert len(slots) == 2
    a, b = (scheduler.analisar(s) for s in slots)
    assert (b - a).total_seconds() >= 3600


def test_slots_saem_ordenados_mesmo_com_peso(conn):
    # A ordem de PREFERÊNCIA é por peso, mas a agenda gravada é cronológica.
    slots = scheduler.proximos_slots(
        conn, "youtube", 3, agora=AGORA, horarios=HORARIOS, intervalo_min=0,
        pesos={(21, 0): 9.0},
    )
    assert slots == sorted(slots)


def test_horario_ilegivel_no_banco_nao_derruba(conn, clip_publicavel, caplog):
    clip_id = clip_publicavel()
    repositorio.agendar_publicacao(conn, clip_id, "youtube", "amanhã de tarde")
    slots = scheduler.proximos_slots(
        conn, "youtube", 1, agora=AGORA, horarios=HORARIOS, intervalo_min=0
    )
    assert len(slots) == 1
    assert "ilegível" in caplog.text


def test_agenda_do_dia_conta_so_hoje(conn, clip_publicavel):
    a, b = clip_publicavel(video_id="a"), clip_publicavel(video_id="b")
    repositorio.agendar_publicacao(conn, a, "youtube", "2026-08-11 12:00:00")
    repositorio.agendar_publicacao(conn, b, "youtube", "2026-08-12 12:00:00")
    assert scheduler.agenda_do_dia(conn, "youtube", agora=AGORA) == 1


def test_formatar_e_analisar_sao_inversos():
    momento = datetime(2026, 8, 11, 18, 30, 0)
    assert scheduler.analisar(scheduler.formatar(momento)) == momento


def test_formato_e_comparavel_com_o_do_sqlite(conn):
    # O agendado_para é comparado direto com datetime('now','localtime') em
    # SQL; formato diferente faria a comparação virar ordenação de texto errada.
    do_sqlite = conn.execute("SELECT datetime('now', 'localtime')").fetchone()[0]
    assert scheduler.analisar(do_sqlite)


# --- teto diário por plataforma -----------------------------------------------

def test_teto_diario_empurra_o_excedente_para_amanha(conn):
    slots = scheduler.proximos_slots(
        conn, "tiktok", 4, agora=AGORA,
        horarios=["10:00", "12:00", "18:00", "21:00"], intervalo_min=0,
        por_dia=2,
    )
    dias = [s[:10] for s in slots]
    assert dias == ["2026-08-11", "2026-08-11", "2026-08-12", "2026-08-12"]


def test_cada_plataforma_tem_o_proprio_teto(conn, monkeypatch):
    # A quota do YouTube não tem nada a ver com o limite do TikTok; um número
    # global faria a mais restrita ditar o ritmo da outra.
    monkeypatch.setattr(settings, "POSTS_POR_DIA", 3)
    monkeypatch.setattr(settings, "POSTS_POR_DIA_PLATAFORMA", {"tiktok": 1})

    do_tiktok = scheduler.proximos_slots(conn, "tiktok", 3, agora=AGORA,
                                         horarios=HORARIOS, intervalo_min=0)
    do_youtube = scheduler.proximos_slots(conn, "youtube", 3, agora=AGORA,
                                          horarios=HORARIOS, intervalo_min=0)
    assert [s[:10] for s in do_tiktok] == ["2026-08-11", "2026-08-12",
                                           "2026-08-13"]
    assert [s[:10] for s in do_youtube] == ["2026-08-11"] * 3


def test_agendamento_ja_no_banco_ocupa_a_vaga_do_dia(conn, clip_publicavel):
    clip_id = clip_publicavel()
    repositorio.agendar_publicacao(conn, clip_id, "tiktok",
                                   "2026-08-11 18:00:00")
    slots = scheduler.proximos_slots(conn, "tiktok", 1, agora=AGORA,
                                     horarios=HORARIOS, intervalo_min=0,
                                     por_dia=1)
    assert slots == ["2026-08-12 12:00:00"]


def test_post_que_ja_saiu_hoje_ocupa_a_vaga_do_dia(conn, clip_publicavel):
    # O contador do dia olha o dia inteiro, não só o que ainda está por vir:
    # um post das 8h já gastou uma das vagas de hoje.
    clip_id = clip_publicavel()
    repositorio.agendar_publicacao(conn, clip_id, "tiktok",
                                   "2026-08-11 08:00:00")
    slots = scheduler.proximos_slots(conn, "tiktok", 1, agora=AGORA,
                                     horarios=HORARIOS, intervalo_min=0,
                                     por_dia=1)
    assert slots == ["2026-08-12 12:00:00"]


def test_teto_zero_nao_limita(conn):
    # 0 desliga o teto, como todo número de teto neste projeto.
    slots = scheduler.proximos_slots(conn, "tiktok", 3, agora=AGORA,
                                     horarios=HORARIOS, intervalo_min=0,
                                     por_dia=0)
    assert [s[:10] for s in slots] == ["2026-08-11"] * 3
