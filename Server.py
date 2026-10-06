import asyncio
import json
from collections import deque
import os


from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
import logging
from websockets.exceptions import InvalidMessage


class IgnoreProbes(logging.Filter):
    """Esconde handshakes falhos de health check / port scan."""

    def filter(self, record):
        if record.exc_info and isinstance(record.exc_info[1], InvalidMessage):
            return False  # descarta este registro
        return True


logging.getLogger("websockets.server").addFilter(IgnoreProbes())

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8765))

# Clientes que ainda não encontraram um parceiro.
waiting = deque()

# Relação cliente -> parceiro.
# Exemplo:
#   A -> B
#   B -> A
partners = {}


def make_message(message_type, **data):
    """Monta uma mensagem do protocolo usado entre cliente e servidor."""
    return json.dumps(
        {"type": message_type, **data},
        ensure_ascii=False,
    )


def get_stats():
    """Retorna (pessoas conectadas, pares ativos)."""
    people = len(waiting) + len(partners)
    pairs = len(partners) // 2
    return people, pairs


def get_clients():
    """Retorna todos os clientes atualmente conectados."""
    clients = set(waiting)
    clients.update(partners.keys())
    return clients


def pair_client(websocket, ignore=None):
    """Tenta encontrar alguém esperando e cria o par, ignorando um ex-parceiro."""
    for i in range(len(waiting)):
        other = waiting[i]
        
        # Se a pessoa na fila não for quem queremos ignorar, forma o par!
        if other != ignore:
            del waiting[i] # Remove a pessoa da fila

            partners[websocket] = other
            partners[other] = websocket

            return other

    # Se a fila estiver vazia ou só tiver a pessoa ignorada, entra na fila.
    waiting.append(websocket)
    return None


async def send_json(websocket, message):
    try:
        await websocket.send(message)
    except ConnectionClosed:
        pass


async def send_system(websocket, message):
    await send_json(
        websocket,
        make_message("system", message=message),
    )


async def broadcast_stats():
    """Envia as estatísticas atuais para todos os clientes."""
    people, pairs = get_stats()

    message = make_message(
        "server_stats",
        people=people,
        pairs=pairs,
    )

    clients = get_clients()

    if not clients:
        return

    await asyncio.gather(
        *(send_json(client, message) for client in clients),
        return_exceptions=True,
    )


async def handler(websocket):
    partner = pair_client(websocket)

    if partner is None:
        await send_system(
            websocket,
            "Waiting for someone else...",
        )
    else:
        await asyncio.gather(
            send_system(
                websocket,
                "Partner found!",
            ),
            send_system(
                partner,
                "Partner found!",
            ),
        )

    await broadcast_stats()

    people, pairs = get_stats()
    print(
        f"Client connected. "
        f"People: {people} | Pairs: {pairs}",
        flush = True,
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

            # ----------------------------------------------------
            # LÓGICA DE SKIP (PULAR)
            # ----------------------------------------------------
            if message_type == "skip":
                partner = partners.pop(websocket, None)
                
                if partner is not None:
                    # Remove o parceiro da relação
                    partners.pop(partner, None)

                    await send_system(partner, "The other person left.")
                    new_partner = pair_client(partner) # Parceiro não ignora ninguém

                    if new_partner is None:
                        await send_system(partner, "Waiting for someone else...")
                    else:
                        await asyncio.gather(
                            send_system(partner, "Partner found!"),
                            send_system(new_partner, "Partner found!"),
                        )

                    new_match = pair_client(websocket, ignore=partner)

                    if new_match is None:
                        await send_system(websocket, "Waiting for someone else...")
                    else:
                        await asyncio.gather(
                            send_system(websocket, "Partner found!"),
                            send_system(new_match, "Partner found!"),
                        )
                    
                    await broadcast_stats()
                continue # Pula para a próxima mensagem do loop
            # ----------------------------------------------------

            # AQUI ESTÁ A MUDANÇA: Adicionado "audio" à lista permitida
            if message_type not in {"chat", "video", "audio"}:
                continue

            partner = partners.get(websocket)

            # Ainda não existe parceiro para este cliente.
            if partner is None:
                continue

            try:
                await partner.send(
                    json.dumps(
                        message,
                        ensure_ascii=False,
                    )
                )
            except ConnectionClosed:
                pass

    except ConnectionClosed:
        pass

    finally:
        # Se estava esperando, retira da fila.
        try:
            waiting.remove(websocket)
        except ValueError:
            pass

        # Se estava pareado, desfaz o par.
        partner = partners.pop(websocket, None)

        if partner is not None:
            partners.pop(partner, None)

            await send_system(
                partner,
                "The other person left.",
            )


            new_partner = pair_client(partner)

            if new_partner is None:
                await send_system(
                    partner,
                    "Waiting for someone else...",
                )
            else:
                await asyncio.gather(
                    send_system(partner, "Partner found!"),
                    send_system(new_partner, "Partner found!"),
                )

        await broadcast_stats()

        people, pairs = get_stats()
        print(
            f"Client disconnected. "
            f"People: {people} | Pairs: {pairs}",
            flush = True,

        )


async def main():
    print(f"WebSocket server running at ws://{HOST}:{PORT}", flush = True,)

    async with serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server shut down.", flush = True,)