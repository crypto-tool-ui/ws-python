import os
import asyncio
import logging
import base64
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


def decode_target(encoded: str):
    decoded = base64.b64decode(encoded).decode("utf-8")
    parts = decoded.rsplit(":", 1)
    if len(parts) != 2:
        raise ValueError("Invalid target format")
    host, port_str = parts
    port = int(port_str)
    if not (0 < port <= 65535):
        raise ValueError("Invalid port")
    return host, port


# ─────────────────────────────────────────────
# Pure ASGI app — không dùng FastAPI/Starlette
# để tránh bất kỳ middleware nào can thiệp vào
# WebSocket Upgrade headers.
# ─────────────────────────────────────────────
class ProxyApp:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            await self._handle_http(scope, send)
        elif scope["type"] == "websocket":
            await self._handle_ws(scope, receive, send)

    # ── HTTP: trả 200 cho mọi path ──
    async def _handle_http(self, scope, send):
        path = scope.get("path", "/")
        logger.info(f"[HTTP] {scope['method']} {path}")
        body = b"MCP SERVER READY !!!\n"
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                [b"content-type", b"text/plain"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body})

    # ── WebSocket → TCP bridge ──
    async def _handle_ws(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        client = scope.get("client", ("unknown", 0))
        client_ip = client[0]

        # Decode mining pool target
        try:
            host, port = decode_target(FIXED_TARGET)
        except Exception as e:
            logger.error(f"[ERROR] decode target: {e}")
            await send({"type": "websocket.close", "code": 1008})
            return

        # Chấp nhận WebSocket — gửi accept NGAY, trước mọi thứ khác
        # để Databricks proxy không kịp redirect
        subproto = headers.get(b"sec-websocket-protocol", b"").decode()
        first_proto = subproto.split(",")[0].strip() if subproto else None

        accept_msg = {"type": "websocket.accept"}
        if first_proto:
            accept_msg["subprotocol"] = first_proto
        await send(accept_msg)

        logger.info(f"[WS] {client_ip} accepted, connecting -> {host}:{port}")

        # Mở TCP tới mining pool
        try:
            reader, writer = await asyncio.open_connection(host, port)
            logger.info(f"[TCP] Connected -> {host}:{port}")
        except Exception as e:
            logger.error(f"[TCP ERROR] {host}:{port}: {e}")
            await send({"type": "websocket.close", "code": 1011})
            return

        stop = asyncio.Event()

        async def cleanup():
            if stop.is_set():
                return
            stop.set()
            try:
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                pass

        async def ws_to_tcp():
            try:
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(receive(), timeout=60)
                    except asyncio.TimeoutError:
                        continue

                    mtype = msg.get("type")
                    if mtype == "websocket.disconnect":
                        logger.info(f"[WS] {client_ip} disconnected")
                        break
                    if mtype != "websocket.receive":
                        continue

                    data = msg.get("text") or (
                        msg.get("bytes", b"").decode("utf-8", errors="replace")
                    )
                    if not data:
                        continue
                    if len(data.encode()) > MAX_PAYLOAD:
                        logger.warning("[WS->TCP] payload too large, dropped")
                        continue

                    line = data if data.endswith("\n") else data + "\n"
                    writer.write(line.encode("utf-8"))
                    await writer.drain()
            except Exception as e:
                logger.error(f"[WS->TCP] {e}")
            finally:
                await cleanup()

        async def tcp_to_ws():
            try:
                while not stop.is_set():
                    data = await reader.read(4096)
                    if not data:
                        logger.info(f"[TCP] {host}:{port} closed by remote")
                        break
                    text = data.decode("utf-8", errors="replace")
                    await send({"type": "websocket.send", "text": text})
            except Exception as e:
                logger.error(f"[TCP->WS] {e}")
            finally:
                await cleanup()
                # Báo WS đóng nếu TCP đóng trước
                try:
                    await send({"type": "websocket.close", "code": 1000})
                except Exception:
                    pass

        await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)


app = ProxyApp()

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
