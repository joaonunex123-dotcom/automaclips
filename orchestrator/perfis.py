"""Roda o pipeline de cada canal, um perfil por vez.

    python -m orchestrator.perfis --listar
    python -m orchestrator.perfis --verificar
    python -m orchestrator.perfis --uma-vez
    python -m orchestrator.perfis --uma-vez --perfil esportes

Um perfil é um CANAL DE DESTINO: fontes próprias, banco próprio, mídia
própria e contas próprias nas três plataformas. Quem define um perfil é o
arquivo `.env.<nome>` na raiz; os dados dele vivem em `perfis/<nome>/`.

**Cada perfil roda num processo próprio**, com `CLIPS_PERFIL` no ambiente, e
essa é a decisão central do módulo. O `settings` resolve os caminhos uma vez,
no import, e congela — então trocar de perfil dentro do mesmo processo pediria
recarregar o módulo e todos os que já leram valores dele. O que sobraria seria
um sistema em que um esquecimento faz o clip de um canal ser publicado na
conta do outro, sem desfazer. Um processo por perfil torna isso impossível
por construção, e custa o preço de um `python` a mais por ciclo.

**Falha de um perfil não interrompe os outros**, pela mesma razão que uma
etapa que falha não derruba o laço: um canal com o token vencido não pode
impedir que os outros publiquem. O resumo do fim diz quem passou e quem não.

Sem nenhum `.env.<nome>` na raiz, roda a instalação única de sempre — quem
tem um canal só nunca precisa saber que perfis existem.
"""
import argparse
import logging
import os
import subprocess
import sys

import settings

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PREFIXO = ".env."

# Arquivos que casam com o prefixo mas não são perfil: o exemplo versionado e
# a convenção de override local do dotenv.
NAO_SAO_PERFIL = frozenset({"example", "exemplo", "local", "sample"})

# O perfil "de sempre": nenhum CLIPS_PERFIL no ambiente, dados na raiz.
PADRAO = ""


def listar(base_dir=None):
    """Os perfis configurados, em ordem, pelo `.env.<nome>` de cada um.

    O arquivo de configuração é que define o perfil, e não a pasta de dados:
    a pasta nasce sozinha na primeira execução, e uma pasta órfã (de um canal
    abandonado) não deve ressuscitar o canal no ciclo seguinte.
    """
    base_dir = base_dir or _BASE_DIR
    try:
        arquivos = os.listdir(base_dir)
    except OSError as e:  # pragma: no cover - raiz ilegível
        log.warning("Não deu para listar %s: %s", base_dir, e)
        return []

    perfis = []
    for arquivo in arquivos:
        if not arquivo.startswith(PREFIXO):
            continue
        nome = arquivo[len(PREFIXO):]
        if nome.lower() in NAO_SAO_PERFIL:
            continue
        try:
            perfis.append(settings.nome_de_perfil(nome))
        except ValueError as e:
            # Um .env.Backup~ na raiz é engano de quem copiou arquivo, não um
            # canal. Avisar é melhor que rodar um ciclo inteiro para ele.
            log.warning("Ignorando %s: %s", arquivo, e)
    return sorted(set(perfis))


def montar_comando(perfil, uma_vez=True, verificar=False, verbose=False,
                   python=None):
    """A linha de comando do main_loop para este perfil."""
    comando = [python or sys.executable, "-m", "orchestrator.main_loop"]
    if verificar:
        comando.append("--verificar")
    elif uma_vez:
        comando.append("--uma-vez")
    if verbose:
        comando.append("--verbose")
    return comando


def ambiente(perfil, base=None):
    """O ambiente do subprocesso: o nosso, com o CLIPS_PERFIL daquele canal.

    Herdar o ambiente é o que mantém as chaves exportadas no shell valendo
    para todos os perfis. O perfil PADRÃO tira a variável em vez de mandar
    vazio — assim um `CLIPS_PERFIL` herdado do terminal não vaza para dentro
    da execução que deveria ser a da instalação única.
    """
    env = dict(os.environ if base is None else base)
    if perfil:
        env["CLIPS_PERFIL"] = perfil
    else:
        env.pop("CLIPS_PERFIL", None)
    return env


def rodar(perfil, executar=None, base_dir=None, **kwargs):
    """Roda um perfil. Devolve o código de saída (0 = ok).

    Exceção do subprocesso vira código 1 e log, e não propagação: a lista de
    perfis precisa continuar.
    """
    executar = executar or subprocess.run
    comando = montar_comando(perfil, **kwargs)
    log.info("--- perfil %s ---", perfil or "(único)")
    try:
        resultado = executar(comando, env=ambiente(perfil),
                             cwd=base_dir or _BASE_DIR)
    except Exception as e:
        log.exception("Perfil %s não chegou a rodar: %s", perfil or "(único)", e)
        return 1
    codigo = int(getattr(resultado, "returncode", 0) or 0)
    if codigo:
        log.warning("Perfil %s terminou com código %d.",
                    perfil or "(único)", codigo)
    return codigo


def rodar_todos(perfis, executar=None, base_dir=None, **kwargs):
    """Roda a lista inteira, em ordem. Devolve {perfil: código}."""
    return {
        perfil: rodar(perfil, executar=executar, base_dir=base_dir, **kwargs)
        for perfil in perfis
    }


def _resumo(codigos):
    linhas = ["", "--- perfis ---"]
    for perfil in sorted(codigos):
        estado = "ok" if codigos[perfil] == 0 else f"código {codigos[perfil]}"
        linhas.append(f"  {perfil or '(único)':<20} {estado}")
    return "\n".join(linhas)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Roda o pipeline de cada canal, um perfil por vez."
    )
    parser.add_argument("--listar", action="store_true",
                        help="mostra os perfis configurados e sai")
    parser.add_argument("--verificar", action="store_true",
                        help="roda o --verificar do main_loop em cada perfil")
    parser.add_argument("--uma-vez", action="store_true",
                        help="um ciclo completo por perfil (padrão)")
    parser.add_argument("--perfil", action="append", default=None,
                        metavar="NOME",
                        help="roda só este perfil (pode repetir)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.perfil:
        try:
            perfis = [settings.nome_de_perfil(p) for p in args.perfil]
        except ValueError as e:
            log.error("%s", e)
            return 2
    else:
        perfis = listar()
        if not perfis:
            # Instalação de um canal só: roda como sempre rodou, e diz por quê
            # — para ninguém concluir que o comando não fez nada.
            log.info(
                "Nenhum .env.<nome> na raiz: rodando a instalação única. "
                "Para separar canais, crie um .env.<nome> por canal."
            )
            perfis = [PADRAO]

    if args.listar:
        for perfil in perfis:
            print(perfil or "(único)")
        return 0

    codigos = rodar_todos(
        perfis, uma_vez=args.uma_vez or not args.verificar,
        verificar=args.verificar, verbose=args.verbose,
    )
    print(_resumo(codigos))
    # Com --verificar o código importa: é o que diz se dá para ligar a
    # publicação. Num ciclo normal, não — falha de etapa é esperada (canal
    # fora do ar, API instável), e código diferente de zero sob agendador
    # viraria alerta a cada hora. Mesma escolha do main_loop --uma-vez.
    if args.verificar and any(codigos.values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
