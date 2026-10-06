import numpy as np


LOGS_H = 3      # altura da janela de logs
INPUT_H = 3     # altura do campo de digitação
MIN_H, MIN_W = 20, 60

KERNEL = np.array(
            [[ 0, -1,  0],
             [-1,  5, -1],
             [ 0, -1,  0]]
            )

CHAR_ASPECT = 0.5  # largura/altura de uma célula do terminal