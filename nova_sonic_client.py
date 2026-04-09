"""
Nova Sonic Client — Bidirectional streaming with Amazon Nova 2 Sonic.

Supports both voice and text input (cross-modal).

Key requirements from AWS documentation:
- Cross-modal input requires continuous audio streaming
- Session timeout: 55 seconds of inactivity OR 8 minutes max duration
- Silence must be sent continuously when mic is not active
- Text input uses interactive=True with unique contentName per message
"""

import asyncio
import base64
import json
import uuid
import logging
import os
from pathlib import Path
from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient, InvokeModelWithBidirectionalStreamOperationInput
from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart
from aws_sdk_bedrock_runtime.config import Config, HTTPAuthSchemeResolver, SigV4AuthScheme
from smithy_aws_core.identity import EnvironmentCredentialsResolver

logger = logging.getLogger(__name__)

MODEL_ID = "amazon.nova-2-sonic-v1:0"
REGION = "us-east-1"
VOICE_ID = "lupe"  # Native Spanish (es-US) feminine voice

# Load system prompt from file
def _load_system_prompt() -> str:
    """Load system prompt from prompts/PromptDatIA_v2.md file."""
    # Try multiple paths (for local dev and Docker container)
    possible_paths = [
        Path(__file__).parent / "prompts" / "PromptDatIA_v2.md",
        Path("/app/prompts/PromptDatIA_v2.md"),
        Path("prompts/PromptDatIA_v2.md"),
    ]
    
    for prompt_path in possible_paths:
        if prompt_path.exists():
            content = prompt_path.read_text(encoding="utf-8")
            logger.info(f"System prompt loaded from {prompt_path}")
            return content
    
    # Fallback if file not found
    logger.warning("Prompt file not found, using fallback prompt")
    return (
        "Eres DatIA, una experta en gobierno de datos y gobierno de inteligencia artificial. "
        "Responde siempre en español. Se concisa."
    )

SYSTEM_PROMPT = _load_system_prompt()

# Silence chunk: 1024 frames * 2 bytes = 2048 bytes of silence
# At 16kHz, this is ~64ms of audio
SILENCE_CHUNK = bytes(1024 * 2)
SILENCE_B64 = base64.b64encode(SILENCE_CHUNK).decode()

# Silence interval in seconds - send frequently to keep stream alive
SILENCE_INTERVAL = 0.05  # 50ms - AWS recommends continuous streaming


class NovaSonicClient:
    """Client for bidirectional streaming with Amazon Nova 2 Sonic."""

    def __init__(self):
        self.model_id = MODEL_ID
        self.region = REGION
        self.client = None
        self.stream = None
        self.response_task = None
        self.silence_task = None
        self.is_active = False
        self.mic_active = False

        # Session identifiers
        self.prompt_name = None
        self.content_name = None  # For system prompt
        self.audio_content_name = None  # For audio stream

        # Response tracking
        self.role = None
        self.display_assistant_text = False

        # Callbacks
        self.on_audio_output = None
        self.on_text_output = None
        self.on_reconnected = None

        # Lock for thread-safe sending
        self._send_lock = asyncio.Lock()

    def _initialize_client(self):
        """Initialize the Bedrock client with fresh credentials."""
        import os
        import boto3
        
        # Refresh credentials from boto3 (handles ECS Task Role automatically)
        try:
            session = boto3.Session()
            creds = session.get_credentials()
            if creds:
                frozen = creds.get_frozen_credentials()
                os.environ['AWS_ACCESS_KEY_ID'] = frozen.access_key
                os.environ['AWS_SECRET_ACCESS_KEY'] = frozen.secret_key
                if frozen.token:
                    os.environ['AWS_SESSION_TOKEN'] = frozen.token
                logger.info("Credentials refreshed from boto3 session")
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
        """Send an event to the stream (thread-safe)."""
        if not self.stream or not self.is_active:
            return
        async with self._send_lock:
            try:
                chunk = InvokeModelWithBidirectionalStreamInputChunk(
                    value=BidirectionalInputPayloadPart(bytes_=json.dumps(event_dict).encode())
                )
                await self.stream.input_stream.send(chunk)
            except Exception as e:
                if self.is_active:
                    logger.error(f"Error sending event: {e}")
                raise

    async def start_session(self):
        """Start a new Nova Sonic session."""
        self._initialize_client()

        # Generate unique identifiers for this session
        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.mic_active = False
        self.role = None
        self.display_assistant_text = False

        # Open bidirectional stream
        self.stream = await self.client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)
        )
        self.is_active = True
        logger.info("Stream opened")

        # 1. Session Start
        await self._send({"event": {"sessionStart": {
            "inferenceConfiguration": {
                "maxTokens": 1024,
                "topP": 0.9,
                "temperature": 0.7
            },
            "turnDetectionConfiguration": {
                "endpointingSensitivity": "HIGH"
            }
        }}})

        # 2. Prompt Start - configure audio output
        await self._send({"event": {"promptStart": {
            "promptName": self.prompt_name,
            "textOutputConfiguration": {"mediaType": "text/plain"},
            "audioOutputConfiguration": {
                "mediaType": "audio/lpcm",
                "sampleRateHertz": 24000,
                "sampleSizeBits": 16,
                "channelCount": 1,
                "voiceId": VOICE_ID,
                "encoding": "base64",
                "audioType": "SPEECH"
            }
        }}})

        # 3. System Prompt (non-interactive)
        await self._send({"event": {"contentStart": {
            "promptName": self.prompt_name,
            "contentName": self.content_name,
            "type": "TEXT",
            "interactive": False,
            "role": "SYSTEM",
            "textInputConfiguration": {"mediaType": "text/plain"}
        }}})
        await self._send({"event": {"textInput": {
            "promptName": self.prompt_name,
            "contentName": self.content_name,
            "content": SYSTEM_PROMPT
        }}})
        await self._send({"event": {"contentEnd": {
            "promptName": self.prompt_name,
            "contentName": self.content_name
        }}})

        # 4. Open Audio Stream (stays open for entire session)
        # This is required for both voice AND text input (cross-modal)
        await self._send({"event": {"contentStart": {
            "promptName": self.prompt_name,
            "contentName": self.audio_content_name,
            "type": "AUDIO",
            "interactive": True,
            "role": "USER",
            "audioInputConfiguration": {
                "mediaType": "audio/lpcm",
                "sampleRateHertz": 16000,
                "sampleSizeBits": 16,
                "channelCount": 1,
                "audioType": "SPEECH",
                "encoding": "base64"
            }
        }}})

        # Start background tasks
        self.response_task = asyncio.create_task(self._process_responses())
        self.silence_task = asyncio.create_task(self._stream_silence())
        
        logger.info("Session ready - audio stream open")

    async def end_session(self):
        """End the current session and clean up resources."""
        if not self.is_active:
            return
        
        self.is_active = False
        logger.info("Ending session...")

        # Cancel background tasks
        if self.silence_task and not self.silence_task.done():
            self.silence_task.cancel()
            try:
                await self.silence_task
            except asyncio.CancelledError:
                pass

        # Send closing events
        try:
            # Close audio content
            await self._send({"event": {"contentEnd": {
                "promptName": self.prompt_name,
                "contentName": self.audio_content_name
            }}})
            # Close prompt
            await self._send({"event": {"promptEnd": {
                "promptName": self.prompt_name
            }}})
            # End session
            await self._send({"event": {"sessionEnd": {}}})
            # Close stream
            await self.stream.input_stream.close()
        except Exception as e:
            logger.warning(f"Error during session close: {e}")

        # Cancel response task
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
            try:
                await self.response_task
            except asyncio.CancelledError:
                pass

        logger.info("Session ended")

    async def _stream_silence(self):
        """
        Send continuous silence to keep the audio stream alive.
        
        CRITICAL: AWS documentation states that cross-modal input requires
        continuous audio streaming. Without this, the session will timeout
        after 55 seconds of inactivity.
        
        "Cross-modal input requires an active streaming session to function
        properly. The session must maintain continuous streaming like a regular
        voice session, otherwise standard session timeouts will be applied."
        """
        await asyncio.sleep(0.5)  # Brief wait for stream to stabilize
        
        logger.info("Silence streaming started")
        
        try:
            while self.is_active:
                # Only send silence when microphone is NOT active
                # When mic is active, real audio is being sent
                if not self.mic_active:
                    try:
                        await self._send({"event": {"audioInput": {
                            "promptName": self.prompt_name,
                            "contentName": self.audio_content_name,
                            "content": SILENCE_B64
                        }}})
                    except Exception as e:
                        if self.is_active:
                            logger.warning(f"Error sending silence: {e}")
                
                # Sleep briefly - continuous streaming is required
                await asyncio.sleep(SILENCE_INTERVAL)
                
        except asyncio.CancelledError:
            logger.debug("Silence streaming cancelled")
        except Exception as e:
            if self.is_active:
                logger.error(f"Silence streaming error: {e}")

    def start_mic(self):
        """Signal that microphone is active (real audio being sent)."""
        self.mic_active = True
        logger.info("Mic active - pausing silence")

    def stop_mic(self):
        """Signal that microphone is inactive (resume silence)."""
        self.mic_active = False
        logger.info("Mic inactive - resuming silence")

    async def send_audio_chunk(self, audio_bytes: bytes):
        """Send a chunk of audio from the microphone."""
        if not self.is_active:
            return
        await self._send({"event": {"audioInput": {
            "promptName": self.prompt_name,
            "contentName": self.audio_content_name,
            "content": base64.b64encode(audio_bytes).decode()
        }}})

    async def send_text_message(self, text: str):
        """
        Send a text message using cross-modal input.
        
        Per AWS documentation:
        - Text input uses a separate content block with interactive=True
        - Each message needs a unique contentName
        - Audio stream must remain active (silence continues)
        """
        if not self.is_active:
            logger.warning("Cannot send text: session not active")
            return
        if not self.stream:
            logger.warning("Cannot send text: stream is None")
            return

        # Generate unique content name for this text message
        text_content_name = str(uuid.uuid4())
        
        try:
            logger.info(f"Sending text message: {text[:50]}...")
            
            # 1. Content Start for text (interactive=True for cross-modal)
            await self._send({"event": {"contentStart": {
                "promptName": self.prompt_name,
                "contentName": text_content_name,
                "type": "TEXT",
                "interactive": True,
                "role": "USER",
                "textInputConfiguration": {"mediaType": "text/plain"}
            }}})
            
            # 2. Text Input
            await self._send({"event": {"textInput": {
                "promptName": self.prompt_name,
                "contentName": text_content_name,
                "content": text
            }}})
            
            # 3. Content End
            await self._send({"event": {"contentEnd": {
                "promptName": self.prompt_name,
                "contentName": text_content_name
            }}})
            
            logger.info(f"Text message sent successfully")
            
        except Exception as e:
            logger.error(f"Error sending text message: {e}")
            raise

    async def _process_responses(self):
        """Process incoming responses from Nova Sonic."""
        logger.info("Response processor started")
        
        try:
            while self.is_active:
                try:
                    # Wait for output with timeout
                    output = await asyncio.wait_for(
                        self.stream.await_output(),
                        timeout=60.0  # 60 second timeout
                    )
                    result = await output[1].receive()
                    
                    if not (result.value and result.value.bytes_):
                        continue

                    data = json.loads(result.value.bytes_.decode())
                    event = data.get('event', {})
                    
                    # Log non-audio events for debugging
                    event_type = list(event.keys())[0] if event else 'unknown'
                    if event_type != 'audioOutput':
                        logger.info(f"Event: {event_type}")
                    
                    # Handle error events
                    if 'error' in event:
                        logger.error(f"Nova Sonic error: {event['error']}")
                        continue

                    # Handle content start - track role and generation stage
                    if 'contentStart' in event:
                        cs = event['contentStart']
                        self.role = cs.get('role')
                        
                        additional = cs.get('additionalModelFields')
                        if additional:
                            try:
                                stage = json.loads(additional).get('generationStage', '')
                                self.display_assistant_text = (stage == 'SPECULATIVE')
                            except:
                                self.display_assistant_text = False
                        else:
                            self.display_assistant_text = False

                    # Handle text output (transcripts)
                    elif 'textOutput' in event:
                        text = event['textOutput'].get('content', '')
                        if text and self.on_text_output:
                            if self.role == 'ASSISTANT' and self.display_assistant_text:
                                await self.on_text_output('assistant', text)
                            elif self.role == 'USER':
                                await self.on_text_output('user', text)

                    # Handle audio output
                    elif 'audioOutput' in event:
                        audio_b64 = event['audioOutput'].get('content', '')
                        if self.on_audio_output and audio_b64:
                            await self.on_audio_output(audio_b64)

                except asyncio.TimeoutError:
                    # No response in 60s - stream might be stale
                    if self.is_active:
                        logger.warning("No response in 60s - checking stream health")
                    continue

        except asyncio.CancelledError:
            logger.info("Response processor cancelled")
        except Exception as e:
            if self.is_active:
                logger.error(f"Response processor error: {e}", exc_info=True)
                # Attempt reconnection
                asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        """Reconnect with a clean session."""
        logger.info("Attempting reconnection...")
        
        self.is_active = False
        
        # Cancel tasks
        if self.silence_task and not self.silence_task.done():
            self.silence_task.cancel()
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
        
        # Close stream
        try:
            await self.stream.input_stream.close()
        except Exception:
            pass
        
        # Wait before reconnecting
        await asyncio.sleep(1)
        
        # Start new session
        try:
            await self.start_session()
            if self.on_reconnected:
                await self.on_reconnected()
            logger.info("Reconnection successful")
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
