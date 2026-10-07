import asyncio
import curses
import json
import os
import sys
import time
import base64
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import sounddevice as sd

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from Camera import Camera
from Textbox import TextBox
from Messagebox import ChatBox

from Config import (
    MIN_H,
    MIN_W,
    LOGS_H,
    INPUT_H,
    SAMPLE_RATE,
    CHANNELS, 
    CHUNK_SIZE,
    SERVER_URI,
    FRAME_INTERVAL,
)

from Animation import GLASSES_FRAMES


# ==================================================
# LOGS FORA DA TELA
# ==================================================
# Bibliotecas em C (ALSA/PortAudio, OpenCV...) escrevem direto no descritor 2
# (stderr), por cima do curses. Redirecionamos esse descritor para um arquivo
# enquanto o app roda; assim nada aparece na tela e os erros ficam guardados
# em ~/.loops/loops.log para consulta.
LOG_PATH = Path.home() / ".loops" / "loops.log"


class StderrToFile:
    def __enter__(self):
        self._saved_fd = None
        self._log = None
        try:
            try:
                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                self._log = open(LOG_PATH, "w", encoding="utf-8", errors="replace")
            except OSError:
                self._log = open(os.devnull, "w")
            sys.stderr.flush()
            self._saved_fd = os.dup(2)
            os.dup2(self._log.fileno(), 2)
        except Exception:
            self._saved_fd = None
        return self

    def __exit__(self, *exc_info):
        try:
            sys.stderr.flush()
        except Exception:
            pass
        if self._saved_fd is not None:
            os.dup2(self._saved_fd, 2)
            os.close(self._saved_fd)
        if self._log is not None:
            self._log.close()
        return False


def short_error(exc):
    """Erro em uma linha só, para caber no Nerd info."""
    return " ".join(f"{type(exc).__name__}: {exc}".split())


# ==================================================
# CONEXÃO COM O SERVIDOR (tenta até conseguir)
# ==================================================
# O plano gratuito do Render "dorme" quando fica parado e leva até ~1 min
# para acordar. Em vez de desistir na primeira falha, o cliente mostra a tela
# de espera com um log das tentativas e repete até o servidor responder.
CONNECT_RETRY_DELAY = 3    # segundos entre as tentativas
CONNECT_TIMEOUT = 20       # segundos que cada tentativa espera pela resposta
CONNECT_LOG_KEEP = 8       # quantas linhas de log ficam guardadas


def connect_log(state, kind, text):
    """kind: 'info' ou 'error'."""
    log = state["connect_log"]
    log.append((kind, text))
    del log[:-CONNECT_LOG_KEEP]


def describe_connect_error(exc):
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "no response yet (server may be waking up)"
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code in (502, 503, 504):
        return f"server not ready (HTTP {code})"
    if code is not None:
        return f"server answered HTTP {code}"
    return short_error(exc)


async def connect_with_retry(state):
    """Só termina quando conectar; devolve o websocket."""
    host = urlparse(SERVER_URI).netloc or SERVER_URI
    attempt = 0

    while True:
        attempt += 1
        connect_log(state, "info", f"Attempt {attempt}: connecting to {host}...")

        try:
            return await connect(SERVER_URI, open_timeout=CONNECT_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            connect_log(
                state,
                "error",
                f"Attempt {attempt} failed: {describe_connect_error(exc)}",
            )

        await asyncio.sleep(CONNECT_RETRY_DELAY)



# --- CONFIGURAÇÕES DE ÁUDIO ---

# Fila assíncrona para não bloquear a thread principal
audio_send_queue = asyncio.Queue()


def make_panel(h, w, y, x, title, pair, draw_box=True):
    """Cria uma janela com borda (opcional) e título."""
    outer = curses.newwin(h, w, y, x)
    
    if draw_box:
        outer.box()
        if title:
            outer.addstr(0, 2, f" {title} ", curses.color_pair(pair))
        inner = outer.derwin(h - 2, w - 2, 1, 1)
    else:
        # Sem borda, a janela interna ocupa todo o espaço (h, w) começando do (0, 0)
        inner = outer.derwin(h, w, 0, 0)
        
    return outer, inner

def create_layout(stdscr):
    H, W = stdscr.getmaxyx()

    if H < MIN_H or W < MIN_W:
        return None

    body_h = H - LOGS_H
    left_w = W // 2
    right_w = W - left_w
    friend_h = body_h // 2
    me_h = body_h - friend_h

    return {
        "logs": make_panel(
            LOGS_H,
            W,
            0,
            0,
            "Nerd info",
            2,
        ),
        "friend": make_panel(
            friend_h,
            left_w,
            LOGS_H,
            0,
            "Friend",
            3,
            False,
        ),
        "me": make_panel(
            me_h,
            left_w,
            LOGS_H + friend_h,
            0,
            "You",
            2,
            False,
        ),
        "chat": make_panel(
            body_h - INPUT_H,
            right_w,
            LOGS_H,
            left_w,
            "Chat",
            1,
        ),
        "input": make_panel(
            INPUT_H,
            right_w,
            H - INPUT_H,
            left_w,
            "Message",
            3,
        ),
    }


def create_waiting_layout(stdscr):
    """Cria o layout usado enquanto o cliente espera por um par."""
    H, W = stdscr.getmaxyx()

    if H < 5 or W < 20:
        return None

    main_h = H - LOGS_H

    return {
        "logs": make_panel(
            LOGS_H,
            W,
            0,
            0,
            "Nerd info",
            2,
            True
        ),
        "main": make_panel(
            main_h,
            W,
            LOGS_H,
            0,
            "Main",
            3,
            False
        ),
    }


def queue_layout(layout):
    """Enfileira todas as janelas do layout para repintura.

    touchwin() é o que garante que a BORDA seja repintada. A borda vive na
    janela externa e só é escrita uma vez, na criação. noutrefresh() copia
    apenas linhas "sujas", então, sem touchwin(), depois de qualquer coisa que
    sobrescreva a tela (resize, troca espera <-> chamada) a borda nunca mais
    é reenviada. As janelas internas (derwin) compartilham a memória da
    externa, então o conteúdo vai junto.
    """
    for outer, _ in layout.values():
        outer.touchwin()
        outer.noutrefresh()


def refresh_layout(layout):
    """Enfileira e já manda para o terminal."""
    queue_layout(layout)
    curses.doupdate()


def draw_box(win, y, x, h, w, title="", pair=0):
    """Desenha uma borda no retângulo (y, x, h, w) dentro de win."""
    if h < 3 or w < 3:
        return

    def safe(fn, *args):
        # escrever na última célula da janela levanta curses.error,
        # mas o caractere é desenhado mesmo assim
        try:
            fn(*args)
        except curses.error:
            pass

    safe(win.addch, y, x, curses.ACS_ULCORNER)
    safe(win.hline, y, x + 1, curses.ACS_HLINE, w - 2)
    safe(win.addch, y, x + w - 1, curses.ACS_URCORNER)
    safe(win.vline, y + 1, x, curses.ACS_VLINE, h - 2)
    safe(win.vline, y + 1, x + w - 1, curses.ACS_VLINE, h - 2)
    safe(win.addch, y + h - 1, x, curses.ACS_LLCORNER)
    safe(win.hline, y + h - 1, x + 1, curses.ACS_HLINE, w - 2)
    safe(win.addch, y + h - 1, x + w - 1, curses.ACS_LRCORNER)

    if title and w >= len(title) + 6:
        safe(win.addstr, y, x + 2, f" {title} ", curses.color_pair(pair))


def draw_ascii_lines(win, lines, placeholder="", title="", pair=0):
    """Desenha o frame ASCII com a borda ajustada ao tamanho da imagem.

    Sem frame, desenha a borda no painel inteiro com o placeholder no meio.
    """
    if win is None:
        return

    h, w = win.getmaxyx()
    win.erase()

    if h < 3 or w < 3:
        win.noutrefresh()
        return

    # área útil para a imagem, já descontando a borda
    max_w, max_h = w - 2, h - 2
    visible = [line[:max_w] for line in lines[:max_h]] if lines else []

    # Sem imagem: borda no painel inteiro + placeholder no meio
    if not visible:
        draw_box(win, 0, 0, h, w, title, pair)

        if placeholder:
            text = placeholder[:w - 2]
            try:
                win.addnstr(
                    h // 2,
                    max(0, (w - len(text)) // 2),
                    text,
                    w - 2,
                    curses.A_DIM,
                )
            except curses.error:
                pass

        win.noutrefresh()
        return

    image_h = len(visible)
    image_w = max(len(line) for line in visible)

    box_h, box_w = image_h + 2, image_w + 2
    box_y = (h - box_h) // 2
    box_x = (w - box_w) // 2

    draw_box(win, box_y, box_x, box_h, box_w, title, pair)

    for i, line in enumerate(visible):
        try:
            win.addnstr(box_y + 1 + i, box_x + 1, line, image_w)
        except curses.error:
            pass

    win.noutrefresh()


async def send_json(websocket, message_type, **data):
    """Envia uma mensagem pelo WebSocket."""
    await websocket.send(
        json.dumps(
            {
                "type": message_type,
                **data,
            },
            ensure_ascii=False,
        )
    )

# ==================================================
# ÁUDIO CALLBACKS
# ==================================================
def audio_callback(indata, frames, time_info, status):
    if status:
        pass
    audio_int16 = (indata * 32767).astype(np.int16)
    audio_bytes = audio_int16.tobytes()
    audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
    
    loop = asyncio.get_event_loop()
    loop.call_soon_threadsafe(audio_send_queue.put_nowait, audio_b64)


async def audio_send_loop(websocket, state):
    loop = asyncio.get_running_loop()
    audio_send_queue = asyncio.Queue()

    def audio_callback(indata, frames, time_info, status):
        if status:
            pass

        audio_int16 = (indata * 32767).astype(np.int16)
        audio_bytes = audio_int16.tobytes()
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        # O callback roda em outra thread, então usamos
        # call_soon_threadsafe para voltar ao event loop principal.
        loop.call_soon_threadsafe(
            audio_send_queue.put_nowait,
            audio_b64
        )

    try:
        stream_in = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=audio_callback,
            blocksize=CHUNK_SIZE
        )
        stream_in.start()
    except Exception as exc:
        # Driver recusou o microfone: o app segue funcionando sem ele.
        state["audio_in_ok"] = False
        state["audio_in_error"] = short_error(exc)
        return

    state["audio_in_ok"] = True

    try:
        while True:
            audio_b64 = await audio_send_queue.get()

            if state["paired"] and state["mic_active"]:
                await send_json(
                    websocket,
                    "audio",
                    data=audio_b64
                )

    except asyncio.CancelledError:
        pass

    finally:
        try:
            stream_in.stop()
            stream_in.close()
        except Exception:
            pass



async def camera_send_loop(websocket, camera, state):
    """
    Captura a câmera, transforma em ASCII e envia para o servidor.

    Enquanto não houver parceiro, a câmera não é enviada.
    
    """
    try:
        while True:
            # IMPORTANTE:
            # precisamos dar controle ao event loop aqui.
            if not state["paired"]:
                await asyncio.sleep(FRAME_INTERVAL)
                continue

            width, height = state["camera_size"]

            if width > 0 and height > 0:
                frame = None

                try:
                    frame = await asyncio.to_thread(
                        camera.get_ascii,
                        width,
                        height,
                    )
                except Exception as exc:
                    state["camera_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    if frame is None:
                        state["camera_error"] = (
                            "sem imagem da câmera "
                            "(em uso por outro processo?)"
                        )

                if frame is None:
                    state["camera_ok"] = False
                    state["my_frame"] = []
                else:
                    state["camera_ok"] = True
                    state["camera_error"] = None
                    state["my_frame"] = frame

                    await send_json(
                        websocket,
                        "video",
                        frame="\n".join(frame),
                    )

            await asyncio.sleep(FRAME_INTERVAL)

    except asyncio.CancelledError:
        raise

    except ConnectionClosed:
        state["connected"] = False

    except Exception as exc:
        state["connected"] = False
        state["network_error"] = str(exc)


async def receive_loop(websocket, chat, state):
    """Recebe vídeo, áudio, chat, estado do pareamento e estatísticas."""
    stream_out = None
    try:
        stream_out = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16'
        )
        stream_out.start()
        state["audio_out_ok"] = True
    except Exception as exc:
        # Driver recusou a saída de áudio: continua recebendo vídeo e chat.
        stream_out = None
        state["audio_out_ok"] = False
        state["audio_out_error"] = short_error(exc)
        chat.add_message(
            "System",
            f"Sem saída de áudio: {state['audio_out_error']}",
        )

    try:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except (TypeError, json.JSONDecodeError):
                continue

            if not isinstance(message, dict):
                continue

            message_type = message.get("type")

            if message_type == "audio":
                # Toca o áudio do parceiro
                if stream_out is None:
                    continue

                try:
                    raw_bytes = base64.b64decode(message["data"])
                    audio_array = np.frombuffer(raw_bytes, dtype=np.int16)
                except Exception:
                    continue

                try:
                    stream_out.write(audio_array)
                except Exception as exc:
                    # Dispositivo sumiu ou o driver travou no meio da chamada.
                    state["audio_out_ok"] = False
                    state["audio_out_error"] = short_error(exc)
                    try:
                        stream_out.abort()
                        stream_out.close()
                    except Exception:
                        pass
                    stream_out = None
                    chat.add_message(
                        "System",
                        f"Saída de áudio parou: {state['audio_out_error']}",
                    )

            elif message_type == "video":
                raw_frame = message.get("frame", "")
                state["friend_frame"] = raw_frame.split("\n")

            elif message_type == "system":
                system_message = message.get("message", "")

                if system_message == "Partner found!":
                    state["paired"] = True

                elif system_message == "The other person left.":
                    state["paired"] = False
                    state["friend_frame"] = []

                if system_message:
                    chat.add_message(
                        "System",
                        system_message,
                    )

            elif message_type == "server_stats":
                state["people"] = message.get(
                    "people",
                    0,
                )
                state["pairs"] = message.get(
                    "pairs",
                    0,
                )

            elif message_type == "chat":
                text = message.get("text", "")

                if text:
                    chat.add_message(
                        "Friend",
                        text,
                    )

    except asyncio.CancelledError:
        raise

    except ConnectionClosed:
        pass

    except WebSocketException:
        pass

    finally:
        state["connected"] = False
        if stream_out is not None:
            try:
                stream_out.stop()
                stream_out.close()
            except Exception:
                pass


def draw_waiting_screen(layout, state, frame_index):

    # ==================================================
    # JANELAS
    # ==================================================

    logs_outer, logs_win = layout["logs"]
    main_outer, main_win = layout["main"]

    # ==================================================
    # NERD INFO
    # ==================================================

    logs_win.erase()

    people = state["people"]
    pairs = state["pairs"]

    try:
        logs_win.addstr(
            0,
            3,
            f"People online: {people}"
        )

        logs_win.addstr(
            0,
            31,
            f"Active pairs: {pairs}"
        )

        # Bolinha verde
        logs_win.addstr(
            0,
            1,
            "●",
            curses.color_pair(2)
        )
        
        # Bolinha verde
        logs_win.addstr(
            0,
            29,
            "●",
            curses.color_pair(1)
        )

    except curses.error:
        pass

    # ==================================================
    # MAIN
    # ==================================================

    main_win.erase()

    height, width = main_win.getmaxyx()

    frame = GLASSES_FRAMES[frame_index]

    frame_height = len(frame)

    start_y = (
        height // 2
        - frame_height // 2
        - 2
    )

    # ==================================================
    # ÓCULOS
    # ==================================================

    for i, line in enumerate(frame):

        x = max(
            0,
            (width - len(line)) // 2
        )

        try:
            main_win.addstr(
                start_y + i,
                x,
                line
            )

        except curses.error:
            pass

    # ==================================================
    # TEXTO
    # ==================================================

    dots = "." * (
        (frame_index % 3) + 1
    )

    if state.get("connecting"):
        text = f"Trying to connect to server{dots}"
    else:
        text = (
            "Trying to match u "
            f"with someone{dots}"
        )

    text_y = (
        start_y
        + frame_height
        + 2
    )

    text_x = max(
        0,
        (width - len(text)) // 2
    )

    try:
        main_win.addstr(
            text_y,
            text_x,
            text
        )

    except curses.error:
        pass

    # ==================================================
    # LOG DA CONEXÃO (só enquanto tenta conectar ao servidor)
    # ==================================================
    if state.get("connecting"):
        room = height - (text_y + 1)          # linhas livres abaixo do texto
        n = min(3, room)

        if n > 0:
            for i, (kind, line) in enumerate(state["connect_log"][-n:]):
                line = line[:max(0, width - 2)]
                attr = (
                    curses.color_pair(1)
                    if kind == "error"
                    else curses.A_DIM
                )

                try:
                    main_win.addstr(
                        text_y + 1 + i,
                        max(0, (width - len(line)) // 2),
                        line,
                        attr,
                    )
                except curses.error:
                    pass

    # Conteúdo + borda de todas as janelas (touchwin + noutrefresh)
    queue_layout(layout)

def draw_call_screen(layout, state, box, chat):
    """Desenha o layout normal da chamada."""

    # -----------------------------------------------
    # Conteúdo dos widgets
    # -----------------------------------------------
    box.draw()
    chat.draw()

    # -----------------------------------------------
    # Vídeo do usuário e do amigo (com placeholder)
    # -----------------------------------------------
    if state["camera_ok"] is None:
        my_placeholder = "Iniciando câmera..."
    else:
        my_placeholder = "Sem imagem da câmera"

    draw_ascii_lines(
        layout["me"][1],
        state["my_frame"],
        placeholder=my_placeholder,
        title="You",
        pair=2,
    )

    draw_ascii_lines(
        layout["friend"][1],
        state["friend_frame"],
        placeholder="Aguardando vídeo do amigo...",
        title="Friend",
        pair=3,
    )

    # -----------------------------------------------
    # Nerd info: UMA linha só (o painel tem 1 linha útil;
    # antes o erro ia para a linha 1, que não existe, e
    # o curses.error era engolido, então nunca aparecia).
    # -----------------------------------------------
    logs_win = layout["logs"][1]
    logs_win.erase()
    _, logs_w = logs_win.getmaxyx()

    camera_status = {
        None: "iniciando",
        True: "OK",
        False: "ERRO",
    }[state["camera_ok"]]

    mic_status = "ON" if state["mic_active"] else "MUTED"
    if state["audio_in_ok"] is False:
        mic_status = "ERRO"

    audio_out_status = {
        None: "iniciando",
        True: "OK",
        False: "ERRO",
    }[state["audio_out_ok"]]

    # Em terminais estreitos usa o rótulo curto para a linha caber.
    audio_label = "Saída de áudio" if logs_w >= 72 else "Som"

    ws_label = "WebSocket" if logs_w >= 72 else "WS"

    parts = [
        f"{ws_label}: "
        + ("CONNECTED" if state["connected"] else "DISCONNECTED"),
        f"Câmera: {camera_status}",
        f"Mic: {mic_status}",
        f"{audio_label}: {audio_out_status}",
    ]

    if state["audio_out_error"]:
        parts.append(state["audio_out_error"])

    if state["audio_in_error"]:
        parts.append("Mic: " + state["audio_in_error"])

    if state["camera_error"]:
        parts.append(state["camera_error"])

    if state["network_error"]:
        parts.append(state["network_error"])

    try:
        logs_win.addnstr(
            0,
            0,
            " | ".join(parts),
            max(0, logs_w - 1),
        )
    except curses.error:
        pass

    # -----------------------------------------------
    # Conteúdo + borda de todas as janelas
    # -----------------------------------------------
    queue_layout(layout)


async def curses_main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)

    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(
        1,
        curses.COLOR_RED,
        -1,
    )
    curses.init_pair(
        2,
        curses.COLOR_GREEN,
        -1,
    )
    curses.init_pair(
        3,
        curses.COLOR_CYAN,
        -1,
    )

    layout = create_layout(stdscr)
    waiting_layout = create_waiting_layout(stdscr)

    box = TextBox(
        layout["input"][1]
        if layout
        else None
    )

    chat = ChatBox(
        layout["chat"][1]
        if layout
        else None
    )

    my_video = Camera()

    def rebuild_layouts():
        """Recria as janelas depois de um resize do terminal."""
        nonlocal layout, waiting_layout

        curses.update_lines_cols()

        # clear() deixa o stdscr "sujo". Sem o refresh() logo
        # em seguida, o PRÓXIMO get_wch() faz um refresh
        # implícito do stdscr (em branco) por cima das janelas
        # já desenhadas, e as bordas somem.
        stdscr.clear()
        stdscr.refresh()

        layout = create_layout(stdscr)
        waiting_layout = create_waiting_layout(stdscr)

        box.set_window(
            layout["input"][1]
            if layout
            else None
        )

        chat.set_window(
            layout["chat"][1]
            if layout
            else None
        )

    state = {
        "connected": False,
        "network_error": None,

        "my_frame": [],
        "friend_frame": [],

        "camera_ok": None,       # None = ainda iniciando
        "camera_error": None,
        
        "mic_active": True,      # Começa com o microfone ativado

        "audio_in_ok": None,     # None = ainda iniciando
        "audio_in_error": None,
        "audio_out_ok": None,    # saída de áudio (alto-falante)
        "audio_out_error": None,

        "paired": False,

        "connecting": True,      # tentando conectar ao servidor
        "connect_log": [],       # [(kind, texto)] mostrado na tela de espera

        "camera_size": (1, 1),

        "people": 0,
        "pairs": 0,
    }

    try:
        # =====================================================
        # FASE DE CONEXÃO: tela de espera + log até o servidor responder
        # =====================================================
        connector = asyncio.create_task(connect_with_retry(state))

        frame_index = 0
        last_animation = time.monotonic()
        animation_interval = 0.2

        try:
            while not connector.done():
                try:
                    key = stdscr.get_wch()
                except curses.error:
                    key = None

                if key == curses.KEY_RESIZE:
                    rebuild_layouts()

                if waiting_layout is None:
                    stdscr.erase()

                    try:
                        stdscr.addstr(0, 0, "Terminal too small")
                    except curses.error:
                        pass

                    stdscr.refresh()
                else:
                    now = time.monotonic()

                    if now - last_animation >= animation_interval:
                        frame_index = (
                            frame_index + 1
                        ) % len(GLASSES_FRAMES)

                        last_animation = now

                    draw_waiting_screen(
                        waiting_layout,
                        state,
                        frame_index
                    )

                    curses.doupdate()

                await asyncio.sleep(0.03)
        finally:
            if not connector.done():
                connector.cancel()

        websocket = connector.result()

        state["connecting"] = False
        state["connect_log"].clear()
        state["connected"] = True

        async with websocket:

            receive_task = asyncio.create_task(
                receive_loop(
                    websocket,
                    chat,
                    state,
                )
            )

            camera_task = asyncio.create_task(
                camera_send_loop(
                    websocket,
                    my_video,
                    state,
                )
            )
            
            audio_task = asyncio.create_task(
                audio_send_loop(
                    websocket,
                    state,
                )
            )

            frame_index = 0
            last_animation = time.monotonic()
            animation_interval = 0.2
            
            try:
                while True:

                    # =====================================
                    # TECLADO
                    # =====================================

                    try:
                        key = stdscr.get_wch()
                    except curses.error:
                        key = None

                    if key == curses.KEY_RESIZE:
                        rebuild_layouts()

                    elif key is not None:

                        # Só permite chat quando existe parceiro.
                        if state["paired"]:

                            if chat.handle_key(key):
                                pass

                            else:
                                msg = box.handle_key(key)

                                if msg == "/sair":
                                    break

                                if msg == "/type":
                                    my_video.TIPO = not my_video.TIPO
                                    
                                elif msg == "/mic":
                                    # Alterna o estado do microfone
                                    state["mic_active"] = not state["mic_active"]
                                    status = "ativado" if state["mic_active"] else "mutado"
                                    chat.add_message("System", f"Microfone {status}.")

                                elif msg == "/skip":
                                    # Envia o pedido de skip para o servidor
                                    await send_json(websocket, "skip")
                                    
                                    # Reseta a tela local para voltar ao modo de espera
                                    state["paired"] = False
                                    state["friend_frame"] = []
                                    chat.messages.clear() # Limpa o chat para a próxima pessoa
                                    
                                    # Redesenha a tela imediatamente para não esperar o próximo frame
                                    stdscr.clear()
                                    stdscr.refresh()

                                elif msg:
                                    chat.add_message(
                                        "Você",
                                        msg,
                                        mine=True,
                                    )
                                    
                                    

                                    await send_json(
                                        websocket,
                                        "chat",
                                        text=msg,
                                    )

                    # =====================================
                    # TERMINAL PEQUENO
                    # =====================================

                    if (
                        layout is None
                        or waiting_layout is None
                    ):
                        stdscr.erase()

                        try:
                            stdscr.addstr(
                                0,
                                0,
                                "Terminal too small",
                            )
                        except curses.error:
                            pass

                        stdscr.refresh()

                        await asyncio.sleep(0.1)
                        continue

                    # =====================================
                    # TAMANHO DA CÂMERA
                    # =====================================

                    camera_h, camera_w = (
                        layout["me"][1].getmaxyx()
                    )

                    # desconta a borda, que agora é desenhada por nós
                    state["camera_size"] = (
                        max(1, camera_w - 2),
                        max(1, camera_h - 2),
                    )

                    # =====================================
                    # ESCOLHA DA TELA
                    # =====================================
                    if not state["paired"]:

                        now = time.monotonic()

                        if now - last_animation >= animation_interval:
                            frame_index = (
                                frame_index + 1
                            ) % len(GLASSES_FRAMES)

                            last_animation = now

                        draw_waiting_screen(
                            waiting_layout,
                            state,
                            frame_index
                        )

                        curses.doupdate()

                    else:

                        draw_call_screen(
                            layout,
                            state,
                            box,
                            chat,
                        )

                        curses.doupdate()

                    # Dá controle às tasks do asyncio.
                    await asyncio.sleep(0.03)

            finally:
                receive_task.cancel()
                camera_task.cancel()
                audio_task.cancel()

                await asyncio.gather(
                    receive_task,
                    camera_task,
                    audio_task,
                    return_exceptions=True,
                )

    except (
        ConnectionClosed,
        WebSocketException,
        OSError,
    ) as exc:

        if layout is not None:
            chat.add_message(
                "System",
                f"Não foi possível conectar: {exc}",
            )

            chat.draw()
            refresh_layout(layout)

            await asyncio.sleep(2)

    finally:
        my_video.release()


def main(stdscr):
    with StderrToFile():
        asyncio.run(
            curses_main(stdscr)
        )


def run():
    """Ponto de entrada do comando `loops` (pyproject: loops = "Client:run")."""
    curses.wrapper(main)


if __name__ == "__main__":
    curses.wrapper(main)