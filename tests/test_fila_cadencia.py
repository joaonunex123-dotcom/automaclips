"""Fila de clips prontos, cadência de 5 posts/dia, trava, status e quota."""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

import settings
from db import repositorio
from orchestrator import fila, main_loop, status, trava
from publish import quota, scheduler


def _clip_renderizado(conn, video, vid, publicacao=None, inicio=10.0):
    fila_id = repositorio.registrar_observacao(
        conn, video(video_id=vid), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_ANALISADO)
    repositorio.registrar_clips(conn, fila_id, [{
        "inicio_s": inicio, "fim_s": inicio + 30, "score_claude": 9.0,
        "motivo": "m", "hook_text": "h", "picos_energia": 0,
        "score_final": 9.0, "picos_instantes": []}])
    clip_id = repositorio.clips_do_video(conn, fila_id)[0]["id"]
    repositorio.registrar_render(conn, clip_id, f"/r/{vid}.mp4")
    if publicacao:
        repositorio.agendar_publicacao(conn, clip_id, "youtube",
                                       "2026-10-12 08:00:00", titulo="t")
        conn.execute("UPDATE publicacoes SET status = ? WHERE clip_id = ?",
                     (publicacao, clip_id))
        if publicacao == "publicado":
            conn.execute("UPDATE publicacoes SET publicado_em = "
                         "datetime('now') WHERE clip_id = ?", (clip_id,))
    return clip_id


# --- padrões do pedido ----------------------------------------------------------

def test_cinco_horarios_espacados_entre_8_e_21():
    horarios = scheduler.horarios_configurados(settings.HORARIOS_PADRAO)
    assert len(horarios) == 5
    assert horarios[0] == (8, 0) and horarios[-1] == (21, 0)
    minutos = [h * 60 + m for h, m in horarios]
    passos = [b - a for a, b in zip(minutos, minutos[1:])]
    assert min(passos) >= settings.INTERVALO_MINIMO_MIN
    assert max(passos) - min(passos) <= 5          # praticamente uniforme


def test_padrao_publica_ate_cinco_por_dia():
    assert settings.POSTS_POR_DIA == 5
    assert settings.max_posts_dia("youtube") >= 5


def test_fuso_padrao_e_sao_paulo():
    assert settings.FUSO_AGENDAMENTO == "America/Sao_Paulo"


def test_agenda_de_um_dia_enche_os_cinco_slots(conn, monkeypatch):
    monkeypatch.setattr(settings, "AGENDAMENTO_MAX_DIAS", 2)
    agora = datetime(2026, 10, 12, 6, 0, 0)
    slots = scheduler.proximos_slots(conn, "youtube", 5, agora=agora)
    assert [s[11:16] for s in slots] == ["08:00", "11:15", "14:30", "17:45",
                                         "21:00"]
    assert {s[:10] for s in slots} == {"2026-10-12"}


def test_sexto_clip_do_dia_vai_para_o_dia_seguinte(conn, monkeypatch):
    agora = datetime(2026, 10, 12, 6, 0, 0)
    slots = scheduler.proximos_slots(conn, "youtube", 6, agora=agora)
    assert len(slots) == 6 and slots[5].startswith("2026-10-13")


# --- quota ----------------------------------------------------------------------

def test_cinco_uploads_cabem_e_o_sexto_nao(conn):
    agora = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    margem = settings.YOUTUBE_QUOTA_MARGEM
    for i in range(5):
        assert quota.cabe(conn, agora=agora, margem=margem), i
        quota.registrar(conn, agora=agora)
    assert quota.usada(conn, agora=agora) == 8000
    assert not quota.cabe(conn, agora=agora, margem=margem)
    # sem margem o sexto ainda "cabe" na conta crua (2000 >= 1600): é a margem
    # que protege o sourcing e as capas.
    assert quota.cabe(conn, agora=agora)


def test_uploads_restantes_respeita_a_margem(conn):
    agora = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    assert quota.uploads_restantes(conn, agora=agora) == 5
    quota.registrar(conn, agora=agora)
    assert quota.uploads_restantes(conn, agora=agora) == 4


def test_margem_de_quota_padrao_deixa_2000_unidades(conn):
    gasto_de_5 = 5 * settings.YOUTUBE_CUSTO_UPLOAD
    assert settings.YOUTUBE_QUOTA_DIARIA - gasto_de_5 == 2000


# --- fila de clips prontos ------------------------------------------------------

def test_conta_clips_renderizados_ainda_nao_publicados(conn, video):
    _clip_renderizado(conn, video, "a1")
    _clip_renderizado(conn, video, "a2", publicacao="agendado")
    _clip_renderizado(conn, video, "a3", publicacao="simulado")
    _clip_renderizado(conn, video, "a4", publicacao="publicado")
    _clip_renderizado(conn, video, "a5", publicacao="falha")
    assert fila.clips_prontos(conn) == 3
    assert fila.clips_agendados(conn) == 1


def test_fila_baixa_inicia_video_novo(conn, video):
    chamadas = []

    def processar_fila(conn_, **kw):
        chamadas.append(kw)
        return {"analisado": 1} if repositorio.STATUS_DESCOBERTO in kw.get(
            "estados", ()) else {}

    r = fila.reabastecer(conn, processar_fila=processar_fila, limiar=8,
                         videos_por_ciclo=1)
    assert r == {"analisado": 1}
    assert chamadas[0]["estados"] == fila.EM_ANDAMENTO
    assert chamadas[1]["estados"] == (repositorio.STATUS_DESCOBERTO,)
    assert chamadas[1]["limite"] == 1


def test_fila_cheia_so_termina_o_que_esta_em_andamento(conn, video):
    for i in range(8):
        _clip_renderizado(conn, video, f"c{i}")
    chamadas = []

    def processar_fila(conn_, **kw):
        chamadas.append(kw["estados"])
        return {}

    r = fila.reabastecer(conn, processar_fila=processar_fila, limiar=8)
    assert chamadas == [fila.EM_ANDAMENTO]
    assert r == {"fila_ok": 8}


def test_orcamento_estourado_para_tudo(conn, monkeypatch):
    monkeypatch.setattr(settings, "ORCAMENTO_USD", 1.0)
    repositorio.registrar_custo(conn, "whisper", 1.5, referencia="x")
    chamadas = []
    r = fila.reabastecer(conn, processar_fila=lambda *a, **k: chamadas.append(1))
    assert r == {"orcamento_estourado": 1} and chamadas == []


def test_gasto_durante_o_ciclo_impede_novo_video(conn, monkeypatch):
    monkeypatch.setattr(settings, "ORCAMENTO_USD", 1.0)
    chamadas = []

    def processar_fila(conn_, **kw):
        chamadas.append(kw["estados"])
        repositorio.registrar_custo(conn, "llm", 2.0)      # estoura no meio
        return {}

    r = fila.reabastecer(conn, processar_fila=processar_fila, limiar=8)
    assert chamadas == [fila.EM_ANDAMENTO]
    assert r["orcamento_estourado"] == 1


def test_limiar_zero_volta_ao_comportamento_antigo(conn, monkeypatch):
    monkeypatch.setattr(settings, "FILA_MINIMA_CLIPS", 0)
    visto = {}
    monkeypatch.setattr(main_loop.processar, "processar_fila",
                        lambda c, **kw: visto.update(kw) or {"x": 1})
    assert main_loop.etapa_pipeline(conn) == {"x": 1}
    assert "estados" not in visto


def test_video_em_andamento_e_retomado_apos_reinicio(conn, video):
    """O estado vive no banco: um 'baixado' é retomado, não refeito."""
    fila_id = repositorio.registrar_observacao(
        conn, video(video_id="meio1"), views=1, ganho=1, score=9.0,
        status=repositorio.STATUS_BAIXADO)
    pend = fila_id
    from pipeline import processar
    linhas = processar.fila_pendente(conn, estados=fila.EM_ANDAMENTO)
    assert [l["id"] for l in linhas] == [pend]
    assert processar.fila_pendente(
        conn, estados=(repositorio.STATUS_DESCOBERTO,)) == []


# --- trava ----------------------------------------------------------------------

def test_segunda_instancia_e_recusada(tmp_path):
    caminho = str(tmp_path / "o.lock")
    with trava.Trava(caminho):
        with pytest.raises(trava.OutraInstancia):
            trava.Trava(caminho).adquirir()
    # liberada: pode adquirir de novo
    with trava.Trava(caminho):
        pass


def test_trava_de_processo_morto_nao_prende(tmp_path):
    """flock é solto pelo SO; o caminho por pid detecta processo morto."""
    caminho = str(tmp_path / "o.lock")
    t = trava.Trava(caminho)
    (tmp_path / "o.lock").write_text("999999\n")
    t._adquirir_por_pid()
    assert t._por_pid
    t.liberar()
    assert not os.path.exists(caminho)


def test_trava_por_pid_recusa_processo_vivo(tmp_path):
    caminho = tmp_path / "o.lock"
    caminho.write_text(f"{os.getpid()}\n")
    with pytest.raises(trava.OutraInstancia):
        trava.Trava(str(caminho))._adquirir_por_pid()


def test_main_sai_zero_quando_ha_outra_instancia(conn, monkeypatch, caplog):
    segura = trava.Trava(settings.TRAVA_PATH)
    segura.adquirir()
    try:
        assert main_loop.main(["--uma-vez"]) == 0
    finally:
        segura.liberar()
    assert "outro orquestrador" in caplog.text


def test_trava_funciona_entre_processos(tmp_path):
    caminho = str(tmp_path / "x.lock")
    codigo = (
        "import sys; sys.path.insert(0, %r);"
        "from orchestrator.trava import Trava, OutraInstancia;"
        "t = Trava(%r)\n"
        "try:\n    t.adquirir(); print('LIVRE')\n"
        "except OutraInstancia:\n    print('PRESA')\n"
    ) % (os.getcwd(), caminho)
    with trava.Trava(caminho):
        saida = subprocess.run([sys.executable, "-c", codigo],
                               capture_output=True, text=True).stdout
    assert "PRESA" in saida


# --- status.json ----------------------------------------------------------------

def test_status_traz_fila_quota_espaco_e_token(conn, video, monkeypatch,
                                               tmp_path):
    _clip_renderizado(conn, video, "s1")
    token = tmp_path / "t.json"
    token.write_text("{}")
    monkeypatch.setattr(settings, "YOUTUBE_OAUTH_TOKEN", str(token))
    monkeypatch.setattr(settings, "YOUTUBE_TOKEN_VENCE_EM", "2026-10-18")
    dados = status.montar(
        conn, etapas={"pipeline": {"ok": True, "erro": None}},
        inicio=datetime(2026, 10, 11, 8, 0), fim=datetime(2026, 10, 11, 8, 5),
        agora=datetime(2026, 10, 11, 8, 5))
    assert dados["fila"]["prontos"] == 1 and dados["fila"]["minimo"] == 8
    assert dados["quota_youtube"]["limite"] == 10000
    assert dados["quota_youtube"]["uploads_restantes"] == 5
    assert dados["espaco_livre_gb"] > 0
    assert dados["token_youtube"]["dias_restantes"] == 7
    assert dados["token_youtube"]["alerta"] is True
    assert any("token vence em 7" in a for a in dados["alertas"])
    assert any("fila baixa" in a for a in dados["alertas"])
    assert dados["ultima_execucao"]["etapas"]["pipeline"]["ok"] is True
    assert dados["modo"]["auto_publish"] is False


def test_status_alerta_etapa_que_falhou(conn):
    dados = status.montar(conn, etapas={"editing": {"ok": False,
                                                     "erro": "ffmpeg"}})
    assert any("etapa editing falhou: ffmpeg" in a for a in dados["alertas"])


def test_token_sem_data_conhecida_nao_alarma(monkeypatch, tmp_path):
    token = tmp_path / "t.json"
    token.write_text("{}")
    monkeypatch.setattr(settings, "YOUTUBE_TOKEN_VENCE_EM", "")
    info = status.token_youtube(date(2026, 10, 11), str(token))
    assert info["alerta"] is False and "desconhecido" in info["aviso"]


def test_token_vencido(monkeypatch, tmp_path):
    token = tmp_path / "t.json"
    token.write_text("{}")
    monkeypatch.setattr(settings, "YOUTUBE_TOKEN_VENCE_EM", "2026-10-01")
    info = status.token_youtube(date(2026, 10, 11), str(token))
    assert info["alerta"] and "VENCIDO" in info["aviso"]


def test_escrita_e_atomica_e_json_valido(tmp_path):
    destino = str(tmp_path / "ops" / "status.json")
    assert status.escrever({"a": 1, "b": "ç"}, destino) == destino
    assert json.load(open(destino, encoding="utf-8")) == {"a": 1, "b": "ç"}
    assert not os.path.exists(destino + ".tmp")


def test_ciclo_escreve_o_status(conn, tmp_path):
    def etapa(c):
        return {"ok": 1}
    main_loop.ciclo(conn, (("x", etapa, "INTERVALO_PIPELINE_MIN"),))
    dados = json.load(open(settings.STATUS_PATH, encoding="utf-8"))
    assert dados["ultima_execucao"]["etapas"]["x"]["ok"] is True
    assert dados["ultima_execucao"]["fim"]


# --- fuso -----------------------------------------------------------------------

def test_fuso_confere_detecta_diferenca():
    agora = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    ok, local, conf = scheduler.fuso_confere(agora, "America/Sao_Paulo")
    assert conf == -3.0
    ok_los, _, conf_los = scheduler.fuso_confere(agora, "America/Los_Angeles")
    assert conf_los == -7.0
