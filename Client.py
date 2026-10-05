import asyncio
import curses
import json
import time

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
)

from Animation import GLASSES_FRAMES


SERVER_URI = "ws://127.0.0.1:8765"
FPS = 30
FRAME_INTERVAL = 1 / FPS


def make_panel(h, w, y, x, title, pair):
    """Cria uma janela com borda e título."""
    outer = curses.newwin(h, w, y, x)
    outer.box()
    outer.addstr(0, 2, f" {title} ", curses.color_pair(pair))
    inner = outer.derwin(h - 2, w - 2, 1, 1)
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
        ),
        "me": make_panel(
            me_h,
            left_w,
            LOGS_H + friend_h,
            0,
            "You",
            2,
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
        ),
        "main": make_panel(
            main_h,
            W,
            LOGS_H,
            0,
            "Main",
            3,
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


def draw_ascii_lines(win, lines, placeholder=""):
    """Desenha um frame ASCII já processado dentro de uma janela curses.

    Se não há frame, mostra o placeholder (em vez de uma caixa vazia sem
    explicação).
    """
    if win is None:
        return

    h, w = win.getmaxyx()
    win.erase()

    if not lines or h < 1 or w < 1:
        if placeholder and h >= 1 and w >= 2:
            text = placeholder[:w - 1]
            try:
                win.addnstr(
                    h // 2,
                    max(0, (w - len(text)) // 2),
                    text,
                    w - 1,
                    curses.A_DIM,
                )
            except curses.error:
                pass

        win.noutrefresh()
        return

    visible_lines = [line[:w] for line in lines[:h]]

    image_h = len(visible_lines)
    image_w = max(
        (len(line) for line in visible_lines),
        default=0,
    )

    y0 = max(0, (h - image_h) // 2)
    x0 = max(0, (w - image_w) // 2)

    for y, line in enumerate(visible_lines):
        try:
            win.addnstr(
                y0 + y,
                x0,
                line,
                max(0, w - x0),
            )
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


async def camera_send_loop(websocket, camera, state):
    """
    Captura a câmera, transforma em ASCII e envia para o servidor.

    Enquanto não houver parceiro, a câmera não é enviada.

    Erro de CÂMERA (dispositivo ocupado, exceção do cv2/numpy) não é erro de
    REDE: ele é registrado em state["camera_error"] e o loop continua
    tentando, em vez de morrer calado como acontecia antes.
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
    """Recebe vídeo, chat, estado do pareamento e estatísticas."""
    try:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except (TypeError, json.JSONDecodeError):
                continue

            if not isinstance(message, dict):
                continue

            message_type = message.get("type")

            if message_type == "video":
                raw_frame = message.get("frame", "")
                state["friend_frame"] = raw_frame.split("\n")

            elif message_type == "system":
                system_message = message.get("message", "")

                if system_message == "Pessoa encontrada!":
                    state["paired"] = True

                elif system_message == "A outra pessoa saiu.":
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
            2,
            f"Pessoas no servidor: {people}"
        )

        logs_win.addstr(
            0,
            30,
            f"Pares ativos: {pairs}"
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

    text = (
        "Tentando parear você "
        f"com alguém{dots}"
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
    )

    draw_ascii_lines(
        layout["friend"][1],
        state["friend_frame"],
        placeholder="Aguardando vídeo do amigo...",
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

    parts = [
        "WebSocket: "
        + ("CONNECTED" if state["connected"] else "DISCONNECTED"),
        f"Câmera: {camera_status}",
    ]

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

    curses.init_pair(
        1,
        curses.COLOR_RED,
        curses.COLOR_BLACK,
    )
    curses.init_pair(
        2,
        curses.COLOR_GREEN,
        curses.COLOR_BLACK,
    )
    curses.init_pair(
        3,
        curses.COLOR_CYAN,
        curses.COLOR_BLACK,
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

    state = {
        "connected": True,
        "network_error": None,

        "my_frame": [],
        "friend_frame": [],

        "camera_ok": None,       # None = ainda iniciando
        "camera_error": None,

        "paired": False,

        "camera_size": (1, 1),

        "people": 0,
        "pairs": 0,
    }

    try:
        async with connect(SERVER_URI) as websocket:

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

                    state["camera_size"] = (
                        camera_w,
                        camera_h,
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

                await asyncio.gather(
                    receive_task,
                    camera_task,
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
    asyncio.run(
        curses_main(stdscr)
    )


if __name__ == "__main__":
    curses.wrapper(main)
