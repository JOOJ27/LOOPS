"""
Ponto de entrada do executável do LOOPS.

- Se já estiver num terminal (TTY): roda a aplicação direto.
- Se foi aberto com duplo clique (sem terminal): abre um terminal
  (Terminal.app no Mac, gnome-terminal/konsole/xterm... no Linux,
  nova janela de console no Windows) e roda a si mesmo lá dentro.
"""
import os
import sys
import shlex
import shutil
import platform
import subprocess

ENV_FLAG = "LOOPS_IN_TERMINAL"


def _self_command():
    """Comando que reexecuta este mesmo programa."""
    if getattr(sys, "frozen", False):          # rodando como binário PyInstaller
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def _has_tty():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------- terminais
_LINUX_TERMINALS = [
    # (executável, flag que antecede o comando)
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("kgx", ["-e"]),                 # GNOME Console
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("mate-terminal", ["-x"]),
    ("tilix", ["-e"]),
    ("terminator", ["-x"]),
    ("lxterminal", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("wezterm", ["start", "--"]),
    ("xterm", ["-e"]),
]


def _relaunch_linux(cmd):
    env = dict(os.environ, **{ENV_FLAG: "1"})
    for exe, flag in _LINUX_TERMINALS:
        path = shutil.which(exe)
        if path:
            subprocess.Popen([path, *flag, *cmd], env=env, start_new_session=True)
            return True
    return False


def _relaunch_macos(cmd):
    line = "export %s=1; exec %s" % (ENV_FLAG, " ".join(shlex.quote(c) for c in cmd))
    script = (
        'tell application "Terminal"\n'
        "  activate\n"
        '  do script "%s"\n'
        "end tell"
    ) % line.replace("\\", "\\\\").replace('"', '\\"')
    return subprocess.call(["osascript", "-e", script]) == 0


def _relaunch_windows(cmd):
    env = dict(os.environ, **{ENV_FLAG: "1"})
    subprocess.Popen(cmd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return True


def _open_terminal_and_exit():
    cmd = _self_command()
    system = platform.system()
    if system == "Darwin":
        ok = _relaunch_macos(cmd)
    elif system == "Windows":
        ok = _relaunch_windows(cmd)
    else:
        ok = _relaunch_linux(cmd)

    if not ok:
        msg = ("LOOPS precisa de um terminal e não encontrei nenhum.\n"
               "Abra um terminal e rode o programa por lá.")
        if shutil.which("zenity"):
            subprocess.call(["zenity", "--error", "--text", msg])
        elif shutil.which("notify-send"):
            subprocess.call(["notify-send", "LOOPS", msg])
        print(msg, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


# ------------------------------------------------------------- PortAudio
def _fix_portaudio_linux():
    """
    No Linux o sounddevice não traz o PortAudio dentro da wheel.
    O build embute libportaudio.so.2 no executável; aqui avisamos o
    sounddevice onde ele está (precisa rodar ANTES de importar sounddevice).
    """
    if platform.system() != "Linux" or not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    for name in ("libportaudio.so.2", "libportaudio.so"):
        lib = os.path.join(base, name)
        if os.path.exists(lib):
            import ctypes.util
            real = ctypes.util.find_library

            def patched(n, _real=real, _lib=lib):
                return _lib if n == "portaudio" else _real(n)

            ctypes.util.find_library = patched
            return


# ------------------------------------------------------------------ main
def main():
    # Duplo clique / sem terminal → abre um terminal e reexecuta lá
    if not _has_tty() and not os.environ.get(ENV_FLAG):
        _open_terminal_and_exit()

    _fix_portaudio_linux()

    import curses
    try:
        import Client
        curses.wrapper(Client.main)
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
        # Mantém a janela aberta para dar tempo de ler o erro
        if os.environ.get(ENV_FLAG):
            try:
                input("\nPressione Enter para fechar...")
            except EOFError:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
