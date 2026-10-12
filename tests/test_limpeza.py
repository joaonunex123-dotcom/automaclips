"""Limpeza do vídeo-fonte: só o que é do pipeline, só depois de tudo pronto."""
import os
from types import SimpleNamespace

import pytest

import settings
from db import repositorio
from pipeline import limpeza, processar

SAIDA_OK = (
    "Duration: 00:00:30.04, start: 0.000000\n"
    "  Stream #0:0: Video: h264, yuv420p, 1080x1920, 30 fps\n"
    "  Stream #0:1: Audio: aac, 48000 Hz, stereo\n")


def _ffmpeg(texto=SAIDA_OK):
    return lambda cmd, **kw: SimpleNamespace(returncode=1, stderr=texto)


@pytest.fixture
def cenario(conn, video, tmp_path):
    """Um podcast com 2 clips renderizados e a fonte + intermediários em disco."""
    downloads = tmp_path / "downloads"
    render = tmp_path / "render"
    (downloads / "transcricoes").mkdir(parents=True)
    render.mkdir()
    vid = "pbPDFVURR0Q"
    arquivos = {}
    for nome, conteudo in {
        f"{vid}.mp4": b"v" * 5000, f"{vid}.wav": b"w" * 3000,
        f"{vid}.mp3": b"m" * 1000, f"{vid}.parte00.mp3": b"p" * 500,
        f"{vid}.mp4.part": b"x" * 100,
        # de OUTRO vídeo e de prefixo parecido: nunca podem ser tocados
        "outroVideo.mp4": b"o" * 700, f"{vid}XX.mp4": b"y" * 50,
    }.items():
        caminho = downloads / nome
        caminho.write_bytes(conteudo)
        arquivos[nome] = str(caminho)
    transcricao = downloads / "transcricoes" / f"{vid}.json"
    transcricao.write_text("{}", encoding="utf-8")

    fila_id = repositorio.registrar_observacao(
        conn, video(video_id=vid), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_ANALISADO)
    repositorio.registrar_midia(
        conn, fila_id, video_path=arquivos[f"{vid}.mp4"],
        audio_path=arquivos[f"{vid}.wav"], transcricao_path=str(transcricao),
        duracao_real_s=600.0)
    repositorio.registrar_clips(conn, fila_id, [
        {"inicio_s": 10.0, "fim_s": 40.0, "score_claude": 9.0, "motivo": "a",
         "hook_text": "a", "picos_energia": 0, "score_final": 9.0,
         "picos_instantes": []},
        {"inicio_s": 100.0, "fim_s": 130.0, "score_claude": 8.0, "motivo": "b",
         "hook_text": "b", "picos_energia": 0, "score_final": 8.0,
         "picos_instantes": []}])
    ids = [c["id"] for c in repositorio.clips_do_video(conn, fila_id)]
    clips = []
    for cid in ids:
        mp4 = render / f"{vid}_{cid}.mp4"
        mp4.write_bytes(b"clip" * 100)
        capa = render / f"{vid}_{cid}_capa"
        capa.mkdir()
        png = capa / "capa_1080x1920.png"
        png.write_bytes(b"png")
        ass = render / f"{vid}_{cid}.ass"
        ass.write_text("ass", encoding="utf-8")
        repositorio.registrar_render(conn, cid, str(mp4), duracao_s=30.0,
                                     capa_path=str(png))
        clips.append({"id": cid, "mp4": mp4, "png": png, "ass": ass})
    linha = conn.execute("SELECT * FROM fila_clips WHERE id = ?",
                         (fila_id,)).fetchone()
    return SimpleNamespace(downloads=downloads, render=render, vid=vid,
                           fila_id=fila_id, linha=linha, clips=clips,
                           arquivos=arquivos, transcricao=transcricao)


def _limpar(cenario, conn, **kw):
    return limpeza.limpar_video(
        conn, cenario.linha, downloads_dir=str(cenario.downloads),
        render_dir=str(cenario.render), executar=_ffmpeg(), **kw)


# --- caminho seguro -------------------------------------------------------------

def test_caminho_dentro_do_diretorio_passa(tmp_path):
    (tmp_path / "d").mkdir()
    f = tmp_path / "d" / "a.mp4"
    f.write_text("x")
    assert limpeza.caminho_seguro(str(f), str(tmp_path / "d"))[0]


def test_caminho_fora_do_diretorio_e_recusado(tmp_path):
    (tmp_path / "d").mkdir()
    f = tmp_path / "fora.mp4"
    f.write_text("x")
    ok, motivo = limpeza.caminho_seguro(str(f), str(tmp_path / "d"))
    assert not ok and "fora do diretório" in motivo


def test_link_simbolico_e_recusado_mesmo_apontando_para_dentro(tmp_path):
    (tmp_path / "d").mkdir()
    alvo = tmp_path / "d" / "real.mp4"
    alvo.write_text("x")
    link = tmp_path / "d" / "link.mp4"
    link.symlink_to(alvo)
    ok, motivo = limpeza.caminho_seguro(str(link), str(tmp_path / "d"))
    assert not ok and "link" in motivo


def test_link_que_escapa_do_diretorio_e_recusado(tmp_path):
    (tmp_path / "d").mkdir()
    segredo = tmp_path / "segredo.mp4"
    segredo.write_text("x")
    link = tmp_path / "d" / "v.mp4"
    link.symlink_to(segredo)
    assert not limpeza.caminho_seguro(str(link), str(tmp_path / "d"))[0]


def test_pasta_com_link_no_meio_do_caminho_e_recusada(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.mp4").write_text("x")
    (tmp_path / "d" / "sub").symlink_to(tmp_path / "real")
    assert not limpeza.caminho_seguro(
        str(tmp_path / "d" / "sub" / "a.mp4"), str(tmp_path / "d"))[0]


def test_extensao_fora_da_lista_e_recusada(tmp_path):
    (tmp_path / "d").mkdir()
    f = tmp_path / "d" / "a.json"
    f.write_text("{}")
    ok, _ = limpeza.caminho_seguro(str(f), str(tmp_path / "d"),
                                   limpeza.EXTENSOES_INTERMEDIARIAS)
    assert not ok


def test_a_propria_raiz_nao_e_apagavel(tmp_path):
    assert not limpeza.caminho_seguro(str(tmp_path), str(tmp_path))[0]


# --- validação do clip ----------------------------------------------------------

def test_clip_valido(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"x")
    assert limpeza.validar_clip(str(f), executar=_ffmpeg()) == []


@pytest.mark.parametrize("texto, trecho", [
    (SAIDA_OK.replace("1080x1920", "1920x1080"), "resolução"),
    (SAIDA_OK.replace("00:00:30.04", "00:00:00.00"), "duração zero"),
    (SAIDA_OK.replace("Audio:", "Data:"), "sem trilha de áudio"),
])
def test_clip_invalido(tmp_path, texto, trecho):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"x")
    problemas = limpeza.validar_clip(str(f), executar=_ffmpeg(texto))
    assert any(trecho in p for p in problemas)


def test_clip_vazio_ou_ausente(tmp_path):
    vazio = tmp_path / "v.mp4"
    vazio.write_bytes(b"")
    assert "tamanho zero" in limpeza.validar_clip(str(vazio), executar=_ffmpeg())
    assert limpeza.validar_clip(str(tmp_path / "nao.mp4")) == ["arquivo não existe"]


# --- limpeza --------------------------------------------------------------------

def test_simular_lista_e_nao_apaga_nada(cenario, conn):
    r = _limpar(cenario, conn, simular=True)
    assert r["estado"] == "simulado" and r["bytes"] > 0
    assert all(os.path.exists(p) for p in cenario.arquivos.values())
    midia = repositorio.midia(conn, cenario.fila_id)
    assert midia["fonte_apagada_em"] is None


def test_real_apaga_so_fonte_e_intermediarios(cenario, conn):
    r = _limpar(cenario, conn, simular=False)
    assert r["estado"] == "apagado"
    vid = cenario.vid
    # apagados
    for nome in (f"{vid}.mp4", f"{vid}.wav", f"{vid}.mp3",
                 f"{vid}.parte00.mp3", f"{vid}.mp4.part"):
        assert not os.path.exists(cenario.arquivos[nome]), nome
    for clip in cenario.clips:
        assert not clip["ass"].exists()
    # preservados
    assert os.path.exists(cenario.arquivos["outroVideo.mp4"])
    assert os.path.exists(cenario.arquivos[f"{vid}XX.mp4"])
    assert cenario.transcricao.exists()
    for clip in cenario.clips:
        assert clip["mp4"].exists() and clip["png"].exists()
    assert os.path.exists(settings.DB_PATH)


def test_registra_no_banco_o_que_foi_liberado(cenario, conn):
    r = _limpar(cenario, conn, simular=False)
    midia = repositorio.midia(conn, cenario.fila_id)
    assert midia["fonte_apagada_em"]
    assert midia["fonte_bytes_liberados"] == r["bytes"] > 0
    # idempotente: já apagado não volta à lista
    assert limpeza.candidatos(conn) == []


def test_nao_apaga_se_falta_render_de_um_clip(cenario, conn):
    conn.execute("DELETE FROM renders WHERE clip_id = ?",
                 (cenario.clips[1]["id"],))
    r = _limpar(cenario, conn, simular=False)
    assert r["estado"] == "nao_pronto"
    assert os.path.exists(cenario.arquivos[f"{cenario.vid}.mp4"])


def test_nao_apaga_se_um_render_e_invalido(cenario, conn):
    ruim = SAIDA_OK.replace("1080x1920", "640x360")
    r = limpeza.limpar_video(
        conn, cenario.linha, simular=False, downloads_dir=str(cenario.downloads),
        render_dir=str(cenario.render), executar=_ffmpeg(ruim))
    assert r["estado"] == "nao_pronto"
    assert os.path.exists(cenario.arquivos[f"{cenario.vid}.mp4"])


def test_caminho_do_banco_fora_do_diretorio_e_recusado(cenario, conn, tmp_path):
    fora = tmp_path / "meus_documentos"
    fora.mkdir()
    precioso = fora / "tese.mp4"
    precioso.write_bytes(b"nao apague")
    repositorio.registrar_midia(conn, cenario.fila_id,
                                video_path=str(precioso))
    r = _limpar(cenario, conn, simular=False)
    assert precioso.exists()
    assert any(str(precioso) == c for c, _m in r["recusados"])
    assert r["estado"] == "apagado"        # o resto do que era seguro


def test_render_registrado_nunca_entra_no_plano(cenario, conn):
    # banco apontando midia.video_path para um render: protegido
    repositorio.registrar_midia(conn, cenario.fila_id,
                                video_path=str(cenario.clips[0]["mp4"]))
    plano = limpeza.planejar(conn, cenario.fila_id, cenario.vid,
                             str(cenario.downloads), str(cenario.render))
    assert str(cenario.clips[0]["mp4"].resolve()) not in [
        c for c, _b in plano["arquivos"]]


def test_link_simbolico_na_pasta_de_download_e_preservado(cenario, conn):
    alvo = cenario.downloads / "outroVideo.mp4"
    link = cenario.downloads / f"{cenario.vid}.extra.mp4"
    link.symlink_to(alvo)
    r = _limpar(cenario, conn, simular=False)
    assert alvo.exists()
    assert os.path.islink(link)
    assert any("link" in m for _c, m in r["recusados"])


def test_limpar_exige_a_flag_para_apagar(cenario, conn):
    with pytest.raises(limpeza.ErroLimpeza):
        limpeza.limpar(conn, simular=False, ativo=False)


def test_flag_desligada_so_simula(cenario, conn, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOADS_DIR", str(cenario.downloads))
    monkeypatch.setattr(settings, "RENDER_DIR", str(cenario.render))
    monkeypatch.setattr(limpeza.subprocess, "run", _ffmpeg())
    resultado = limpeza.limpar(conn, ativo=False)
    assert resultado[cenario.vid]["estado"] == "simulado"
    assert os.path.exists(cenario.arquivos[f"{cenario.vid}.mp4"])


def test_flag_padrao_e_desligada():
    assert settings.LIMPAR_FONTE_APOS_CLIPS is False


# --- guarda de espaço -----------------------------------------------------------

def test_espaco_insuficiente_pausa(monkeypatch, tmp_path):
    monkeypatch.setattr(limpeza.shutil, "disk_usage",
                        lambda p: SimpleNamespace(free=5e9))
    assert limpeza.espaco_insuficiente(str(tmp_path), minimo_gb=10)
    assert not limpeza.espaco_insuficiente(str(tmp_path), minimo_gb=2)
    assert not limpeza.espaco_insuficiente(str(tmp_path), minimo_gb=0)


def test_pipeline_pausa_download_novo_sem_espaco(conn, video, monkeypatch):
    fila = repositorio.registrar_observacao(
        conn, video(video_id="novo1"), views=1, ganho=1, score=5.0,
        status=repositorio.STATUS_DESCOBERTO)
    monkeypatch.setattr(limpeza, "espaco_insuficiente", lambda *a, **k: True)
    baixou = []
    contagem = processar.processar_fila(
        conn, baixar=lambda v: baixou.append(v), transcrever=None,
        detectar=None, picos_do_audio=None)
    assert baixou == []
    assert contagem == {processar.STATUS_PAUSADO_ESPACO: 1}
    # continua na fila, não virou falha
    linha = conn.execute("SELECT status FROM fila_clips WHERE id = ?",
                         (fila,)).fetchone()
    assert linha["status"] == repositorio.STATUS_DESCOBERTO


def test_fonte_apagada_conta_como_nao_baixada(conn, video):
    fila = repositorio.registrar_observacao(
        conn, video(video_id="velho1"), views=1, ganho=1, score=5.0,
        status=repositorio.STATUS_ANALISADO)
    repositorio.registrar_midia(conn, fila, video_path="/x/velho1.mp4",
                                audio_path="/x/velho1.wav")
    linha = conn.execute("SELECT * FROM fila_clips WHERE id = ?",
                         (fila,)).fetchone()
    assert processar._precisa_baixar(conn, linha) is False
    conn.execute("UPDATE midia SET fonte_apagada_em = datetime('now') "
                 "WHERE fila_clip_id = ?", (fila,))
    assert processar._precisa_baixar(conn, linha) is True


def test_raiz_sob_link_do_so_nao_e_confundida_com_link_no_arquivo(tmp_path):
    # /var -> /private/var no macOS: a raiz vive sob um link, o arquivo não.
    real = tmp_path / "real"
    (real / "d").mkdir(parents=True)
    (tmp_path / "atalho").symlink_to(real)
    f = real / "d" / "a.mp4"
    f.write_text("x")
    assert limpeza.caminho_seguro(str(tmp_path / "atalho" / "d" / "a.mp4"),
                                  str(tmp_path / "atalho" / "d"))[0]
