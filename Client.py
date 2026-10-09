import asyncio
import curses
import json
import os
import sys
import time
import threading
import collections
import zlib
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


# ==================================================
# COR DO VÍDEO (fundo de cada célula)
# ==================================================
# Cada célula do terminal tem duas camadas: a "tinta" (o caractere) e o
# "papel" (a cor de fundo). O vídeo em cor pinta o papel de cada célula com a
# cor do pixel (o caractere vira um espaço), então a imagem vira um mosaico.
# Os índices (16..255 da paleta xterm-256) viajam comprimidos junto do quadro.

def encode_colors(colors):
    return base64.b64encode(zlib.compress(colors.tobytes(), 1)).decode("ascii")


def decode_colors(data, rows, cols):
    """Devolve uint8 (rows, cols), ou None se vier ausente ou inválido."""
    try:
        rows, cols = int(rows), int(cols)
        if not data or rows < 1 or cols < 1:
            return None

        d = zlib.decompressobj()
        # max_length: um parceiro mal-intencionado não consegue estourar a memória
        raw = d.decompress(base64.b64decode(data), rows * cols + 1)

        if len(raw) != rows * cols:
            return None

        arr = np.frombuffer(raw, dtype=np.uint8).reshape(rows, cols)
        return np.clip(arr, 16, 255)
    except Exception:
        return None


def init_color_pairs():
    """Um par por cor da paleta: fundo = cor i (o número do par é o próprio i).

    Devolve False se o terminal não tem 256 cores (aí o vídeo fica preto e branco).
    """
    try:
        if curses.COLORS < 256 or curses.COLOR_PAIRS < 256:
            return False

        for i in range(16, 256):
            curses.init_pair(i, -1, i)

        return True
    except curses.error:
        return False


def draw_color_row(win, y, x, row):
    """Pinta uma linha de células. Agrupa vizinhas de mesma cor (1 chamada por trecho)."""
    n = len(row)
    cuts = np.flatnonzero(row[1:] != row[:-1]) + 1
    starts = [0] + cuts.tolist()
    ends = cuts.tolist() + [n]

    for a, b in zip(starts, ends):
        try:
            win.addnstr(
                y,
                x + a,
                " " * (b - a),
                b - a,
                curses.color_pair(int(row[a])),
            )
        except curses.error:
            pass


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


def draw_ascii_lines(win, lines, placeholder="", title="", pair=0, colors=None):
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

    paint = None
    if (
        colors is not None
        and colors.shape[0] >= image_h
        and colors.shape[1] >= image_w
    ):
        paint = colors[:image_h, :image_w]

    for i, line in enumerate(visible):
        if paint is not None:
            draw_color_row(win, box_y + 1 + i, box_x + 1, paint[i])
            continue

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
# FORMATO DE ÁUDIO: o que o driver aceita x o que a rede usa
# ==================================================
# Na rede o áudio é sempre 16 kHz mono (SAMPLE_RATE). Muitas placas de som só
# aceitam 44,1/48 kHz e/ou estéreo ("Invalid sample rate [PaErrorCode -9997]").
# Então testamos combinações até o driver aceitar e convertemos a taxa.

class Resampler:
    """Reamostra áudio mono em blocos (interpolação linear + filtro anti-aliasing).

    Ao REDUZIR a taxa (microfone 48 kHz -> 16 kHz), o que passa de 8 kHz
    "dobraria" para dentro da faixa da voz e viraria chiado; por isso o sinal
    passa antes por um filtro passa-baixa (sinc com janela de Hamming).
    Guarda o estado entre os blocos, então não aparecem cliques nas emendas.
    """

    def __init__(self, src_rate, dst_rate):
        self.step = src_rate / dst_rate          # amostras de entrada por saída

        if dst_rate < src_rate:
            taps = 16 * max(1, int(round(self.step))) + 1
            fc = 0.4 * dst_rate / src_rate       # corte em 40% da taxa de saída
            n = np.arange(taps) - (taps - 1) / 2
            h = 2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)
            self.kernel = (h / h.sum()).astype(np.float32)
        else:
            self.kernel = None

        self.hist = np.zeros(
            len(self.kernel) - 1 if self.kernel is not None else 0,
            dtype=np.float32,
        )
        self.buf = np.zeros(0, dtype=np.float32)
        self.pos = 0.0                           # posição da próxima saída em buf

    def process(self, x):
        y = np.asarray(x, dtype=np.float32)

        if self.kernel is not None:
            ext = np.concatenate((self.hist, y))
            self.hist = ext[len(ext) - len(self.hist):]
            y = np.convolve(ext, self.kernel, mode="valid")

        buf = np.concatenate((self.buf, y))
        last = len(buf) - 1

        if last < self.pos:
            self.buf = buf
            return np.zeros(0, dtype=np.float32)

        n = int((last - self.pos) // self.step) + 1
        idx = self.pos + np.arange(n) * self.step
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)

        nxt = self.pos + n * self.step
        # Se a próxima saída cai depois do fim do buffer, o excesso fica em
        # self.pos (relativo ao início do PRÓXIMO bloco), senão perderíamos fase.
        drop = min(int(nxt), len(buf))
        self.buf = buf[drop:]
        self.pos = nxt - drop
        return out


def audio_candidates(kind):
    """(dispositivo, taxa, canais) para tentar, do mais desejável ao menos.

    Primeiro o padrão do sistema em 16 kHz mono (como sempre foi); depois
    outras taxas/canais no padrão; por último dispositivos virtuais
    (pulse/pipewire/default), que costumam aceitar qualquer taxa.
    """
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    devices = []

    try:
        devices.append((None, sd.query_devices(kind=kind)))
    except Exception:
        devices.append((None, {key: 2, "default_samplerate": 48000}))

    try:
        for index, info in enumerate(sd.query_devices()):
            if info[key] > 0 and info["name"].lower() in (
                "pulse", "pipewire", "default", "sysdefault"
            ):
                devices.append((index, info))
    except Exception:
        pass

    for device, info in devices:
        rates = []
        for rate in (SAMPLE_RATE, info.get("default_samplerate"), 48000, 44100):
            if rate and int(rate) not in rates:
                rates.append(int(rate))

        channel_options = [1] + ([2] if info.get(key, 2) >= 2 else [])

        for rate in rates:
            for channels in channel_options:
                yield device, rate, channels


def open_audio_stream(kind, make_stream):
    """Tenta cada combinação até o driver aceitar.

    make_stream(device, rate, channels) devolve um stream já iniciado ou levanta
    erro. Devolve (stream, rate, channels). Se nada funcionar, levanta o erro da
    PRIMEIRA tentativa (a do dispositivo padrão), que é o mais informativo.
    """
    first_error = None
    tried = 0

    # Diagnóstico para ~/.loops/loops.log (stderr não aparece na tela)
    try:
        print(f"[audio:{kind}] dispositivos:\n{sd.query_devices()}", file=sys.stderr)
    except Exception as exc:
        print(f"[audio:{kind}] não consegui listar dispositivos: {exc}", file=sys.stderr)

    for device, rate, channels in audio_candidates(kind):
        tried += 1
        try:
            stream = make_stream(device, rate, channels)
            print(
                f"[audio:{kind}] OK: dispositivo={device} {rate} Hz {channels} canal(is)",
                file=sys.stderr,
            )
            return (stream, rate, channels)
        except Exception as exc:
            print(
                f"[audio:{kind}] falhou: dispositivo={device} {rate} Hz "
                f"{channels} canal(is): {short_error(exc)}",
                file=sys.stderr,
            )
            if first_error is None:
                first_error = exc

    if first_error is None:
        raise RuntimeError("nenhum dispositivo de áudio encontrado")

    message = f"{first_error} (tentei {tried} combinações)"

    try:
        error = type(first_error)(message)
    except Exception:
        error = RuntimeError(message)

    raise error from first_error


def rate_note(rate):
    """' (48k)' quando o áudio está sendo convertido; vazio no formato da rede."""
    if not rate or rate == SAMPLE_RATE:
        return ""
    return f" ({rate / 1000:g}k)"


# ==================================================
# REPRODUÇÃO DO ÁUDIO (buffer de jitter + callback)
# ==================================================
# Por que não usar stream.write()? Ele BLOQUEIA até a placa tocar o som. Dentro
# do asyncio isso prende o programa inteiro (tela, teclado, vídeo) e, pior: o
# recebimento passa a andar exatamente na velocidade do áudio e nunca "alcança"
# a fila, então nem devolve o controle ao loop. Além disso, sem colchão, qualquer
# pacote atrasado da rede vira um buraco de som (estalo/chiado).
#
# Aqui o PortAudio chama o callback (em outra thread) quando a placa precisa de
# som; o asyncio só deposita os blocos recebidos num buffer e segue a vida.
PLAY_MIN_BUFFER = 0.10     # s de colchão antes de começar a tocar
PLAY_STEP_UP = 0.04        # s a mais de colchão a cada falha de som
PLAY_CAP = 0.50            # s: colchão máximo
PLAY_MAX_EXTRA = 0.30      # s acima do colchão; além disso descarta o mais antigo
FADE_FRAMES = 48           # suaviza o corte quando o som acaba (evita estalo)


class AudioPlayer:
    def __init__(self, device, rate, channels):
        self.rate = rate
        self.channels = channels
        self.resampler = Resampler(SAMPLE_RATE, rate) if rate != SAMPLE_RATE else None

        self._lock = threading.Lock()
        self._chunks = collections.deque()      # blocos (frames, canais) int16
        self._buffered = 0                      # frames guardados
        self._playing = False
        self._fade_in = False
        self._last = None                       # último frame tocado (para o fade-out)
        self._target = int(PLAY_MIN_BUFFER * rate)
        self._chunk_frames = 0                  # tamanho típico dos blocos que chegam
        self.underruns = 0
        self.dropped = 0

        self.stream = sd.OutputStream(
            device=device,
            samplerate=rate,
            channels=channels,
            dtype="int16",
            callback=self._callback,
            latency=0.05,
        )
        self.stream.start()

    def _goal(self):
        """Quanto som acumular antes de (re)começar a tocar."""
        # blocos grandes (cliente antigo manda 250 ms) precisam de colchão maior
        goal = max(self._target, int(1.3 * self._chunk_frames))
        return min(goal, int(PLAY_CAP * self.rate))

    def push(self, samples):
        """Recebe áudio na taxa da rede (16 kHz, mono, int16)."""
        x = samples

        if self.resampler is not None:
            x = np.clip(self.resampler.process(x), -32768, 32767).astype(np.int16)

        if self.channels > 1:
            x = np.repeat(x[:, None], self.channels, axis=1)
        else:
            x = x.reshape(-1, 1)

        n = len(x)
        if n == 0:
            return

        with self._lock:
            self._chunks.append(x)
            self._buffered += n
            self._chunk_frames = max(n, int(self._chunk_frames * 0.98))

            # atraso não pode crescer sem limite (rede travou e liberou tudo de uma vez)
            limit = self._goal() + int(PLAY_MAX_EXTRA * self.rate)
            while self._buffered > limit and len(self._chunks) > 1:
                old = self._chunks.popleft()
                self._buffered -= len(old)
                self.dropped += len(old)

    def _callback(self, outdata, frames, time_info, status):
        underrun = False
        fade_in = False
        pos = 0

        with self._lock:
            if not self._playing:
                if self._buffered >= self._goal():
                    self._playing = True
                    fade_in = True
                else:
                    outdata.fill(0)
                    return

            while pos < frames and self._chunks:
                chunk = self._chunks[0]
                n = min(frames - pos, len(chunk))
                outdata[pos:pos + n] = chunk[:n]

                if n == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[n:]

                pos += n

            self._buffered -= pos

            if pos < frames:
                underrun = True
                outdata[pos:] = 0
                self._playing = False
                self.underruns += 1
                # a rede está instável: aumenta o colchão
                self._target = min(
                    self._target + int(PLAY_STEP_UP * self.rate),
                    int(PLAY_CAP * self.rate),
                )

        if fade_in:
            k = min(frames, FADE_FRAMES)
            ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)[:, None]
            outdata[:k] = (outdata[:k] * ramp).astype(np.int16)

        if underrun:
            k = min(pos, FADE_FRAMES)
            if k > 0:
                ramp = np.linspace(1.0, 0.0, k, dtype=np.float32)[:, None]
                outdata[pos - k:pos] = (outdata[pos - k:pos] * ramp).astype(np.int16)
            elif self._last is not None:
                k = min(frames, FADE_FRAMES)
                ramp = np.linspace(1.0, 0.0, k, dtype=np.float32)[:, None]
                outdata[:k] = (self._last * ramp).astype(np.int16)
            self._last = None
        elif pos > 0:
            self._last = outdata[pos - 1].astype(np.float32)

    def alive(self):
        try:
            return bool(self.stream.active)
        except Exception:
            return False

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

        print(
            f"[audio:output] reprodução: {self.underruns} falhas de som, "
            f"{self.dropped} amostras descartadas, colchão final "
            f"{self._goal() * 1000 // self.rate} ms",
            file=sys.stderr,
        )


def link_congested(websocket, limit=32_000):
    """True se a conexão já tem muito dado esperando para sair (internet lenta).

    O vídeo cede lugar ao áudio: quadro atrasado não faz falta, som atrasado sim.
    """
    try:
        return websocket.transport.get_write_buffer_size() > limit
    except Exception:
        return False


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

    def make_input(device, rate, channels):
        resampler = (
            Resampler(rate, SAMPLE_RATE)
            if rate != SAMPLE_RATE
            else None
        )

        def audio_callback(indata, frames, time_info, status):
            if status:
                pass

            try:
                mono = indata[:, 0] if channels == 1 else indata.mean(axis=1)

                if resampler is not None:
                    mono = resampler.process(mono)

                audio_int16 = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
                audio_bytes = audio_int16.tobytes()
                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

                # O callback roda em outra thread, então usamos
                # call_soon_threadsafe para voltar ao event loop principal.
                loop.call_soon_threadsafe(
                    audio_send_queue.put_nowait,
                    audio_b64
                )
            except Exception:
                pass

        stream = sd.InputStream(
            device=device,
            samplerate=rate,
            channels=channels,
            dtype="float32",
            callback=audio_callback,
            # mesmo tempo de bloco (250 ms) em qualquer taxa
            blocksize=max(1, round(CHUNK_SIZE * rate / SAMPLE_RATE)),
        )
        stream.start()
        return stream

    try:
        stream_in, in_rate, _ = open_audio_stream("input", make_input)
        state["audio_in_rate"] = in_rate
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

            await asyncio.sleep(0)

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
                colors = None

                try:
                    result = await asyncio.to_thread(
                        camera.get_frame,
                        width,
                        height,
                    )
                except Exception as exc:
                    state["camera_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    if result is None:
                        state["camera_error"] = (
                            "sem imagem da câmera "
                            "(em uso por outro processo?)"
                        )
                    else:
                        frame, colors = result

                if frame is None:
                    state["camera_ok"] = False
                    state["my_frame"] = []
                    state["my_colors"] = None
                else:
                    state["camera_ok"] = True
                    state["camera_error"] = None
                    state["my_frame"] = frame
                    state["my_colors"] = colors

                    extra = {}
                    if colors is not None:
                        extra = {
                            "colors": encode_colors(colors),
                            "cols": int(colors.shape[1]),
                        }

                    if not link_congested(websocket):
                        await send_json(
                            websocket,
                            "video",
                            frame="\n".join(frame),
                            **extra,
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
    player = None

    def make_output(device, rate, channels):
        return AudioPlayer(device, rate, channels)

    try:
        player, out_rate, _ = open_audio_stream("output", make_output)

        state["audio_out_rate"] = out_rate
        state["audio_out_ok"] = True
    except Exception as exc:
        # Driver recusou a saída de áudio: continua recebendo vídeo e chat.
        player = None
        state["audio_out_ok"] = False
        state["audio_out_error"] = short_error(exc)
        chat.add_message(
            "System",
            f"Sem saída de áudio: {state['audio_out_error']}",
        )

    last_alive_check = time.monotonic()

    try:
        async for raw_message in websocket:
            # Se houver mensagens acumuladas, o "async for" não devolve o controle
            # ao event loop sozinho: sem isto a tela e o teclado congelam.
            await asyncio.sleep(0)

            try:
                message = json.loads(raw_message)
            except (TypeError, json.JSONDecodeError):
                continue

            if not isinstance(message, dict):
                continue

            message_type = message.get("type")

            if message_type == "audio":
                # Entrega o áudio do parceiro ao player (não bloqueia)
                if player is None:
                    continue

                try:
                    raw_bytes = base64.b64decode(message["data"])
                    audio_array = np.frombuffer(
                        raw_bytes[: len(raw_bytes) // 2 * 2],
                        dtype=np.int16,
                    )
                    player.push(audio_array)
                except Exception as exc:
                    state["audio_out_ok"] = False
                    state["audio_out_error"] = short_error(exc)
                    player.close()
                    player = None
                    chat.add_message(
                        "System",
                        f"Saída de áudio parou: {state['audio_out_error']}",
                    )
                    continue

                # Dispositivo sumiu no meio da chamada?
                now = time.monotonic()
                if now - last_alive_check > 1.0:
                    last_alive_check = now

                    if not player.alive():
                        state["audio_out_ok"] = False
                        state["audio_out_error"] = "dispositivo de áudio parou"
                        player.close()
                        player = None
                        chat.add_message(
                            "System",
                            "Saída de áudio parou: dispositivo de áudio parou",
                        )

            elif message_type == "video":
                raw_frame = message.get("frame", "")
                lines = raw_frame.split("\n")
                state["friend_frame"] = lines
                state["friend_colors"] = decode_colors(
                    message.get("colors"),
                    len(lines),
                    message.get("cols"),
                )

            elif message_type == "system":
                system_message = message.get("message", "")

                if system_message == "Partner found!":
                    state["paired"] = True

                elif system_message == "The other person left.":
                    state["paired"] = False
                    state["friend_frame"] = []
                    state["friend_colors"] = None

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
        if player is not None:
            player.close()


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
        colors=state["my_colors"] if state["color_ok"] else None,
    )

    draw_ascii_lines(
        layout["friend"][1],
        state["friend_frame"],
        placeholder="Aguardando vídeo do amigo...",
        title="Friend",
        pair=3,
        colors=state["friend_colors"] if state["color_ok"] else None,
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
    elif logs_w >= 72:
        mic_status += rate_note(state["audio_in_rate"])

    audio_out_status = {
        None: "iniciando",
        True: "OK",
        False: "ERRO",
    }[state["audio_out_ok"]]

    # Em terminais estreitos usa o rótulo curto para a linha caber.
    audio_label = "Saída de áudio" if logs_w >= 72 else "Som"

    if state["audio_out_ok"] and logs_w >= 72:
        audio_out_status += rate_note(state["audio_out_rate"])

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

    color_ok = init_color_pairs()

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
    my_video.COLOR = color_ok

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

        "color_ok": color_ok,    # terminal com 256 cores?
        "my_colors": None,       # índices xterm-256 por célula (ou None)
        "friend_colors": None,

        "camera_ok": None,       # None = ainda iniciando
        "camera_error": None,
        
        "mic_active": True,      # Começa com o microfone ativado

        "audio_in_ok": None,     # None = ainda iniciando
        "audio_in_error": None,
        "audio_in_rate": None,   # taxa real aberta no driver (Hz)
        "audio_out_rate": None,
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
                                    
                                elif msg == "/color":
                                    if not state["color_ok"]:
                                        chat.add_message(
                                            "System",
                                            "Seu terminal não suporta 256 cores.",
                                        )
                                    else:
                                        my_video.COLOR = not my_video.COLOR
                                        status = "ativadas" if my_video.COLOR else "desativadas"
                                        chat.add_message("System", f"Cores {status}.")

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
                                    state["friend_colors"] = None
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