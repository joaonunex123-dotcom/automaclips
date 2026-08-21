"""Coleta de métricas dos posts publicados."""
import pytest

import settings
from analytics import coletar
from db import repositorio


class ClienteEstatisticasFalso:
    """Duplo do videos().list — a mesma superfície que o sourcing usa."""

    def __init__(self, estatisticas=None):
        self._estatisticas = estatisticas or {}
        self.chamadas = []

    def videos(self):
        return self

    def list(self, part=None, id="", **kwargs):
        self.chamadas.append({"part": part, "ids": id.split(",")})
        return self

    def execute(self):
        ids = self.chamadas[-1]["ids"]
        return {"items": [
            {"id": v, "statistics": self._estatisticas[v]}
            for v in ids if v in self._estatisticas
        ]}


@pytest.fixture
def publicado(conn, video):
    """Um post realmente publicado, pronto para ser medido."""
    contador = {"n": 0}

    def _fn(plataforma="youtube", id_externo=None, horas=48.0,
            status=repositorio.PUB_PUBLICADO):
        contador["n"] += 1
        n = contador["n"]
        fila_id = repositorio.registrar_observacao(
            conn, video(video_id=f"v{n}"), views=1, ganho=1, score=1.0,
            status=repositorio.STATUS_ANALISADO,
        )
        repositorio.registrar_clips(conn, fila_id, [{
            "inicio_s": 100.0, "fim_s": 145.0, "score_claude": 8.0,
            "score_final": 8.5,
        }])
        clip_id = repositorio.clips_do_video(conn, fila_id)[0]["id"]
        pub_id = repositorio.agendar_publicacao(
            conn, clip_id, plataforma, "2026-08-10 12:00:00"
        )
        conn.execute(
            "UPDATE publicacoes SET status = ?, publicado_em ="
            "  datetime('now', 'localtime', ?), id_externo = ? WHERE id = ?",
            (status, f"-{horas} hours", id_externo or f"ext{n}", pub_id),
        )
        return pub_id

    return _fn


# --- elegibilidade ------------------------------------------------------------

def test_post_simulado_nunca_e_medido(conn, publicado):
    # Ele não existe em plataforma nenhuma; medi-lo devolveria zero para
    # sempre, e esses zeros entrariam na média puxando a recalibração.
    publicado(status=repositorio.PUB_SIMULADO)
    assert repositorio.publicacoes_para_medir(conn) == []


def test_post_novo_demais_espera(conn, publicado):
    publicado(horas=2)
    assert repositorio.publicacoes_para_medir(conn, idade_minima_h=48) == []
    assert len(repositorio.publicacoes_para_medir(conn, idade_minima_h=1)) == 1


def test_post_velho_demais_para_de_ser_remedido(conn, publicado):
    # Depois de um mês a curva estabilizou e cada medição gasta quota para
    # confirmar o que já se sabe.
    publicado(horas=1000)
    cliente = ClienteEstatisticasFalso()
    assert coletar.coletar(conn, cliente_youtube=cliente, idade_maxima_h=720) == {}


def test_a_idade_do_post_e_calculada(conn, publicado):
    publicado(horas=50)
    linha = repositorio.publicacoes_para_medir(conn, idade_minima_h=1)[0]
    assert linha["horas_publicado"] == pytest.approx(50, abs=1)


# --- YouTube ------------------------------------------------------------------

def test_le_estatisticas_publicas(conn, publicado):
    # Views de vídeo público saem do mesmo videos.list do sourcing — 1 unidade,
    # sem precisar da autorização OAuth do canal.
    publicado(id_externo="abc123")
    cliente = ClienteEstatisticasFalso({
        "abc123": {"viewCount": "5000", "likeCount": "300", "commentCount": "42"}
    })

    contagem = coletar.coletar(conn, cliente_youtube=cliente, idade_minima_h=1)

    assert contagem == {"youtube": 1}
    assert cliente.chamadas[0]["part"] == "statistics"
    linha = repositorio.ultimos_resultados(conn)[0]
    assert (linha["views"], linha["likes"], linha["comentarios"]) == (5000, 300, 42)


def test_metrica_ausente_vira_zero_e_nao_erro(conn, publicado):
    # A API omite o campo quando é zero, e renomeia métrica com o tempo.
    publicado(id_externo="abc123")
    cliente = ClienteEstatisticasFalso({"abc123": {"viewCount": "10"}})
    coletar.coletar(conn, cliente_youtube=cliente, idade_minima_h=1)
    linha = repositorio.ultimos_resultados(conn)[0]
    assert linha["views"] == 10 and linha["likes"] == 0


def test_video_removido_e_pulado_sem_derrubar(conn, publicado, caplog):
    publicado(id_externo="sumiu")
    publicado(id_externo="existe")
    cliente = ClienteEstatisticasFalso({"existe": {"viewCount": "77"}})

    assert coletar.coletar(conn, cliente_youtube=cliente,
                           idade_minima_h=1) == {"youtube": 1}


def test_retencao_desligada_grava_nulo(conn, publicado, monkeypatch):
    # averageViewPercentage exige a YouTube Analytics API, outro escopo OAuth.
    monkeypatch.setattr(settings, "ANALYTICS_RETENCAO", False)
    publicado(id_externo="abc")
    cliente = ClienteEstatisticasFalso({"abc": {"viewCount": "100"}})
    coletar.coletar(conn, cliente_youtube=cliente, idade_minima_h=1)
    assert repositorio.ultimos_resultados(conn)[0]["retencao"] is None


def test_retencao_ligada_sem_cliente_nao_quebra(conn, publicado, monkeypatch):
    monkeypatch.setattr(settings, "ANALYTICS_RETENCAO", True)
    publicado(id_externo="abc")
    cliente = ClienteEstatisticasFalso({"abc": {"viewCount": "100"}})
    coletar.coletar(conn, cliente_youtube=cliente, cliente_analytics=None,
                    idade_minima_h=1)
    assert repositorio.ultimos_resultados(conn)[0]["retencao"] is None


def test_consome_quota_da_leitura(conn, publicado):
    from publish import quota

    publicado(id_externo="abc")
    cliente = ClienteEstatisticasFalso({"abc": {"viewCount": "1"}})
    coletar.coletar(conn, cliente_youtube=cliente, idade_minima_h=1)
    # videos.list custa 1 unidade — três ordens de grandeza abaixo de um upload.
    assert 0 < quota.usada(conn) < settings.YOUTUBE_CUSTO_UPLOAD


# --- Instagram ----------------------------------------------------------------

def test_le_campos_e_insights(conn, publicado, http_falso, resposta_falsa,
                              monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "tok")
    publicado(plataforma="instagram", id_externo="media9")
    http = http_falso([
        resposta_falsa({"access_token": "novo", "expires_in": 5184000}),
        resposta_falsa({"like_count": 120, "comments_count": 8}),
        resposta_falsa({"data": [{"name": "plays", "values": [{"value": 9000}]}]}),
    ])

    contagem = coletar.coletar(conn, http=http, idade_minima_h=1)

    assert contagem == {"instagram": 1}
    linha = repositorio.ultimos_resultados(conn)[0]
    assert (linha["views"], linha["likes"], linha["comentarios"]) == (9000, 120, 8)


def test_insights_indisponivel_mantem_likes(conn, publicado, http_falso,
                                            resposta_falsa, monkeypatch):
    # Likes e comentários já foram lidos e continuam valendo.
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "tok")
    publicado(plataforma="instagram", id_externo="media9")
    http = http_falso([
        resposta_falsa({"access_token": "novo", "expires_in": 5184000}),
        resposta_falsa({"like_count": 120, "comments_count": 8}),
        resposta_falsa({"error": {"message": "mídia antiga"}}, status_code=400),
    ])

    coletar.coletar(conn, http=http, idade_minima_h=1)
    linha = repositorio.ultimos_resultados(conn)[0]
    assert linha["likes"] == 120 and linha["views"] == 0


def test_midia_sumida_nao_derruba_as_outras(conn, publicado, http_falso,
                                            resposta_falsa, monkeypatch, caplog):
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "tok")
    publicado(plataforma="instagram", id_externo="sumiu")
    publicado(plataforma="instagram", id_externo="existe")
    http = http_falso([
        resposta_falsa({"access_token": "novo", "expires_in": 5184000}),
        resposta_falsa({"error": {"message": "não existe"}}, status_code=404),
        resposta_falsa({"like_count": 5, "comments_count": 1}),
        resposta_falsa({"data": []}),
    ])

    assert coletar.coletar(conn, http=http, idade_minima_h=1) == {"instagram": 1}


# --- histórico ----------------------------------------------------------------

def test_cada_coleta_anexa_uma_medicao(conn, publicado):
    # Um clip medido uma vez só não diz se cresceu ou estagnou.
    publicado(id_externo="abc")
    cliente = ClienteEstatisticasFalso({"abc": {"viewCount": "100"}})
    coletar.coletar(conn, cliente_youtube=cliente, idade_minima_h=1)
    cliente = ClienteEstatisticasFalso({"abc": {"viewCount": "900"}})
    coletar.coletar(conn, cliente_youtube=cliente, idade_minima_h=1)

    assert repositorio.contar_resultados(conn) == 2
    # ...mas a recalibração vê só a mais recente de cada post.
    ultimos = repositorio.ultimos_resultados(conn)
    assert len(ultimos) == 1 and ultimos[0]["views"] == 900


def test_score_previsto_fica_congelado(conn, publicado):
    # Reprocessar um vídeo recria as linhas de `clips`; buscar o score por JOIN
    # meses depois traria o valor recalibrado, não o que a seleção apostou.
    publicado(id_externo="abc")
    cliente = ClienteEstatisticasFalso({"abc": {"viewCount": "100"}})
    coletar.coletar(conn, cliente_youtube=cliente, idade_minima_h=1)
    assert repositorio.ultimos_resultados(conn)[0]["score_previsto"] == 8.5


def test_sem_post_elegivel_nao_chama_api_nenhuma(conn):
    cliente = ClienteEstatisticasFalso()
    assert coletar.coletar(conn, cliente_youtube=cliente) == {}
    assert cliente.chamadas == []
