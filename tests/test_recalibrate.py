"""As quatro recalibrações: few-shot, canais, duração e horários."""
import json

import pytest

import settings
from analytics import recalibrate
from db import repositorio


@pytest.fixture
def medir(conn, video):
    """medir(...) -> grava um post publicado com performance medida."""
    contador = {"n": 0}

    def _fn(views=1000, horas=48.0, duracao=45.0, canal="UC_bom",
            canal_nome="Canal Bom", plataforma="youtube", retencao=None,
            hook="ele nunca contou isso", motivo="fecha na virada",
            hora_post=12, score_previsto=8.0):
        contador["n"] += 1
        n = contador["n"]
        fila_id = repositorio.registrar_observacao(
            conn, video(video_id=f"v{n}", canal_id=canal, canal_nome=canal_nome),
            views=1, ganho=1, score=100.0, status=repositorio.STATUS_ANALISADO,
        )
        repositorio.registrar_clips(conn, fila_id, [{
            "inicio_s": 100.0, "fim_s": 100.0 + duracao, "score_claude": 8.0,
            "hook_text": hook, "motivo": motivo, "score_final": score_previsto,
        }])
        clip_id = repositorio.clips_do_video(conn, fila_id)[0]["id"]
        repositorio.registrar_render(conn, clip_id, f"/r/{n}.mp4")
        pub_id = repositorio.agendar_publicacao(
            conn, clip_id, plataforma, f"2026-08-10 {hora_post:02d}:00:00"
        )
        conn.execute(
            "UPDATE publicacoes SET status = ?, publicado_em = ?, id_externo = ?"
            " WHERE id = ?",
            (repositorio.PUB_PUBLICADO, f"2026-08-10 {hora_post:02d}:00:05",
             f"ext{n}", pub_id),
        )
        publicacao = conn.execute(
            "SELECT p.*, c.inicio_s, c.fim_s, c.score_final,"
            "       f.canal_id AS canal_id_fonte, f.canal_nome AS canal_fonte"
            " FROM publicacoes p JOIN clips c ON c.id = p.clip_id"
            " JOIN fila_clips f ON f.id = c.fila_clip_id WHERE p.id = ?",
            (pub_id,),
        ).fetchone()
        repositorio.registrar_resultado(
            conn, publicacao,
            {"views": views, "likes": views // 20, "comentarios": views // 100,
             "retencao": retencao},
            horas_publicado=horas,
        )
        return clip_id

    return _fn


# --- desempenho ---------------------------------------------------------------

def test_desempenho_e_views_por_hora():
    linha = {"views": 4800, "horas_publicado": 48.0}
    assert recalibrate.desempenho(linha, horas_minimas=6) == pytest.approx(100.0)


def test_piso_contem_post_recem_publicado():
    # Sem o piso, 50 views em 20 minutos marcam 150/h e lideram o ranking por
    # ruído de amostragem — a mesma armadilha do score de sourcing.
    novo = recalibrate.desempenho(
        {"views": 50, "horas_publicado": 0.33}, horas_minimas=6
    )
    assert novo == pytest.approx(50 / 6)


def test_post_sem_view_nao_quebra():
    assert recalibrate.desempenho({"views": 0, "horas_publicado": 48}) == 0.0


def test_ranque_e_relativo_a_mediana_da_plataforma(conn, medir):
    # Instagram e YouTube têm escalas de view diferentes; sem normalizar, uma
    # plataforma venceria sempre, independentemente da qualidade do clip.
    for _ in range(3):
        medir(views=100000, plataforma="youtube")
    medir(views=100, plataforma="instagram")
    medir(views=1000, plataforma="instagram")   # o melhor do Instagram
    medir(views=100, plataforma="instagram")

    relativos = recalibrate.ranquear(repositorio.ultimos_resultados(conn))
    topo = relativos[0][1]
    assert topo["plataforma"] == "instagram"


# --- 1. few-shot --------------------------------------------------------------

def test_sem_dado_suficiente_nao_alimenta_o_prompt(conn, medir, monkeypatch):
    # Few-shot com dois clips ensinaria o modelo a imitar o acaso.
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 20)
    for _ in range(3):
        medir()
    assert recalibrate.exemplos_few_shot(conn) == []


def test_topo_vira_exemplo(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 5)
    for i in range(9):
        medir(views=100, hook=f"fraco {i}")
    medir(views=100000, hook="o que ninguém contou")

    exemplos = recalibrate.exemplos_few_shot(conn, percentil=90, maximo=3)
    assert exemplos
    assert exemplos[0]["hook_text"] == "o que ninguém contou"


def test_exemplos_respeitam_o_teto(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 5)
    for i in range(20):
        medir(views=1000 * (i + 1), hook=f"h{i}")
    assert len(recalibrate.exemplos_few_shot(conn, percentil=0, maximo=4)) == 4


def test_exemplo_sem_texto_nao_entra(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 1)
    medir(views=1000, hook="", motivo="")
    assert recalibrate.exemplos_few_shot(conn, percentil=0) == []


def test_post_novo_demais_fica_de_fora(conn, medir, monkeypatch):
    # Post de duas horas ainda está na janela de distribuição inicial;
    # compará-lo com um de três dias mede a idade, não o clip.
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 1)
    medir(views=99999, horas=2.0, hook="novinho")
    assert recalibrate.exemplos_few_shot(conn, idade_minima_h=48) == []


# --- 2. canais ----------------------------------------------------------------

def test_canal_com_poucos_clips_nao_e_julgado(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS_CANAL", 5)
    for _ in range(8):
        medir(views=10000, canal="UC_bom")
    medir(views=1, canal="UC_novo")           # um clip só
    assert [c for c, _n, _m in recalibrate.canais_ruins(conn)] == []


def test_canal_consistentemente_fraco_e_apontado(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS_CANAL", 3)
    for _ in range(6):
        medir(views=10000, canal="UC_bom")
    for _ in range(4):
        medir(views=100, canal="UC_fraco", canal_nome="Canal Fraco")

    ruins = recalibrate.canais_ruins(conn, fracao=0.5)
    assert [c for c, _n, _m in ruins] == ["UC_fraco"]
    assert "4 clips" in ruins[0][2]


def test_usa_mediana_e_nao_media(conn, medir, monkeypatch):
    # Um único clip que viralizou levantaria a média de um canal que não rende
    # — e é justamente o canal que acerta uma vez a cada vinte que se quer
    # desligar.
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS_CANAL", 3)
    for _ in range(6):
        medir(views=10000, canal="UC_bom")
    for _ in range(4):
        medir(views=50, canal="UC_sortudo")
    medir(views=500000, canal="UC_sortudo")   # o golpe de sorte

    assert "UC_sortudo" in [c for c, _n, _m in recalibrate.canais_ruins(conn)]


def test_desativa_no_json_sem_remover_a_linha(conn, tmp_path):
    # Remover apagaria a informação de que o canal já foi avaliado, e ele
    # voltaria na próxima vez que alguém montasse a lista de memória.
    caminho = tmp_path / "canais.json"
    caminho.write_text(json.dumps({"canais": [
        {"id": "UC_bom", "nome": "Bom", "ativo": True},
        {"id": "UC_fraco", "nome": "Fraco", "ativo": True},
    ]}), encoding="utf-8")

    desativados = recalibrate.desativar_canais(
        conn, [("UC_fraco", "Fraco", "mediana baixa")], caminho=str(caminho)
    )
    assert desativados == ["UC_fraco"]

    dados = json.loads(caminho.read_text(encoding="utf-8"))
    por_id = {c["id"]: c for c in dados["canais"]}
    assert len(por_id) == 2                       # ninguém sumiu
    assert por_id["UC_fraco"]["ativo"] is False
    assert por_id["UC_bom"]["ativo"] is True
    assert "mediana baixa" in por_id["UC_fraco"]["_desativado_porque"]


def test_simular_nao_grava(conn, tmp_path):
    caminho = tmp_path / "canais.json"
    original = json.dumps({"canais": [{"id": "UC_fraco", "ativo": True}]})
    caminho.write_text(original, encoding="utf-8")

    recalibrate.desativar_canais(
        conn, [("UC_fraco", "Fraco", "x")], caminho=str(caminho), simular=True
    )
    assert caminho.read_text(encoding="utf-8") == original


def test_canal_ja_desativado_nao_conta_de_novo(conn, tmp_path):
    caminho = tmp_path / "canais.json"
    caminho.write_text(json.dumps({"canais": [
        {"id": "UC_fraco", "ativo": False},
    ]}), encoding="utf-8")
    assert recalibrate.desativar_canais(
        conn, [("UC_fraco", "Fraco", "x")], caminho=str(caminho)
    ) == []


# --- 3. duração ---------------------------------------------------------------

def test_faixa_sem_clips_suficientes_e_ignorada(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS_FAIXA", 5)
    for _ in range(2):
        medir(duracao=35.0)
    assert recalibrate.duracao_ideal(conn) is None


def test_escolhe_a_faixa_de_melhor_desempenho(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS_FAIXA", 3)
    for _ in range(4):
        medir(duracao=35.0, views=100)
    for _ in range(4):
        medir(duracao=55.0, views=50000)

    faixa = recalibrate.duracao_ideal(conn, faixa_s=10.0)
    assert faixa == (50.0, 60.0)


def test_retencao_ganha_de_views_quando_existe(conn, medir, monkeypatch):
    # Views/hora mede alcance; retenção mede o quanto o clip segurou, que é a
    # pergunta certa para escolher duração.
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS_FAIXA", 3)
    for _ in range(4):
        medir(duracao=35.0, views=100000, retencao=0.2)
    for _ in range(4):
        medir(duracao=55.0, views=100, retencao=0.9)

    assert recalibrate.duracao_ideal(conn, faixa_s=10.0) == (50.0, 60.0)


# --- 4. horários --------------------------------------------------------------

def test_horario_com_poucos_posts_fica_de_fora(conn, medir, monkeypatch):
    # Um clip que viralizou às 3 da manhã não é evidência de que 3 da manhã
    # funciona.
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_POSTS_HORARIO", 3)
    medir(hora_post=3, views=999999)
    for _ in range(4):
        medir(hora_post=18, views=1000)

    pesos = recalibrate.pesos_por_horario(conn)
    assert (3, 0) not in pesos
    assert (18, 0) in pesos


def test_peso_reflete_o_desempenho_relativo(conn, medir, monkeypatch):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_POSTS_HORARIO", 2)
    for _ in range(3):
        medir(hora_post=12, views=100)
    for _ in range(3):
        medir(hora_post=21, views=50000)

    pesos = recalibrate.pesos_por_horario(conn)
    assert pesos[(21, 0)] > pesos[(12, 0)]


def test_scheduler_passa_a_usar_os_pesos(conn, medir, monkeypatch):
    # O laço que a etapa 5 deixou aberto: pesos_do_historico devolvia {}.
    from publish import scheduler

    monkeypatch.setattr(settings, "RECALIBRAR_MIN_POSTS_HORARIO", 2)
    for _ in range(3):
        medir(hora_post=21, views=50000)
    assert scheduler.pesos_do_historico(conn) != {}


def test_scheduler_degrada_para_o_relogio_se_o_historico_quebrar(conn,
                                                                monkeypatch,
                                                                caplog):
    # Peso é otimização; derrubar o agendamento por causa dele pararia a
    # publicação inteira.
    from publish import scheduler

    monkeypatch.setattr(
        recalibrate, "pesos_por_horario",
        lambda c, **k: (_ for _ in ()).throw(RuntimeError("histórico corrompido")),
    )
    assert scheduler.pesos_do_historico(conn) == {}
    assert "ordem do" in caplog.text


# --- lições em texto ----------------------------------------------------------

def test_licoes_precisam_de_dado(conn, medir, monkeypatch, cliente_openrouter):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 20)
    medir()
    cliente = cliente_openrouter(['{"licoes": "nunca chamado"}'])
    assert recalibrate.licoes_do_historico(conn, cliente=cliente) == ""
    assert cliente.chamadas == []


def test_licoes_usam_o_modelo_de_recalibracao(conn, medir, monkeypatch,
                                              cliente_openrouter):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 4)
    for i in range(6):
        medir(views=100 * (i + 1), hook=f"h{i}")

    cliente = cliente_openrouter(['{"licoes": "os que abrem com pergunta rendem"}'])
    texto = recalibrate.licoes_do_historico(conn, cliente=cliente)

    assert texto == "os que abrem com pergunta rendem"
    assert cliente.chamadas[0]["model"] == settings.MODEL_RECALIBRATE


def test_falha_das_licoes_nao_derruba_o_resto(conn, medir, monkeypatch,
                                              cliente_openrouter, caplog):
    # Lições são um extra; os few-shot é que são o sinal forte.
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS", 4)
    for i in range(6):
        medir(views=100 * (i + 1))
    cliente = cliente_openrouter([RuntimeError("modelo fora do ar"), RuntimeError("idem")])

    assert recalibrate.licoes_do_historico(conn, cliente=cliente) == ""
    assert "Lições não geradas" in caplog.text


# --- orquestração -------------------------------------------------------------

def test_recalibrar_grava_a_duracao_na_calibracao(conn, medir, monkeypatch,
                                                  cliente_openrouter, tmp_path):
    monkeypatch.setattr(settings, "RECALIBRAR_MIN_CLIPS_FAIXA", 3)
    monkeypatch.setattr(settings, "CANAIS_PATH", str(tmp_path / "canais.json"))
    for _ in range(4):
        medir(duracao=55.0, views=50000)

    recalibrate.recalibrar(conn, cliente_llm=cliente_openrouter(['{"licoes": ""}']))
    assert repositorio.obter_calibracao(conn, settings.CALIBRACAO_DURACAO_MIN) == "50.0"


def test_banco_sem_medicao_deixa_tudo_como_estava(conn, monkeypatch, tmp_path,
                                                  cliente_openrouter):
    # Um banco sem calibração nenhuma se comporta exatamente como antes da
    # etapa 7 — é o que torna a etapa segura de ligar.
    monkeypatch.setattr(settings, "CANAIS_PATH", str(tmp_path / "canais.json"))
    resultado = recalibrate.recalibrar(
        conn, cliente_llm=cliente_openrouter(['{"licoes": ""}'])
    )
    assert resultado == {"exemplos": 0, "canais_desativados": [],
                         "duracao": None, "horarios": 0, "licoes": ""}
    assert repositorio.toda_calibracao(conn) == []
