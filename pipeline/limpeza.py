"""Limpeza do vídeo-fonte baixado, depois que os clips dele estão prontos.

    python -m pipeline.limpeza --simular      # só lista o que seria apagado
    python -m pipeline.limpeza                # apaga (exige LIMPAR_FONTE_APOS_CLIPS=true)

Um podcast de duas horas em 1080p pesa gigabytes; os clips renderizados dele,
dezenas de megabytes. Num mini PC 24/7 o disco acaba antes da fila. Este módulo
apaga o vídeo-fonte e os intermediários do PIPELINE (áudio, fatias de áudio,
temporários do yt-dlp, .ass) de um vídeo — e só dele.

Regras, todas verificadas no código e em teste:

* **Só depois de tudo pronto.** O vídeo precisa ter pelo menos um clip
  selecionado e TODOS eles com render registrado em `renders` e validado no
  arquivo (existe, tamanho > 0, 1080x1920, duração > 0, trilha de áudio).
* **Só o que é do pipeline, só onde o pipeline escreve.** Cada candidato passa
  por `caminho_seguro`: precisa ser arquivo comum, não ser link simbólico, e
  seu `realpath` precisa estar dentro do diretório de download (ou, para os
  `.ass`, do diretório de render, com extensão `.ass`). Qualquer outra coisa é
  RECUSADA e logada — nunca apagada.
* **Nunca**: clips renderizados, capas, o clips.db, transcrições (são o
  metadado que o agendamento usa), nem arquivo que o banco aponte como render
  ou capa.
* **Desligado por padrão** (`LIMPAR_FONTE_APOS_CLIPS=false`).
* O que foi liberado fica no banco: `midia.fonte_apagada_em` e
  `midia.fonte_bytes_liberados`.

Guarda de espaço: com menos de `LIMPEZA_ESPACO_MIN_GB` livres, o pipeline
pausa os downloads novos (ver pipeline/processar.py) e registra no log.
"""
import argparse
import logging
import os
import re
import shutil
import subprocess
import sys

import settings
from db import repositorio

log = logging.getLogger(__name__)

# Extensões que o pipeline produz a partir de um vídeo-fonte. Nada de .json
# (transcrição/metadado) nem .png (capa).
EXTENSOES_INTERMEDIARIAS = (
    ".mp4", ".mkv", ".webm", ".m4a", ".wav", ".mp3", ".part", ".ytdl", ".temp",
    ".tmp",
)

_STREAM_VIDEO = re.compile(r"Stream #.*Video:.*?[ ,](\d{2,5})x(\d{2,5})")
_DURACAO = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class ErroLimpeza(Exception):
    """Configuração impossível de executar com segurança."""


# --- segurança de caminho -------------------------------------------------------

def _dentro(caminho_real, raiz_real):
    return caminho_real == raiz_real or caminho_real.startswith(raiz_real + os.sep)


def caminho_seguro(caminho, raiz, extensoes=None):
    """(ok, motivo). Só `True` para arquivo comum, não-link, dentro de `raiz`."""
    if not caminho or not raiz:
        return False, "caminho ou diretório vazio"
    try:
        if os.path.islink(caminho):
            return False, "é link simbólico"
        raiz_real = os.path.realpath(raiz)
        real = os.path.realpath(caminho)
    except OSError as e:
        return False, f"não resolvi o caminho: {e}"
    if not _dentro(real, raiz_real) or real == raiz_real:
        return False, f"fora do diretório permitido ({raiz_real})"
    # Um componente do caminho que seja link e leve para fora já foi pego pelo
    # realpath; este confere que o caminho dado é o real (sem desvios).
    if os.path.abspath(caminho) != real:
        return False, "caminho passa por link simbólico"
    if not os.path.isfile(real):
        return False, "não é arquivo comum"
    if extensoes and os.path.splitext(real)[1].lower() not in extensoes:
        return False, f"extensão fora da lista {extensoes}"
    return True, ""


# --- validação de clip -----------------------------------------------------------

def _ler_metadados(caminho, ffmpeg, executar):
    r = executar([ffmpeg, "-hide_banner", "-i", caminho],
                 capture_output=True, text=True)
    texto = r.stderr or ""
    video = _STREAM_VIDEO.search(texto)
    dur = _DURACAO.search(texto)
    segundos = 0.0
    if dur:
        h, m, s = dur.groups()
        segundos = int(h) * 3600 + int(m) * 60 + float(s)
    return {
        "largura": int(video.group(1)) if video else 0,
        "altura": int(video.group(2)) if video else 0,
        "duracao_s": segundos,
        "audio": " Audio:" in texto,
    }


def validar_clip(caminho, largura=1080, altura=1920, ffmpeg="ffmpeg",
                 executar=None):
    """Lista de problemas do arquivo renderizado ([] = válido)."""
    executar = executar or subprocess.run
    if not caminho or not os.path.isfile(caminho):
        return ["arquivo não existe"]
    problemas = []
    if os.path.getsize(caminho) <= 0:
        problemas.append("tamanho zero")
    try:
        meta = _ler_metadados(caminho, ffmpeg, executar)
    except OSError as e:
        return problemas + [f"ffmpeg indisponível: {e}"]
    if (meta["largura"], meta["altura"]) != (largura, altura):
        problemas.append(
            f"resolução {meta['largura']}x{meta['altura']} "
            f"(esperado {largura}x{altura})")
    if meta["duracao_s"] <= 0:
        problemas.append("duração zero")
    if not meta["audio"]:
        problemas.append("sem trilha de áudio")
    return problemas


def clips_do_video(conn, fila_clip_id):
    return repositorio.clips_do_video(conn, fila_clip_id,
                                      repositorio.CLIP_SELECIONADO)


def podcast_pronto(conn, fila_clip_id, largura=1080, altura=1920,
                   ffmpeg="ffmpeg", executar=None):
    """(ok, motivos). Todos os clips selecionados renderizados e válidos."""
    clips = clips_do_video(conn, fila_clip_id)
    if not clips:
        return False, ["nenhum clip selecionado"]
    motivos = []
    for clip in clips:
        render = repositorio.render(conn, clip["id"])
        if render is None:
            motivos.append(f"clip {clip['id']}: sem render registrado")
            continue
        problemas = validar_clip(render["caminho"], largura, altura, ffmpeg,
                                 executar)
        if problemas:
            motivos.append(f"clip {clip['id']}: " + "; ".join(problemas))
    return (not motivos), motivos


# --- plano de limpeza -------------------------------------------------------------

def _protegidos(conn, fila_clip_id):
    """Caminhos reais que NUNCA podem entrar no plano: renders e capas."""
    protegidos = set()
    for clip in repositorio.clips_do_video(conn, fila_clip_id):
        render = repositorio.render(conn, clip["id"])
        if render is None:
            continue
        for chave in ("caminho", "capa_path"):
            valor = render[chave] if chave in render.keys() else ""
            if valor:
                protegidos.add(os.path.realpath(valor))
    return protegidos


def planejar(conn, fila_clip_id, video_id, downloads_dir=None, render_dir=None):
    """{'arquivos': [(caminho, bytes)], 'recusados': [(caminho, motivo)], 'bytes'}.

    Candidatos: arquivos de intermediário em `downloads_dir` cujo nome começa
    por `<video_id>.` (nível raiz; nada recursivo) e os caminhos que `midia`
    registra; e os .ass de cada clip em `render_dir`.
    """
    downloads_dir = downloads_dir or settings.DOWNLOADS_DIR
    render_dir = render_dir or settings.RENDER_DIR
    protegidos = _protegidos(conn, fila_clip_id)
    midia = repositorio.midia(conn, fila_clip_id)

    candidatos = []     # (caminho, raiz, extensoes)
    if os.path.isdir(downloads_dir) and video_id:
        prefixo = f"{video_id}."
        for nome in sorted(os.listdir(downloads_dir)):
            if nome.startswith(prefixo):
                candidatos.append((os.path.join(downloads_dir, nome),
                                   downloads_dir, EXTENSOES_INTERMEDIARIAS))
    if midia is not None:
        for chave in ("video_path", "audio_path"):
            if midia[chave]:
                candidatos.append((midia[chave], downloads_dir,
                                   EXTENSOES_INTERMEDIARIAS))

    if render_dir and os.path.isdir(render_dir):
        from editing import render as render_mod
        for clip in repositorio.clips_do_video(conn, fila_clip_id):
            nome = render_mod.nome_base(video_id, clip["id"])
            ass = os.path.join(render_dir, f"{nome}.ass")
            if os.path.lexists(ass):
                candidatos.append((ass, render_dir, (".ass",)))

    vistos, arquivos, recusados = set(), [], []
    for caminho, raiz, extensoes in candidatos:
        chave = os.path.abspath(caminho)
        if chave in vistos or not os.path.lexists(caminho):
            continue
        vistos.add(chave)
        ok, motivo = caminho_seguro(caminho, raiz, extensoes)
        if ok and os.path.realpath(caminho) in protegidos:
            ok, motivo = False, "é um render/capa registrado no banco"
        if not ok:
            recusados.append((caminho, motivo))
            continue
        arquivos.append((os.path.realpath(caminho), os.path.getsize(caminho)))
    return {"arquivos": arquivos, "recusados": recusados,
            "bytes": sum(b for _c, b in arquivos)}


def _registrar(conn, fila_clip_id, bytes_liberados):
    with repositorio.escrita(conn):
        conn.execute(
            "UPDATE midia SET fonte_apagada_em = datetime('now','localtime'),"
            "   fonte_bytes_liberados = ? WHERE fila_clip_id = ?",
            (int(bytes_liberados), fila_clip_id),
        )


def limpar_video(conn, linha, simular=True, downloads_dir=None, render_dir=None,
                 ffmpeg="ffmpeg", executar=None, largura=1080, altura=1920):
    """Resultado da limpeza de UM vídeo: {'estado', 'bytes', 'arquivos', ...}.

    estado: 'apagado' | 'simulado' | 'nao_pronto' | 'nada'.
    """
    fila_id, video_id = linha["id"], linha["video_id"]
    pronto, motivos = podcast_pronto(conn, fila_id, largura, altura, ffmpeg,
                                     executar)
    if not pronto:
        log.info("Limpeza de %s adiada: %s", video_id, "; ".join(motivos))
        return {"estado": "nao_pronto", "motivos": motivos, "bytes": 0,
                "arquivos": []}

    plano = planejar(conn, fila_id, video_id, downloads_dir, render_dir)
    for caminho, motivo in plano["recusados"]:
        log.warning("Limpeza RECUSOU %s: %s", caminho, motivo)
    if not plano["arquivos"]:
        return {"estado": "nada", "bytes": 0, "arquivos": [],
                "recusados": plano["recusados"]}

    if simular:
        for caminho, tamanho in plano["arquivos"]:
            log.info("[simular] apagaria %s (%.1f MB)", caminho, tamanho / 1e6)
        return {"estado": "simulado", "bytes": plano["bytes"],
                "arquivos": [c for c, _b in plano["arquivos"]],
                "recusados": plano["recusados"]}

    apagados, liberados = [], 0
    for caminho, tamanho in plano["arquivos"]:
        # Revalida na hora de apagar: entre planejar e apagar o arquivo
        # poderia ter sido trocado por um link.
        if caminho.endswith(".ass"):
            raiz = render_dir or settings.RENDER_DIR
        else:
            raiz = downloads_dir or settings.DOWNLOADS_DIR
        ok, motivo = caminho_seguro(caminho, raiz)
        if not ok:
            log.warning("Limpeza RECUSOU %s na hora de apagar: %s", caminho, motivo)
            continue
        try:
            os.remove(caminho)
        except OSError as e:
            log.warning("Não apaguei %s: %s", caminho, e)
            continue
        apagados.append(caminho)
        liberados += tamanho
    if apagados:
        _registrar(conn, fila_id, liberados)
        log.info("Fonte de %s apagada: %d arquivos, %.1f MB liberados.",
                 video_id, len(apagados), liberados / 1e6)
    return {"estado": "apagado" if apagados else "nada", "bytes": liberados,
            "arquivos": apagados, "recusados": plano["recusados"]}


def candidatos(conn):
    """Vídeos com fonte baixada e ainda não apagada."""
    return conn.execute(
        "SELECT f.* FROM fila_clips f JOIN midia m ON m.fila_clip_id = f.id"
        " WHERE m.video_path != '' AND m.fonte_apagada_em IS NULL"
        " ORDER BY f.id"
    ).fetchall()


def limpar(conn, simular=None, ativo=None, **kwargs):
    """Limpa todos os vídeos prontos. Devolve {'video_id': resultado}.

    `ativo` (padrão: settings.LIMPAR_FONTE_APOS_CLIPS) é o interruptor geral;
    desligado, nada é apagado — só `simular=True` roda (lista, sem tocar).
    """
    ativo = settings.LIMPAR_FONTE_APOS_CLIPS if ativo is None else ativo
    if simular is None:
        simular = not ativo
    if not simular and not ativo:
        raise ErroLimpeza(
            "LIMPAR_FONTE_APOS_CLIPS=false: a limpeza real está desligada. "
            "Use --simular para ver o que seria apagado.")
    resultado = {}
    for linha in candidatos(conn):
        resultado[linha["video_id"]] = limpar_video(
            conn, linha, simular=simular, **kwargs)
    return resultado


# --- guarda de espaço --------------------------------------------------------------

def espaco_livre_gb(diretorio=None):
    alvo = diretorio or settings.DOWNLOADS_DIR
    while alvo and not os.path.exists(alvo):
        pai = os.path.dirname(alvo)
        if pai == alvo:
            break
        alvo = pai
    return shutil.disk_usage(alvo or ".").free / 1e9


def espaco_insuficiente(diretorio=None, minimo_gb=None):
    """True quando os downloads novos devem pausar."""
    minimo = settings.LIMPEZA_ESPACO_MIN_GB if minimo_gb is None else minimo_gb
    if not minimo:
        return False
    return espaco_livre_gb(diretorio) < float(minimo)


# --- CLI ------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Apaga o vídeo-fonte e os intermediários de podcasts cujos "
                    "clips já estão prontos.")
    parser.add_argument("--simular", action="store_true",
                        help="só lista o que seria apagado")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    conn = repositorio.conectar()
    try:
        try:
            resultado = limpar(conn, simular=True if args.simular else None)
        except ErroLimpeza as e:
            log.error("%s", e)
            return 2
        total = sum(r["bytes"] for r in resultado.values())
        for video_id, r in resultado.items():
            print(f"  {video_id:<14} {r['estado']:<10} "
                  f"{r['bytes'] / 1e6:9.1f} MB  {len(r['arquivos'])} arquivos")
        print(f"\nlivre agora: {espaco_livre_gb():.1f} GB; "
              f"{'seria liberado' if args.simular else 'liberado'}: "
              f"{total / 1e6:.1f} MB")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
