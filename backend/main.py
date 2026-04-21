import os
import asyncio
import logging
import base64
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
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

app = FastAPI(title="WebSocket to TCP Stratum Proxy")


# --- HTTP Health Route ---
@app.get("/health")
async def root():
    logger.info("Health check accessed")
    return PlainTextResponse("MCP SERVER READY !!!\n")


# --- Helpers ---
def validate_port(port: str) -> bool:
    try:
        p = int(port)
        return 0 < p <= 65535
    except ValueError:
        return False


def decode_target(encoded: str):
    decoded = base64.b64decode(encoded).decode("utf-8")
    parts = decoded.split(":")
    if len(parts) != 2:
        raise ValueError("Invalid target format")
    host, port = parts
    if not host or not port:
        raise ValueError("Invalid target format")
    if not validate_port(port):
        raise ValueError("Invalid port number")
    return host, int(port)


# --- WebSocket → TCP Proxy ---
@app.websocket("/")
async def websocket_proxy(websocket: WebSocket):
    client_ip = websocket.client.host if websocket.client else "unknown"
    await websocket.accept()

    encoded = FIXED_TARGET
    try:
        host, port = decode_target(encoded)
    except Exception as err:
        logger.error(f"[ERROR] Base64 decode failed: {err}")
        await websocket.close()
        return

    logger.info(f"[WS] Connecting from {client_ip} -> {host}:{port}")

    try:
        reader, writer = await asyncio.open_connection(host, port)
        logger.info(f"[TCP] Connected from {client_ip} -> {host}:{port}")
    except Exception as err:
        logger.error(f"[TCP ERROR] Could not connect to {host}:{port}: {err}")
        await websocket.close()
        return

    is_closing = False

    async def cleanup():
        nonlocal is_closing
        if is_closing:
            return
        is_closing = True
        try:
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass
        logger.info(f"[CLEANUP] Connection closed for {host}:{port}")

    async def ws_to_tcp():
        """Forward messages from WebSocket → TCP."""
        try:
            while not is_closing:
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                except asyncio.TimeoutError:
                    continue

                if len(data.encode("utf-8")) > MAX_PAYLOAD:
                    logger.warning(f"[WS→TCP] Payload too large, dropping message")
                    continue

                msg = data if data.endswith("\n") else data + "\n"
                writer.write(msg.encode("utf-8"))
                await writer.drain()
        except WebSocketDisconnect:
            logger.info(f"[WS] Client {client_ip} disconnected")
        except Exception as err:
            logger.error(f"[ERROR] WS→TCP failed: {err}")
        finally:
            await cleanup()

    async def tcp_to_ws():
        """Forward messages from TCP → WebSocket."""
        try:
            while not is_closing:
                data = await reader.read(4096)
                if not data:
                    logger.info(f"[TCP] Pool socket closed for {host}:{port}")
                    break
                text = data.decode("utf-8", errors="replace")
                await websocket.send_text(text)
        except Exception as err:
            logger.error(f"[ERROR] TCP→WS failed: {err}")
        finally:
            await cleanup()

    # Run both directions concurrently
    await asyncio.gather(
        ws_to_tcp(),
        tcp_to_ws(),
        return_exceptions=True
    )


# --- Entry Point ---
if __name__ == "__main__":
    logger.info(f"[PROXY] WebSocket listening on port: {WS_PORT}")
    uvicorn.run(
        "proxy_server:app",
        host="0.0.0.0",
        port=WS_PORT,
        log_level="info",
        ws_ping_interval=30,
        ws_ping_timeout=10,
    )
