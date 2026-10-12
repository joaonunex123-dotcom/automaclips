"""Trava de processo único para o orquestrador.

Dois laços ao mesmo tempo sobre o mesmo clips.db significam o mesmo vídeo
baixado duas vezes, a mesma quota gasta duas vezes e dois uploads do mesmo
clip. systemd/launchd reiniciam sozinhos e um timer pode disparar enquanto o
ciclo anterior ainda roda — a trava é o que torna isso inofensivo.

Em POSIX usa `flock`: o SO solta a trava quando o processo morre, de qualquer
jeito (kill -9, queda de energia), então não existe trava órfã para limpar à
mão. Sem `fcntl` (Windows) cai num arquivo-pid, com detecção de processo morto.
"""
import logging
import os

log = logging.getLogger(__name__)


class OutraInstancia(Exception):
    """Já existe um orquestrador rodando com esta trava."""


def _pid_vivo(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class Trava:
    """Context manager: `with Trava(caminho): ...` ou levanta OutraInstancia."""

    def __init__(self, caminho):
        self.caminho = caminho
        self._fd = None
        self._por_pid = False

    def __enter__(self):
        self.adquirir()
        return self

    def __exit__(self, *_):
        self.liberar()

    def adquirir(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.caminho)), exist_ok=True)
        try:
            import fcntl
        except ImportError:
            return self._adquirir_por_pid()
        fd = os.open(self.caminho, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise OutraInstancia(
                f"outro orquestrador está rodando (trava {self.caminho})")
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return self

    def _adquirir_por_pid(self):
        for _ in range(2):
            try:
                fd = os.open(self.caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                try:
                    with open(self.caminho, encoding="utf-8") as f:
                        pid = int((f.read() or "0").strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and _pid_vivo(pid):
                    raise OutraInstancia(
                        f"outro orquestrador (pid {pid}) está rodando")
                log.warning("Trava órfã de pid %s removida.", pid)
                try:
                    os.remove(self.caminho)
                except OSError:
                    pass
                continue
            os.write(fd, f"{os.getpid()}\n".encode())
            os.close(fd)
            self._por_pid = True
            return self
        raise OutraInstancia("não consegui obter a trava")

    def liberar(self):
        if self._fd is not None:
            try:
                os.close(self._fd)        # fecha => flock solto
            finally:
                self._fd = None
        if self._por_pid:
            try:
                os.remove(self.caminho)
            except OSError:
                pass
            self._por_pid = False
