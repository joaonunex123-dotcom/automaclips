"""Agendamento e modo sombra — o coração da etapa 5."""
import json
from datetime import datetime

import pytest

import settings
from db import repositorio
from publish import publicar, quota

AGORA = datetime(2026, 8, 11, 9, 0, 0)
DEPOIS = datetime(2026, 8, 11, 23, 0, 0)
PLATAFORMAS = ["youtube", "instagram"]


@pytest.fixture
def agendar(conn, clip_publicavel, meta_falso):
    """Agenda os clips pendentes com metadado de duplo."""
    def _fn(plataformas=None, **kwargs):
        return publicar.agendar_pendentes(
            conn, plataformas=plataformas or PLATAFORMAS, agora=AGORA,
            gerar=meta_falso(), **kwargs,
        )
    return _fn


# --- seleção do que agendar ---------------------------------------------------

def test_clip_sem_render_nao_entra_na_fila(conn, clip_publicavel):
    # Agendar um clip que ainda não renderizou marcaria um horário para um
    # vídeo que não existe.
    clip_publicavel(com_render=False)
    assert publicar.clips_pendentes(conn, PLATAFORMAS) == []


def test_metadado_e_gerado_uma_vez_por_clip(conn, clip_publicavel, meta_falso):
    # Gerar por plataforma dobraria o custo e ainda daria título e caption que
    # não conversam entre si.
    clip_publicavel()
    chamadas = []

    def gerar(clip, **kwargs):
        chamadas.append(clip["id"])
        return meta_falso()(clip)

    publicar.agendar_pendentes(conn, PLATAFORMAS, agora=AGORA, gerar=gerar)
    assert len(chamadas) == 1


def test_agenda_nas_duas_plataformas(conn, clip_publicavel, agendar):
    clip_id = clip_publicavel()
    assert agendar() == {"youtube": 1, "instagram": 1}

    total = repositorio.contar_publicacoes(conn)
    assert total[("youtube", "agendado")] == 1
    assert total[("instagram", "agendado")] == 1


def test_clip_ja_agendado_nao_repete(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar()
    assert agendar() == {}


def test_melhores_clips_primeiro(conn, clip_publicavel, agendar):
    clip_publicavel(video_id="fraco", score=2.0)
    clip_publicavel(video_id="forte", score=9.9)
    agendar(plataformas=["youtube"])

    fila = repositorio.proximas_publicacoes(conn)
    primeiro = conn.execute(
        "SELECT f.video_id FROM publicacoes p JOIN clips c ON c.id = p.clip_id"
        " JOIN fila_clips f ON f.id = c.fila_clip_id WHERE p.id = ?",
        (fila[0]["id"],),
    ).fetchone()
    assert primeiro["video_id"] == "forte"


def test_falha_de_metadado_nao_derruba_os_outros(conn, clip_publicavel,
                                                 meta_falso):
    clip_publicavel(video_id="quebrado", score=9.9)
    clip_publicavel(video_id="bom", score=1.0)

    def gerar(clip, **kwargs):
        if clip["score_final"] > 5:
            raise RuntimeError("modelo recusou")
        return meta_falso()(clip)

    contagem = publicar.agendar_pendentes(
        conn, ["youtube"], agora=AGORA, gerar=gerar
    )
    assert contagem == {"youtube": 1}


def test_transcricao_ilegivel_nao_impede_o_agendamento(conn, clip_publicavel,
                                                       meta_falso, caplog):
    # Metadado sem a fala sai pior, mas sai; derrubar o clip seria perder tudo.
    clip_publicavel(com_transcricao=False)
    recebidos = []

    def gerar(clip, fala="", **kwargs):
        recebidos.append(fala)
        return meta_falso()(clip)

    publicar.agendar_pendentes(conn, ["youtube"], agora=AGORA, gerar=gerar)
    assert recebidos == [""]


def test_a_fala_do_trecho_chega_ao_metadado(conn, clip_publicavel, meta_falso):
    clip_publicavel()
    recebidos = []

    def gerar(clip, fala="", titulo_fonte="", canal="", **kwargs):
        recebidos.append((fala, titulo_fonte, canal))
        return meta_falso()(clip)

    publicar.agendar_pendentes(conn, ["youtube"], agora=AGORA, gerar=gerar)
    fala, titulo, canal = recebidos[0]
    assert "a virada acontece aqui" in fala
    assert titulo == "Vídeo vid1"
    assert canal == "Canal de Teste"


# --- texto por plataforma -----------------------------------------------------

def test_youtube_recebe_descricao_com_credito(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar(plataformas=["youtube"])
    linha = conn.execute(
        "SELECT * FROM publicacoes WHERE plataforma = 'youtube'"
    ).fetchone()
    assert "Trecho de:" in linha["descricao"]
    assert "#podcast" in linha["descricao"]


def test_instagram_recebe_caption(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar(plataformas=["instagram"])
    linha = conn.execute(
        "SELECT * FROM publicacoes WHERE plataforma = 'instagram'"
    ).fetchone()
    assert linha["descricao"].startswith("não dava pra prever")


def test_hashtags_ficam_em_lista_json(conn, clip_publicavel, agendar):
    # Guardar o texto já concatenado impediria a etapa 7 de correlacionar
    # performance com hashtag individual.
    clip_publicavel()
    agendar(plataformas=["youtube"])
    linha = conn.execute("SELECT hashtags FROM publicacoes").fetchone()
    assert json.loads(linha["hashtags"]) == ["podcast", "entrevista"]


# --- horários -----------------------------------------------------------------

def test_cada_plataforma_recebe_horario_proprio(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar()
    horarios = conn.execute(
        "SELECT plataforma, agendado_para FROM publicacoes"
    ).fetchall()
    assert len(horarios) == 2
    assert all(h["agendado_para"] > "2026-08-11 09:00:00" for h in horarios)


def test_clips_demais_ficam_sem_horario_e_voltam_depois(conn, clip_publicavel,
                                                        agendar, monkeypatch):
    # Melhor deixar o excedente sem agendar e reavaliar amanhã do que marcar
    # post para daqui a meses.
    monkeypatch.setattr(settings, "HORARIOS_PADRAO", ["12:00"])
    monkeypatch.setattr(settings, "AGENDAMENTO_MAX_DIAS", 0)
    for i in range(3):
        clip_publicavel(video_id=f"v{i}", score=float(i))

    contagem = agendar(plataformas=["youtube"])
    assert contagem == {"youtube": 1}
    assert len(publicar.clips_pendentes(conn, ["youtube"])) == 2


# --- modo sombra --------------------------------------------------------------

def test_sombra_marca_simulado_sem_enviar(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar(plataformas=["youtube"])

    def explode(*a, **k):
        raise AssertionError("não deveria publicar em modo sombra")

    contagem = publicar.processar_vencidas(
        conn, agora=DEPOIS, auto_publish=False, enviadores={"youtube": explode}
    )
    assert contagem == {"simulado": 1}
    linha = conn.execute("SELECT * FROM publicacoes").fetchone()
    assert linha["status"] == "simulado"
    assert linha["id_externo"] == ""


def test_sombra_nao_consome_quota(conn, clip_publicavel, agendar):
    # Nenhuma chamada aconteceu; contar consumo faria o teto do dia real
    # encolher por causa de uma simulação.
    clip_publicavel()
    agendar(plataformas=["youtube"])
    publicar.processar_vencidas(conn, agora=DEPOIS, auto_publish=False,
                               enviadores={})
    assert quota.usada(conn, agora=DEPOIS) == 0


def test_agendamento_futuro_nao_e_processado(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar(plataformas=["youtube"])
    # Ainda são 9h; o primeiro horário é meio-dia.
    assert publicar.processar_vencidas(
        conn, agora=AGORA, auto_publish=False, enviadores={}
    ) == {}


def test_reagendar_devolve_os_simulados_para_a_fila(conn, clip_publicavel,
                                                    agendar):
    # A ponte da etapa 5 para a 6: o metadado gerado (e pago) continua valendo.
    clip_publicavel()
    agendar(plataformas=["youtube"])
    publicar.processar_vencidas(conn, agora=DEPOIS, auto_publish=False,
                               enviadores={})

    assert repositorio.reagendar_simulados(conn) == 1
    linha = conn.execute("SELECT * FROM publicacoes").fetchone()
    assert linha["status"] == "agendado"
    assert linha["publicado_em"] is None


# --- publicação real ----------------------------------------------------------

def test_publica_e_grava_id_e_url(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar(plataformas=["youtube"])

    def enviar(conn_, linha, agora=None):
        return "abc123", "https://youtu.be/abc123"

    contagem = publicar.processar_vencidas(
        conn, agora=DEPOIS, auto_publish=True, enviadores={"youtube": enviar}
    )
    assert contagem == {"publicado": 1}
    linha = conn.execute("SELECT * FROM publicacoes").fetchone()
    assert linha["id_externo"] == "abc123"
    assert linha["publicado_em"]


def test_falha_de_um_post_nao_derruba_os_outros(conn, clip_publicavel, agendar):
    clip_publicavel(video_id="a", score=9.0)
    clip_publicavel(video_id="b", score=8.0)
    agendar(plataformas=["youtube"])
    tentativas = []

    def enviar(conn_, linha, agora=None):
        tentativas.append(linha["id"])
        if len(tentativas) == 1:
            raise RuntimeError("quota exceeded")
        return "ok", "https://youtu.be/ok"

    contagem = publicar.processar_vencidas(
        conn, agora=datetime(2026, 8, 20), auto_publish=True,
        enviadores={"youtube": enviar},
    )
    assert contagem == {"falha": 1, "publicado": 1}
    erro = conn.execute(
        "SELECT erro FROM publicacoes WHERE status = 'falha'"
    ).fetchone()
    assert "quota exceeded" in erro["erro"]


def test_plataforma_sem_enviador_vira_falha(conn, clip_publicavel, agendar):
    clip_publicavel()
    agendar(plataformas=["instagram"])
    contagem = publicar.processar_vencidas(
        conn, agora=DEPOIS, auto_publish=True, enviadores={}
    )
    assert contagem == {"falha": 1}


def test_render_sumido_vira_falha(conn, clip_publicavel, agendar):
    clip_id = clip_publicavel()
    agendar(plataformas=["youtube"])
    conn.execute("DELETE FROM renders WHERE clip_id = ?", (clip_id,))

    contagem = publicar.processar_vencidas(
        conn, agora=DEPOIS, auto_publish=True, enviadores={"youtube": None}
    )
    assert contagem == {"falha": 1}
    erro = conn.execute("SELECT erro FROM publicacoes").fetchone()
    assert "render ausente" in erro["erro"]


# --- resumo -------------------------------------------------------------------

def test_resumo_avisa_do_modo_sombra(conn, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_PUBLISH", False)
    texto = publicar._resumo(conn, {}, {}, agora=AGORA)
    assert "AUTO_PUBLISH=false" in texto
    assert "--reagendar-simulados" in texto


def test_resumo_mostra_agenda_e_quota(conn, clip_publicavel, agendar,
                                      monkeypatch):
    monkeypatch.setattr(settings, "PLATAFORMAS", ["youtube"])
    clip_publicavel()
    contagem = agendar(plataformas=["youtube"])
    texto = publicar._resumo(conn, contagem, {}, agora=AGORA)
    assert "agendados em youtube" in texto
    assert "quota YouTube" in texto
    assert "O que ninguém contou" in texto
