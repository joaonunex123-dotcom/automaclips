"""Laço principal: isolamento de falha, ordem das etapas e agenda."""
import logging

import pytest

import settings
from orchestrator import main_loop


@pytest.fixture
def etapas_falsas():
    """Etapas de duplo que registram a ordem em que foram chamadas."""
    chamadas = []

    def fabricar(nome, erro=None, resultado=None):
        def etapa(conn):
            chamadas.append(nome)
            if erro:
                raise erro
            return resultado or {nome: 1}
        return (nome, etapa, "INTERVALO_PIPELINE_MIN")

    return chamadas, fabricar


# --- isolamento de falha ------------------------------------------------------

def test_etapa_que_falha_nao_derruba_o_ciclo(conn, etapas_falsas, caplog):
    # Um canal fora do ar não pode impedir que os clips já renderizados sejam
    # publicados.
    chamadas, fabricar = etapas_falsas
    etapas = (
        fabricar("sourcing", erro=RuntimeError("canal fora do ar")),
        fabricar("pipeline"),
        fabricar("publish"),
    )

    resultados = main_loop.ciclo(conn, etapas)

    assert chamadas == ["sourcing", "pipeline", "publish"]
    assert resultados == {"sourcing": False, "pipeline": True, "publish": True}
    assert "canal fora do ar" in caplog.text


def test_nada_escapa_de_rodar_etapa(conn):
    # Uma exceção que suba mata o laço, e laço morto às 2 da manhã só é
    # descoberto quando alguém nota que parou de sair post.
    def explode(conn_):
        raise KeyError("qualquer coisa")

    ok, erro = main_loop.rodar_etapa(conn, "x", explode)
    assert ok is False
    assert isinstance(erro, KeyError)


def test_etapa_bem_sucedida_devolve_o_resultado(conn):
    ok, resultado = main_loop.rodar_etapa(conn, "x", lambda c: {"feito": 3})
    assert ok is True
    assert resultado == {"feito": 3}


# --- ordem --------------------------------------------------------------------

def test_ordem_do_pipeline_e_respeitada():
    # Publicar antes de renderizar não faria sentido: a ordem das etapas é a
    # ordem em que o material existe.
    assert [n for n, _f, _i in main_loop.ETAPAS] == [
        "sourcing", "pipeline", "editing", "publish"
    ]


def test_publish_agenda_antes_de_publicar(conn, monkeypatch):
    # Um clip renderizado agora pode ter horário ainda hoje; inverter faria
    # ele esperar o ciclo seguinte sem motivo.
    ordem = []
    monkeypatch.setattr(main_loop.publicar, "agendar_pendentes",
                        lambda c: ordem.append("agendar") or {})
    monkeypatch.setattr(main_loop.publicar, "processar_vencidas",
                        lambda c: ordem.append("publicar") or {})
    main_loop.etapa_publish(conn)
    assert ordem == ["agendar", "publicar"]


def test_analytics_mede_antes_de_recalibrar(conn, monkeypatch):
    # Recalibrar sobre o histórico de ontem desperdiçaria um dia inteiro de
    # medição que já está disponível.
    from analytics import coletar, recalibrate

    ordem = []
    monkeypatch.setattr(coletar, "coletar",
                        lambda c: ordem.append("medir") or {"youtube": 2})
    monkeypatch.setattr(recalibrate, "recalibrar",
                        lambda c: ordem.append("recalibrar") or {"exemplos": 3})

    resultado = main_loop.etapa_analytics(conn)
    assert ordem == ["medir", "recalibrar"]
    assert resultado["medidos"] == {"youtube": 2}


def test_coleta_falhada_nao_impede_a_recalibracao(conn, monkeypatch, caplog):
    # Dado velho continua sendo dado: o que já foi medido antes ainda serve,
    # e as duas metades são independentes de propósito.
    from analytics import coletar, recalibrate

    def explode(c):
        raise RuntimeError("token vencido")

    monkeypatch.setattr(coletar, "coletar", explode)
    monkeypatch.setattr(recalibrate, "recalibrar", lambda c: {"exemplos": 3})

    resultado = main_loop.etapa_analytics(conn)
    assert resultado["recalibracao"] == {"exemplos": 3}
    assert "token vencido" in caplog.text


# --- sourcing sem canais ------------------------------------------------------

def test_sourcing_sem_canal_ativo_avisa_e_nao_chama_a_api(conn, monkeypatch,
                                                          caplog):
    monkeypatch.setattr(main_loop.canais_mod, "carregar", lambda: [])

    def explode(*a, **k):
        raise AssertionError("não deveria construir cliente")

    monkeypatch.setattr(main_loop.youtube_sourcing, "construir_cliente", explode)
    assert main_loop.etapa_sourcing(conn) == {}
    assert "Nenhum canal ativo" in caplog.text


# --- aviso de estado ----------------------------------------------------------

def test_modo_sombra_e_anunciado(conn, monkeypatch, caplog):
    # Sombra é o estado seguro, então avisa em INFO; publicação real avisa em
    # WARNING, que é a assimetria certa.
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(settings, "AUTO_PUBLISH", False)
    main_loop.avisar_do_estado(conn)
    assert "NADA é publicado" in caplog.text


def test_publicacao_real_e_anunciada_alto(conn, monkeypatch, caplog, tmp_path):
    # A diferença entre sombra e real não aparece na saída normal, e é a
    # diferença entre um arquivo em disco e um post que não dá para desfazer.
    monkeypatch.setattr(settings, "AUTO_PUBLISH", True)
    monkeypatch.setattr(settings, "ARQUIVO_PARAR_PUBLICACAO",
                        str(tmp_path / "PARAR"))
    monkeypatch.setattr(main_loop.preflight, "verificar", lambda c: [])
    main_loop.avisar_do_estado(conn)
    assert "PUBLICAÇÃO REAL ligada" in caplog.text


def test_impedimentos_aparecem_no_aviso(conn, monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(settings, "AUTO_PUBLISH", True)
    monkeypatch.setattr(settings, "ARQUIVO_PARAR_PUBLICACAO",
                        str(tmp_path / "PARAR"))
    monkeypatch.setattr(
        main_loop.preflight, "verificar",
        lambda c: [{"nivel": "bloqueio", "plataforma": "instagram",
                    "mensagem": "CLIPS_BASE_URL vazia", "como_resolver": "preencha"}],
    )
    main_loop.avisar_do_estado(conn)
    assert "impedimentos" in caplog.text
    assert "CLIPS_BASE_URL" in caplog.text


def test_parada_de_emergencia_aparece_no_aviso(conn, monkeypatch, caplog,
                                               tmp_path):
    monkeypatch.setattr(settings, "AUTO_PUBLISH", True)
    parada = tmp_path / "PARAR"
    parada.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "ARQUIVO_PARAR_PUBLICACAO", str(parada))
    main_loop.avisar_do_estado(conn)
    assert "PARADA DE EMERGÊNCIA" in caplog.text


# --- agenda -------------------------------------------------------------------

class AgendadorFalso:
    def __init__(self):
        self.jobs = []

    def add_job(self, funcao, gatilho, **kwargs):
        self.jobs.append({"gatilho": gatilho, **kwargs})


def test_monta_um_job_por_etapa_mais_o_analytics(conn):
    agendador = main_loop.montar_agenda(AgendadorFalso(), conn)
    ids = [j["id"] for j in agendador.jobs]
    assert ids == ["sourcing", "pipeline", "editing", "publish", "analytics"]


def test_intervalos_vem_do_settings(conn):
    agendador = main_loop.montar_agenda(AgendadorFalso(), conn)
    por_id = {j["id"]: j for j in agendador.jobs}
    assert por_id["sourcing"]["minutes"] == settings.INTERVALO_SOURCING_MIN
    assert por_id["publish"]["minutes"] == settings.INTERVALO_PUBLISH_MIN


def test_execucoes_atrasadas_nao_se_acumulam(conn):
    # Sem coalesce, uma etapa lenta acumula disparos; sem max_instances=1, duas
    # cópias mexem na mesma fila ao mesmo tempo.
    agendador = main_loop.montar_agenda(AgendadorFalso(), conn)
    assert all(j["coalesce"] for j in agendador.jobs)
    assert all(j["max_instances"] == 1 for j in agendador.jobs)


def test_analytics_e_diario_no_horario_configurado(conn, monkeypatch):
    monkeypatch.setattr(settings, "HORARIO_ANALYTICS", "05:30")
    agendador = main_loop.montar_agenda(AgendadorFalso(), conn)
    job = [j for j in agendador.jobs if j["id"] == "analytics"][0]
    assert job["gatilho"] == "cron"
    assert (job["hour"], job["minute"]) == (5, 30)


def test_horario_sem_minutos_funciona(conn, monkeypatch):
    monkeypatch.setattr(settings, "HORARIO_ANALYTICS", "6")
    job = [j for j in main_loop.montar_agenda(AgendadorFalso(), conn).jobs
           if j["id"] == "analytics"][0]
    assert (job["hour"], job["minute"]) == (6, 0)
