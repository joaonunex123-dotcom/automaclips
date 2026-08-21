"""Os três freios da publicação real: emergência, teto diário e aquecimento."""
from datetime import datetime, timedelta

import pytest

import settings
from db import repositorio
from publish import publicar

AGORA = datetime(2026, 8, 11, 23, 0, 0)
HOJE = AGORA.date().isoformat()


@pytest.fixture
def sem_freios(monkeypatch, tmp_path):
    """Desliga os três, para cada teste ligar só o que está exercitando."""
    monkeypatch.setattr(settings, "ARQUIVO_PARAR_PUBLICACAO",
                        str(tmp_path / "PARAR_PUBLICACAO"))
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 0)
    monkeypatch.setattr(settings, "AQUECIMENTO_POSTS_DIA", 0)
    return tmp_path


def _publicar(conn, clip_publicavel, quando=HOJE, plataforma="youtube",
              video_id="v"):
    """Grava um post JÁ PUBLICADO, para contar contra os tetos."""
    clip_id = clip_publicavel(video_id=video_id)
    pub_id = repositorio.agendar_publicacao(
        conn, clip_id, plataforma, f"{quando} 12:00:00"
    )
    conn.execute(
        "UPDATE publicacoes SET status = ?, publicado_em = ? WHERE id = ?",
        (repositorio.PUB_PUBLICADO, f"{quando} 12:00:05", pub_id),
    )
    return pub_id


# --- 1. parada de emergência --------------------------------------------------

def test_arquivo_de_parada_trava_tudo(conn, sem_freios):
    (sem_freios / "PARAR_PUBLICACAO").write_text("", encoding="utf-8")
    motivo = publicar.freio_ativo(conn, "youtube", AGORA)
    assert motivo and "emergencia" in motivo


def test_parada_vence_os_outros_freios(conn, sem_freios, monkeypatch):
    # Emergência não negocia com configuração: é checada primeiro.
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 99)
    (sem_freios / "PARAR_PUBLICACAO").write_text("", encoding="utf-8")
    assert "emergencia" in publicar.freio_ativo(conn, "youtube", AGORA)


def test_sem_arquivo_nao_ha_freio(conn, sem_freios):
    assert publicar.freio_ativo(conn, "youtube", AGORA) is None


# --- 2. teto absoluto do dia --------------------------------------------------

def test_teto_diario_para_a_plataforma(conn, sem_freios, clip_publicavel,
                                       monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 2)
    _publicar(conn, clip_publicavel, video_id="a")
    assert publicar.freio_ativo(conn, "youtube", AGORA) is None
    _publicar(conn, clip_publicavel, video_id="b")
    assert "teto de 2" in publicar.freio_ativo(conn, "youtube", AGORA)


def test_teto_e_por_plataforma(conn, sem_freios, clip_publicavel, monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 1)
    _publicar(conn, clip_publicavel, plataforma="youtube", video_id="a")
    assert publicar.freio_ativo(conn, "youtube", AGORA) is not None
    # O Instagram é outra conta e outro feed.
    assert publicar.freio_ativo(conn, "instagram", AGORA) is None


def test_post_de_ontem_nao_conta_hoje(conn, sem_freios, clip_publicavel,
                                      monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 1)
    ontem = (AGORA - timedelta(days=1)).date().isoformat()
    _publicar(conn, clip_publicavel, quando=ontem, video_id="a")
    assert publicar.freio_ativo(conn, "youtube", AGORA) is None


def test_simulado_nao_consome_o_teto(conn, sem_freios, clip_publicavel,
                                     monkeypatch):
    # Uma semana de modo sombra não pode bloquear o primeiro dia de publicação
    # de verdade.
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 1)
    clip_id = clip_publicavel()
    pub_id = repositorio.agendar_publicacao(
        conn, clip_id, "youtube", f"{HOJE} 12:00:00"
    )
    repositorio.marcar_publicacao(conn, pub_id, repositorio.PUB_SIMULADO)
    assert publicar.freio_ativo(conn, "youtube", AGORA) is None


def test_teto_zero_desliga(conn, sem_freios, clip_publicavel, monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 0)
    for i in range(5):
        _publicar(conn, clip_publicavel, video_id=f"v{i}")
    assert publicar.freio_ativo(conn, "youtube", AGORA) is None


# --- 3. aquecimento -----------------------------------------------------------

def test_aquecimento_limita_o_primeiro_dia(conn, sem_freios, clip_publicavel,
                                           monkeypatch):
    monkeypatch.setattr(settings, "AQUECIMENTO_POSTS_DIA", 1)
    monkeypatch.setattr(settings, "AQUECIMENTO_DIAS", 3)

    assert publicar.limite_de_aquecimento(conn, AGORA) == 1
    _publicar(conn, clip_publicavel, video_id="a")
    assert "aquecimento" in publicar.freio_ativo(conn, "youtube", AGORA)


def test_relogio_comeca_no_primeiro_post_nao_na_flag(conn, sem_freios,
                                                     clip_publicavel,
                                                     monkeypatch):
    # Ligar, esquecer uma semana e depois publicar no volume cheio é
    # exatamente o que o aquecimento existe para evitar.
    monkeypatch.setattr(settings, "AQUECIMENTO_POSTS_DIA", 1)
    monkeypatch.setattr(settings, "AQUECIMENTO_DIAS", 3)
    _publicar(conn, clip_publicavel, quando="2026-08-10", video_id="a")

    # Dois dias depois do primeiro post: ainda aquecendo.
    assert publicar.limite_de_aquecimento(
        conn, datetime(2026, 8, 12, 10, 0)) == 1
    # Quatro dias depois: liberado.
    assert publicar.limite_de_aquecimento(
        conn, datetime(2026, 8, 14, 10, 0)) is None


def test_aquecimento_desligado(conn, sem_freios, monkeypatch):
    monkeypatch.setattr(settings, "AQUECIMENTO_POSTS_DIA", 0)
    assert publicar.limite_de_aquecimento(conn, AGORA) is None


# --- integração com o laço de publicação --------------------------------------

def test_freio_adia_em_vez_de_descartar(conn, sem_freios, clip_publicavel,
                                        meta_falso, monkeypatch):
    # Marcar falha aqui queimaria o clip por causa de um teto que vira amanhã.
    monkeypatch.setattr(settings, "MAX_POSTS_DIA_ABSOLUTO", 0)
    (sem_freios / "PARAR_PUBLICACAO").write_text("", encoding="utf-8")

    clip_id = clip_publicavel()
    repositorio.agendar_publicacao(conn, clip_id, "youtube", f"{HOJE} 12:00:00")

    contagem = publicar.processar_vencidas(
        conn, agora=AGORA, auto_publish=True,
        enviadores={"youtube": lambda *a, **k: ("id", "url")},
    )
    assert contagem == {publicar.ADIADO: 1}
    linha = conn.execute("SELECT status FROM publicacoes").fetchone()
    assert linha["status"] == repositorio.PUB_AGENDADO


def test_modo_sombra_ignora_os_freios(conn, sem_freios, clip_publicavel):
    # Em sombra nada é publicado, então não há o que frear — e marcar 'adiado'
    # esconderia da simulação um post que sairia.
    (sem_freios / "PARAR_PUBLICACAO").write_text("", encoding="utf-8")
    clip_id = clip_publicavel()
    repositorio.agendar_publicacao(conn, clip_id, "youtube", f"{HOJE} 12:00:00")

    contagem = publicar.processar_vencidas(
        conn, agora=AGORA, auto_publish=False, enviadores={}
    )
    assert contagem == {repositorio.PUB_SIMULADO: 1}
