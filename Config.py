import numpy as np


LOGS_H = 3      # altura da janela de logs
INPUT_H = 3     # altura do campo de digitação
MIN_H, MIN_W = 20, 60

CHAR_ASPECT = 0.5  # largura/altura de uma célula do terminal

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 320

# Microphone soft limiter: leave headroom before converting float audio to PCM.
MIC_LIMIT_PEAK = 0.75
MIC_LIMIT_RELEASE_SECONDS = 0.5
MIC_SEND_QUEUE_CHUNKS = 8  # 160 ms at the normal 20 ms chunk size


SERVER_URI = "wss://loops-9j0l.onrender.com"
FPS = 30
FRAME_INTERVAL = 1 / FPS
