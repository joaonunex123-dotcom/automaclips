"""Orquestração da etapa 2: retomada, isolamento de falha e ordem da fila."""
import pytest

import settings
from db import repositorio
from pipeline import processar


@pytest.fixture
def enfileirar(conn, video):
    """enfileirar(video_id, score, status) -> id em fila_clips."""
    def _fn(video_id="vid1", score=1000.0, status=repositorio.STATUS_DESCOBERTO,
            **kwargs):
        return repositorio.registrar_observacao(
            conn, video(video_id=video_id, **kwargs),
            views=1, ganho=1, score=score, status=status,
        )
    return _fn


@pytest.fixture
def injecoes(transcricao, trecho):
    """Os quatro pontos caros do pipeline, substituídos por duplos."""
    estado = {"baixou": [], "transcreveu": [], "detectou": [], "picos": []}

    def baixar(video_id):
        estado["baixou"].append(video_id)
        return {
            "video_path": f"/tmp/{video_id}.mp4",
            "audio_path": f"/tmp/{video_id}.wav",
            "duracao_real_s": 600.0,
        }

    def transcrever(audio_path, video_id):
        estado["transcreveu"].append(video_id)
        return (
            f"/tmp/{video_id}.json",
            transcricao((10.0, 14.0, "boa noite"), (100.0, 145.0, "a virada")),
        )

    def detectar(texto):
        estado["detectou"].append(texto)
        return [trecho(inicio_s=100.0, fim_s=145.0, score_claude=9.0)]

    def picos_do_audio(audio_path):
        estado["picos"].append(audio_path)
        return [110.0, 125.0, 140.0]

    return estado, {
        "baixar": baixar, "transcrever": transcrever,
        "detectar": detectar, "picos_do_audio": picos_do_audio,
    }


# --- caminho feliz ------------------------------------------------------------

def test_leva_o_video_de_descoberto_a_analisado(conn, enfileirar, injecoes):
    estado, duplos = injecoes
    clip_id = enfileirar()

    contagem = processar.processar_fila(conn, **duplos)

    assert contagem == {repositorio.STATUS_ANALISADO: 1}
    assert repositorio.buscar_video(conn, "youtube", "vid1")["status"] == (
        repositorio.STATUS_ANALISADO
    )
    assert estado["baixou"] == ["vid1"]
    assert estado["transcreveu"] == ["vid1"]

    clips = repositorio.clips_do_video(conn, clip_id,
                                       status=repositorio.CLIP_SELECIONADO)
    assert len(clips) == 1
    assert clips[0]["picos_energia"] == 3
    assert clips[0]["hook_text"] == "ele nunca contou isso"


def test_grava_os_artefatos_de_midia(conn, enfileirar, injecoes):
    _, duplos = injecoes
    clip_id = enfileirar()
    processar.processar_fila(conn, **duplos)

    midia = repositorio.midia(conn, clip_id)
    assert midia["video_path"] == "/tmp/vid1.mp4"
    assert midia["audio_path"] == "/tmp/vid1.wav"
    assert midia["transcricao_path"] == "/tmp/vid1.json"
    assert midia["duracao_real_s"] == 600.0
    assert midia["baixado_em"] and midia["transcrito_em"]


def test_a_transcricao_enviada_ao_claude_tem_timestamps(conn, enfileirar, injecoes):
    estado, duplos = injecoes
    enfileirar()
    processar.processar_fila(conn, **duplos)
    assert estado["detectou"][0] == "[10.0] boa noite\n[100.0] a virada"


def test_nenhum_trecho_no_corte_vira_sem_clips(conn, enfileirar, injecoes, trecho):
    _, duplos = injecoes
    duplos["detectar"] = lambda texto: [trecho(score_claude=1.0)]
    clip_id = enfileirar()

    contagem = processar.processar_fila(conn, **duplos)

    assert contagem == {repositorio.STATUS_SEM_CLIPS: 1}
    assert repositorio.buscar_video(conn, "youtube", "vid1")["status"] == (
        repositorio.STATUS_SEM_CLIPS
    )
    # O trecho reprovado continua gravado — é a matéria-prima da etapa 7.
    assert len(repositorio.clips_do_video(conn, clip_id)) == 1


# --- retomada -----------------------------------------------------------------

def test_video_ja_baixado_nao_e_rebaixado(conn, enfileirar, injecoes):
    estado, duplos = injecoes
    clip_id = enfileirar(status=repositorio.STATUS_BAIXADO)
    repositorio.registrar_midia(
        conn, clip_id, video_path="/ja/vid1.mp4", audio_path="/ja/vid1.wav",
        duracao_real_s=600.0,
    )

    processar.processar_fila(conn, **duplos)

    assert estado["baixou"] == []
    assert estado["transcreveu"] == ["vid1"]
    assert estado["picos"] == ["/ja/vid1.wav"]


def test_video_ja_transcrito_nao_e_retranscrito(conn, enfileirar, injecoes,
                                                transcricao, monkeypatch):
    estado, duplos = injecoes
    clip_id = enfileirar(status=repositorio.STATUS_TRANSCRITO)
    repositorio.registrar_midia(
        conn, clip_id, video_path="/ja/vid1.mp4", audio_path="/ja/vid1.wav",
        transcricao_path="/ja/vid1.json", duracao_real_s=600.0,
    )
    monkeypatch.setattr(
        processar.transcribe_mod, "carregar",
        lambda caminho: transcricao((100.0, 145.0, "do disco")),
    )

    processar.processar_fila(conn, **duplos)

    assert estado["baixou"] == [] and estado["transcreveu"] == []
    assert estado["detectou"] == ["[100.0] do disco"]


# --- isolamento de falha ------------------------------------------------------

def test_falha_num_video_nao_derruba_a_fila(conn, enfileirar, injecoes):
    estado, duplos = injecoes
    enfileirar(video_id="quebrado", score=9999.0)
    enfileirar(video_id="bom", score=10.0)

    def baixar(video_id):
        if video_id == "quebrado":
            raise RuntimeError("vídeo privado")
        estado["baixou"].append(video_id)
        return {
            "video_path": f"/tmp/{video_id}.mp4",
            "audio_path": f"/tmp/{video_id}.wav",
            "duracao_real_s": 600.0,
        }

    duplos["baixar"] = baixar
    contagem = processar.processar_fila(conn, **duplos)

    assert contagem == {
        repositorio.STATUS_FALHA: 1, repositorio.STATUS_ANALISADO: 1,
    }
    quebrado = repositorio.buscar_video(conn, "youtube", "quebrado")
    assert quebrado["status"] == repositorio.STATUS_FALHA
    assert "vídeo privado" in quebrado["erro"]
    assert repositorio.buscar_video(conn, "youtube", "bom")["status"] == (
        repositorio.STATUS_ANALISADO
    )


def test_sucesso_limpa_o_erro_anterior(conn, enfileirar, injecoes):
    _, duplos = injecoes
    clip_id = enfileirar()
    repositorio.marcar_erro(conn, clip_id, "falha antiga")

    processar.processar_fila(conn, **duplos)

    assert repositorio.buscar_video(conn, "youtube", "vid1")["erro"] is None


# --- seleção da fila ----------------------------------------------------------

def test_fila_vem_ordenada_pelo_score(conn, enfileirar):
    enfileirar(video_id="medio", score=500.0)
    enfileirar(video_id="alto", score=5000.0)
    enfileirar(video_id="baixo", score=50.0)

    ids = [l["video_id"] for l in processar.fila_pendente(conn)]
    assert ids == ["alto", "medio", "baixo"]


def test_fila_pega_todos_os_estados_pendentes(conn, enfileirar):
    enfileirar(video_id="a", status=repositorio.STATUS_DESCOBERTO)
    enfileirar(video_id="b", status=repositorio.STATUS_BAIXADO)
    enfileirar(video_id="c", status=repositorio.STATUS_TRANSCRITO)
    assert len(processar.fila_pendente(conn)) == 3


def test_fila_ignora_o_que_ja_foi_processado(conn, enfileirar):
    enfileirar(video_id="pronto", status=repositorio.STATUS_ANALISADO)
    enfileirar(video_id="vazio", status=repositorio.STATUS_SEM_CLIPS)
    enfileirar(video_id="fraco", status=repositorio.STATUS_ABAIXO_DO_LIMIAR)
    assert processar.fila_pendente(conn) == []


def test_falha_so_volta_com_retentar(conn, enfileirar):
    # Sem isso, um vídeo quebrado seria retentado a cada execução, para sempre,
    # gastando download e API no mesmo erro.
    enfileirar(video_id="quebrado", status=repositorio.STATUS_FALHA)
    assert processar.fila_pendente(conn) == []
    assert len(processar.fila_pendente(conn, retentar=True)) == 1


def test_limite_por_execucao(conn, enfileirar):
    for i in range(5):
        enfileirar(video_id=f"v{i}", score=float(i))
    assert len(processar.fila_pendente(conn, limite=2)) == 2


def test_limite_padrao_vem_de_settings(conn, enfileirar, injecoes, monkeypatch):
    _, duplos = injecoes
    monkeypatch.setattr(settings, "PIPELINE_MAX_VIDEOS", 1)
    for i in range(3):
        enfileirar(video_id=f"v{i}", score=float(i))

    contagem = processar.processar_fila(conn, **duplos)
    assert sum(contagem.values()) == 1


# --- resumo -------------------------------------------------------------------

def test_resumo_e_legivel(conn, enfileirar, injecoes):
    _, duplos = injecoes
    enfileirar()
    contagem = processar.processar_fila(conn, **duplos)

    texto = processar._resumo(conn, contagem)
    assert "analisados  1" in texto
    assert "topo da fila de edição" in texto
    assert "ele nunca contou isso" in texto
