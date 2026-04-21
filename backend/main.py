import os
import asyncio
import logging
import base64
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --- Configuration ---
WS_PORT = int(os.environ.get("PORT", 8080))
FIXED_TARGET = "OC4yMTUuMS40OTozNTc3Ng=="  # base64(host:port)
MAX_PAYLOAD = 100 * 1024  # 100KB


# --- Helpers ---
def validate_port(port: str) -> bool:
    try:
        p = int(port)
        return 0 < p <= 65535
    except ValueError:
        return False


def decode_target(encoded: str):
    decoded = base64.b64decode(encoded).decode("utf-8")
    parts = decoded.rsplit(":", 1)
    if len(parts) != 2:
        raise ValueError("Invalid target format")
    host, port = parts
    if not host or not port:
        raise ValueError("Invalid target format")
    if not validate_port(port):
        raise ValueError("Invalid port number")
    return host, int(port)


# --- HTTP Health Route ---
async def health(request):
    logger.info("Health check accessed")
    return PlainTextResponse("MCP SERVER READY !!!\n")


# --- WebSocket to TCP Proxy ---
async def websocket_proxy(websocket: WebSocket):
    # Accept bất kể subprotocol nào client gửi lên
    subprotocols = websocket.headers.get("sec-websocket-protocol", "")
    requested = [s.strip() for s in subprotocols.split(",") if s.strip()]
    accept_proto = requested[0] if requested else None

    await websocket.accept(subprotocol=accept_proto)

    client_host = websocket.client.host if websocket.client else "unknown"

    # Decode target
    try:
        host, port = decode_target(FIXED_TARGET)
    except Exception as err:
        logger.error(f"[ERROR] Base64 decode failed: {err}")
        await websocket.close(code=1008)
        return

    logger.info(f"[WS] Client {client_host} -> {host}:{port}")

    # Open TCP connection
    try:
        reader, writer = await asyncio.open_connection(host, port)
        logger.info(f"[TCP] Connected -> {host}:{port}")
    except Exception as err:
        logger.error(f"[TCP ERROR] {host}:{port}: {err}")
        await websocket.close(code=1011)
        return

    stop_event = asyncio.Event()

    async def cleanup():
        if stop_event.is_set():
            return
        stop_event.set()
        try:
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass
        logger.info(f"[CLEANUP] {host}:{port} closed")

    async def ws_to_tcp():
        try:
            while not stop_event.is_set():
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(), timeout=60
                    )
                except asyncio.TimeoutError:
                    continue
                except WebSocketDisconnect:
                    logger.info(f"[WS] {client_host} disconnected")
                    break

                if len(data.encode("utf-8")) > MAX_PAYLOAD:
                    logger.warning("[WS->TCP] Payload too large, dropped")
                    continue

                msg = data if data.endswith("\n") else data + "\n"
                writer.write(msg.encode("utf-8"))
                await writer.drain()
        except Exception as err:
            logger.error(f"[WS->TCP ERROR] {err}")
        finally:
            await cleanup()

    async def tcp_to_ws():
        try:
            while not stop_event.is_set():
                data = await reader.read(4096)
                if not data:
                    logger.info(f"[TCP] {host}:{port} closed by remote")
                    break
                text = data.decode("utf-8", errors="replace")
                await websocket.send_text(text)
        except Exception as err:
            logger.error(f"[TCP->WS ERROR] {err}")
        finally:
            await cleanup()

    await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)


# --- App & Routing ---
routes = [
    Route("/health", health),
    Route("/", health),                        # HTTP GET / -> 200 tranh 302
    WebSocketRoute("/", websocket_proxy),
    WebSocketRoute("/ws", websocket_proxy),
]

app = Starlette(routes=routes)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])


# --- Entry Point ---
if __name__ == "__main__":
    logger.info(f"[PROXY] Listening on port {WS_PORT}")
    uvicorn.run(
        "proxy_server:app",
        host="0.0.0.0",
        port=WS_PORT,
        log_level="info",
        ws="websockets",
        ws_ping_interval=30,
        ws_ping_timeout=10,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
