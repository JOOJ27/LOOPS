import asyncio
import json
from collections import deque

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

HOST = "0.0.0.0"
PORT = 8765

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


def pair_client(websocket):
    """Tenta encontrar alguém esperando e cria o par."""
    while waiting:
        other = waiting.popleft()

        partners[websocket] = other
        partners[other] = websocket

        return other

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
            "Esperando outra pessoa...",
        )
    else:
        await asyncio.gather(
            send_system(
                websocket,
                "Pessoa encontrada!",
            ),
            send_system(
                partner,
                "Pessoa encontrada!",
            ),
        )

    await broadcast_stats()

    people, pairs = get_stats()
    print(
        f"Cliente conectado. "
        f"Pessoas: {people} | Pares: {pairs}"
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

            # O servidor só faz relay desses tipos.
            if message_type not in {"chat", "video"}:
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
                "A outra pessoa saiu.",
            )

            # O parceiro continua conectado: tenta parear com alguém que já
            # esteja esperando. Antes ele ia direto para o fim da fila, e se
            # já houvesse outra pessoa esperando, as duas ficavam esperando
            # uma à outra para sempre.
            new_partner = pair_client(partner)

            if new_partner is None:
                await send_system(
                    partner,
                    "Esperando outra pessoa...",
                )
            else:
                await asyncio.gather(
                    send_system(partner, "Pessoa encontrada!"),
                    send_system(new_partner, "Pessoa encontrada!"),
                )

        await broadcast_stats()

        people, pairs = get_stats()
        print(
            f"Cliente desconectado. "
            f"Pessoas: {people} | Pares: {pairs}"
        )


async def main():
    print(f"Servidor WebSocket em ws://{HOST}:{PORT}")

    async with serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Servidor encerrado.")
