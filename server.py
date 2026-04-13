"""
DatIA Voice Server — FastAPI WebSocket server that bridges the browser and Amazon Nova 2 Sonic.

Usage:
    1. Export AWS credentials:
       aws configure export-credentials --profile alx-dev --format env
       (copy and export the three variables)

    2. Run the server:
       uvicorn server:app --host 0.0.0.0 --port 8080

    3. Open frontend/index.html in your browser (use HTTPS or localhost)
"""

import asyncio
import base64
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from nova_sonic_client import NovaSonicClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

import os

app = FastAPI(title="DatIA Voice Agent")

# Serve frontend files locally (not needed in production — S3 serves the frontend)
if os.path.isdir("frontend"):
    app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")


@app.get("/")
async def root():
    return {"status": "DatIA Voice Server running", "model": "amazon.nova-2-sonic-v1:0"}


@app.get("/health")
async def health_check():
    """
    Health check endpoint for ECS container health monitoring.
    
    ECS calls this endpoint periodically. If it fails multiple times,
    ECS automatically restarts the container.
    """
    return {"status": "healthy"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint that bridges the browser and Nova Sonic.

    Message protocol (browser → server):
        {"type": "audio_chunk", "data": "<base64_pcm_16khz>"}
        {"type": "start_audio"}
        {"type": "stop_audio"}
        {"type": "end_session"}

    Message protocol (server → browser):
        {"type": "audio_chunk", "data": "<base64_pcm_24khz>"}
        {"type": "transcript", "role": "user"|"assistant", "text": "..."}
        {"type": "session_ready"}
        {"type": "error", "message": "..."}
    """
    await websocket.accept()
    logger.info("Browser connected")

    nova = NovaSonicClient()

    async def on_audio_output(audio_b64: str):
        """Forward Nova Sonic audio chunks to the browser."""
        try:
            await websocket.send_text(json.dumps({
                "type": "audio_chunk",
                "data": audio_b64
            }))
        except Exception as e:
            logger.warning(f"Error sending audio to browser: {e}")

    async def on_text_output(role: str, text: str):
        """Forward transcripts to the browser."""
        try:
            await websocket.send_text(json.dumps({
                "type": "transcript",
                "role": role,
                "text": text
            }))
        except Exception as e:
            logger.warning(f"Error sending transcript to browser: {e}")

    async def on_reconnected():
        """Notify browser that session was restored."""
        try:
            await websocket.send_text(json.dumps({"type": "session_reconnected"}))
            logger.info("Browser notified of reconnection")
        except Exception as e:
            logger.warning(f"Error notifying reconnection: {e}")

    nova.on_audio_output = on_audio_output
    nova.on_text_output = on_text_output
    nova.on_reconnected = on_reconnected

    try:
        # Start Nova Sonic session
        await nova.start_session()
        await websocket.send_text(json.dumps({"type": "session_ready"}))
        logger.info("Nova Sonic session ready")

        # Process messages from browser
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "start_audio":
                nova.start_mic()

            elif msg_type == "audio_chunk":
                audio_bytes = base64.b64decode(msg["data"])
                await nova.send_audio_chunk(audio_bytes)

            elif msg_type == "stop_audio":
                nova.stop_mic()

            elif msg_type == "text_message":
                text = msg.get("text", "").strip()
                if text:
                    logger.info(f"Text message received: {text[:60]}")
                    await nova.send_text_message(text)

            elif msg_type == "end_session":
                await nova.end_session()
                break

    except WebSocketDisconnect:
        logger.info("Browser disconnected")
    except Exception as e:
        logger.error(f"Session error: {e}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        if nova.is_active:
            await nova.end_session()
        logger.info("Session cleaned up")
