"""
Nova Sonic Client — simple and clean.
No history, no automatic reconnection.
Session expires after 8 minutes or 55s inactivity — user must refresh.
"""

import asyncio
import base64
import json
import uuid
import logging
from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient, InvokeModelWithBidirectionalStreamOperationInput
from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart
from aws_sdk_bedrock_runtime.config import Config, HTTPAuthSchemeResolver, SigV4AuthScheme
from smithy_aws_core.identity import EnvironmentCredentialsResolver

logger = logging.getLogger(__name__)

MODEL_ID = "amazon.nova-2-sonic-v1:0"
REGION = "us-east-1"
VOICE_ID = "lupe"  # Native Spanish (es-US) feminine voice

SYSTEM_PROMPT = (
    "Eres DatIA, una experta en gobierno de datos y gobierno de inteligencia artificial. "
    "Tu conocimiento abarca tanto el gobierno de datos — con base en COBIT 2019, DAMA-DMBOK segunda edicion "
    "y el libro Data Governance: The Definitive Guide — como el gobierno de IA, incluyendo marcos de etica, "
    "responsabilidad, transparencia, gestion de riesgos de modelos, y regulaciones emergentes como el AI Act europeo. "
    "Cuando el usuario inicia la conversacion, preguntale brevemente sobre que tema especifico necesita ayuda. "
    "Haz una pregunta a la vez para entender su contexto antes de aconsejar. "
    "Responde siempre en espanol. "
    "Se concisa: respuestas cortas de dos o tres oraciones para conversacion de voz. "
    "No uses listas ni bullets, habla de forma natural como en una conversacion. "
    "Mantente en tu rol de experta en gobierno de datos y gobierno de IA."
)

# 2048 bytes of silence (1024 frames * 2 bytes) — sent every 5s to prevent 55s timeout
SILENCE_B64 = base64.b64encode(bytes(1024 * 2)).decode()


class NovaSonicClient:

    def __init__(self):
        self.model_id = MODEL_ID
        self.region = REGION
        self.client = None
        self.stream = None
        self.response_task = None
        self.silence_task = None
        self.is_active = False
        self.mic_active = False
        self._sending_text = False

        self.prompt_name = None
        self.content_name = None
        self.audio_content_name = None

        self.role = None
        self.display_assistant_text = False
        self._is_final = False

        self.on_audio_output = None
        self.on_text_output = None
        self.on_reconnected = None

    def _initialize_client(self):
        # In ECS Fargate, credentials come from the Task Role via the container
        # metadata endpoint. These credentials are temporary and expire after a few hours.
        # We must refresh them on each session to avoid using stale credentials.
        # The experimental SDK only supports EnvironmentCredentialsResolver, so we use
        # boto3 to fetch fresh credentials and inject them into the environment.
        import os
        import boto3
        
        # Always refresh credentials from boto3 (it handles ECS Task Role automatically)
        try:
            session = boto3.Session()
            creds = session.get_credentials()
            if creds:
                frozen = creds.get_frozen_credentials()
                os.environ['AWS_ACCESS_KEY_ID'] = frozen.access_key
                os.environ['AWS_SECRET_ACCESS_KEY'] = frozen.secret_key
                if frozen.token:
                    os.environ['AWS_SESSION_TOKEN'] = frozen.token
                logger.info("Credentials refreshed from boto3 session (ECS Task Role)")
        except Exception as e:
            logger.warning(f"Could not refresh credentials via boto3: {e}")

        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            auth_scheme_resolver=HTTPAuthSchemeResolver(),
            auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="bedrock")}
        )
        self.client = BedrockRuntimeClient(config=config)
        logger.info("Bedrock client initialized")

    async def _send(self, event_dict: dict):
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=json.dumps(event_dict).encode())
        )
        await self.stream.input_stream.send(chunk)

    async def start_session(self):
        # Always reinitialize client to get fresh credentials
        self._initialize_client()

        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.mic_active = False
        self._sending_text = False
        self.role = None
        self.display_assistant_text = False
        self._is_final = False

        self.stream = await self.client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)
        )
        self.is_active = True
        logger.info("Stream opened")

        await self._send({"event": {"sessionStart": {
            "inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7},
            "turnDetectionConfiguration": {"endpointingSensitivity": "HIGH"}
        }}})

        await self._send({"event": {"promptStart": {
            "promptName": self.prompt_name,
            "textOutputConfiguration": {"mediaType": "text/plain"},
            "audioOutputConfiguration": {
                "mediaType": "audio/lpcm", "sampleRateHertz": 24000,
                "sampleSizeBits": 16, "channelCount": 1,
                "voiceId": VOICE_ID, "encoding": "base64", "audioType": "SPEECH"
            }
        }}})

        await self._send({"event": {"contentStart": {
            "promptName": self.prompt_name, "contentName": self.content_name,
            "type": "TEXT", "interactive": False, "role": "SYSTEM",
            "textInputConfiguration": {"mediaType": "text/plain"}
        }}})
        await self._send({"event": {"textInput": {
            "promptName": self.prompt_name, "contentName": self.content_name,
            "content": SYSTEM_PROMPT
        }}})
        await self._send({"event": {"contentEnd": {
            "promptName": self.prompt_name, "contentName": self.content_name
        }}})

        # Open audio block once — stays open for the whole session
        await self._send({"event": {"contentStart": {
            "promptName": self.prompt_name,
            "contentName": self.audio_content_name,
            "type": "AUDIO", "interactive": True, "role": "USER",
            "audioInputConfiguration": {
                "mediaType": "audio/lpcm", "sampleRateHertz": 16000,
                "sampleSizeBits": 16, "channelCount": 1,
                "audioType": "SPEECH", "encoding": "base64"
            }
        }}})

        self.response_task = asyncio.create_task(self._process_responses())
        self.silence_task = asyncio.create_task(self._stream_silence())
        logger.info("Session ready")

    async def end_session(self):
        if not self.is_active:
            return
        self.is_active = False
        if self.silence_task and not self.silence_task.done():
            self.silence_task.cancel()
        try:
            await self._send({"event": {"contentEnd": {
                "promptName": self.prompt_name, "contentName": self.audio_content_name
            }}})
            await self._send({"event": {"promptEnd": {"promptName": self.prompt_name}}})
            await self._send({"event": {"sessionEnd": {}}})
            await self.stream.input_stream.close()
        except Exception as e:
            logger.warning(f"Close error: {e}")
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
        logger.info("Session ended")

    async def _stream_silence(self):
        """Send silence every 5s to keep the audio block alive (prevents 55s timeout)."""
        await asyncio.sleep(3)  # Wait for stream to stabilize
        try:
            while self.is_active:
                if not self.mic_active and not self._sending_text:
                    await self._send({"event": {"audioInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "content": SILENCE_B64
                    }}})
                    logger.debug("Silence sent to keep stream alive")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.is_active:
                logger.warning(f"Silence error: {e}")

    def start_mic(self):
        self.mic_active = True
        logger.info("Mic active")

    def stop_mic(self):
        self.mic_active = False
        logger.info("Mic inactive")

    async def send_audio_chunk(self, audio_bytes: bytes):
        if not self.is_active:
            return
        await self._send({"event": {"audioInput": {
            "promptName": self.prompt_name,
            "contentName": self.audio_content_name,
            "content": base64.b64encode(audio_bytes).decode()
        }}})

    async def send_text_message(self, text: str):
        if not self.is_active:
            return
        self._sending_text = True
        try:
            cid = str(uuid.uuid4())
            await self._send({"event": {"contentStart": {
                "promptName": self.prompt_name, "contentName": cid,
                "role": "USER", "type": "TEXT", "interactive": True,
                "textInputConfiguration": {"mediaType": "text/plain"}
            }}})
            await self._send({"event": {"textInput": {
                "promptName": self.prompt_name, "contentName": cid, "content": text
            }}})
            await self._send({"event": {"contentEnd": {
                "promptName": self.prompt_name, "contentName": cid
            }}})
            logger.info(f"Text sent: {text[:60]}")
        finally:
            self._sending_text = False

    async def _process_responses(self):
        try:
            while self.is_active:
                output = await self.stream.await_output()
                result = await output[1].receive()
                if not (result.value and result.value.bytes_):
                    continue

                data = json.loads(result.value.bytes_.decode())
                event = data.get('event', {})
                
                # Log all events for debugging
                event_type = list(event.keys())[0] if event else 'unknown'
                if event_type not in ['audioOutput']:  # Don't spam audio logs
                    logger.info(f"Event received: {event_type}")
                
                # Check for error events
                if 'error' in event:
                    logger.error(f"Error event from Nova Sonic: {event['error']}")

                if 'contentStart' in event:
                    cs = event['contentStart']
                    self.role = cs.get('role')
                    additional = cs.get('additionalModelFields')
                    if additional:
                        stage = json.loads(additional).get('generationStage', '')
                        self.display_assistant_text = (stage == 'SPECULATIVE')
                        self._is_final = (stage == 'FINAL')
                    else:
                        self.display_assistant_text = False
                        self._is_final = False

                elif 'textOutput' in event:
                    text = event['textOutput'].get('content', '')
                    if text and self.on_text_output:
                        if self.role == 'ASSISTANT' and self.display_assistant_text:
                            await self.on_text_output('assistant', text)
                        elif self.role == 'USER':
                            await self.on_text_output('user', text)

                elif 'audioOutput' in event:
                    audio_b64 = event['audioOutput'].get('content', '')
                    if self.on_audio_output and audio_b64:
                        await self.on_audio_output(audio_b64)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.is_active:
                logger.error(f"Session error (reconnecting): {e}")
                asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        """Reconnect with a clean session — no history injected."""
        logger.info("Reconnecting with clean session...")
        self.is_active = False
        if self.silence_task and not self.silence_task.done():
            self.silence_task.cancel()
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
        try:
            await self.stream.input_stream.close()
        except Exception:
            pass
        await asyncio.sleep(1)
        try:
            await self.start_session()
            if self.on_reconnected:
                await self.on_reconnected()
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
