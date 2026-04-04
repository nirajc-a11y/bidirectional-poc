"""Sarvam AI Bulbul v3 TTS plugin for LiveKit Agents v1.5.x.

Bulbul v3 is optimized for telephony and Indian English.
Top-rated voices: priya, amelia, sophia, niharika, kavya, ishita
"""
import base64
import os
import struct

import aiohttp
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, tts, utils

# Recommended voices by use case
VOICES = {
    # Female voices - natural Indian English
    "priya": "Professional, warm female voice - best for telephony",
    "amelia": "Clear, confident female voice - good for English",
    "sophia": "Friendly, approachable female voice",
    "kavya": "Soft, pleasant female voice",
    "ishita": "Energetic, clear female voice",
    "niharika": "Natural, calm female voice",
    "shreya": "Warm, professional female voice",
    # Male voices
    "aditya": "Professional male voice",
    "rahul": "Clear, friendly male voice",
    "varun": "Deep, confident male voice",
}


class SarvamTTS(tts.TTS):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "bulbul:v3",
        speaker: str = "priya",
        target_language_code: str = "en-IN",
        pace: float = 1.0,
        sample_rate: int = 8000,  # 8kHz for telephony (bulbul v3 optimized)
    ):
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._api_key = api_key or os.getenv("SARVAM_API_KEY", "")
        self._model = model
        self._speaker = speaker
        self._lang = target_language_code
        self._pace = pace
        self._sample_rate = sample_rate
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def synthesize(self, text: str, *, conn_options=None) -> "SarvamChunkedStream":
        return SarvamChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS,
            api_key=self._api_key,
            model=self._model,
            speaker=self._speaker,
            lang=self._lang,
            pace=self._pace,
            target_sample_rate=self._sample_rate,
            session=self._ensure_session(),
        )

    async def aclose(self):
        if self._session and not self._session.closed:
            await self._session.close()


class SarvamChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: SarvamTTS,
        input_text: str,
        conn_options,
        api_key: str,
        model: str,
        speaker: str,
        lang: str,
        pace: float,
        target_sample_rate: int,
        session: aiohttp.ClientSession,
    ):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._api_key = api_key
        self._model = model
        self._speaker = speaker
        self._lang = lang
        self._pace = pace
        self._target_sample_rate = target_sample_rate
        self._http_session = session

    async def _run(self, output_emitter: tts.AudioEmitter):
        url = "https://api.sarvam.ai/text-to-speech"
        headers = {
            "api-subscription-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": [self._input_text],
            "target_language_code": self._lang,
            "speaker": self._speaker,
            "model": self._model,
            "pace": self._pace,
            "enable_preprocessing": True,
        }

        async with self._http_session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"Sarvam TTS error {resp.status}: {error_text}")

            data = await resp.json()
            audios = data.get("audios", [])
            if not audios:
                return

            audio_bytes = base64.b64decode(audios[0])

            # Parse WAV header for sample rate + extract PCM
            wav_sample_rate = self._target_sample_rate
            if audio_bytes[:4] == b"RIFF":
                wav_sample_rate = struct.unpack("<I", audio_bytes[24:28])[0]
                pos = 12
                pcm_data = audio_bytes[44:]
                while pos < len(audio_bytes) - 8:
                    chunk_id = audio_bytes[pos : pos + 4]
                    chunk_size = struct.unpack("<I", audio_bytes[pos + 4 : pos + 8])[0]
                    if chunk_id == b"data":
                        pcm_data = audio_bytes[pos + 8 : pos + 8 + chunk_size]
                        break
                    pos += 8 + chunk_size
            else:
                pcm_data = audio_bytes

            output_emitter.initialize(
                request_id=utils.shortuuid(),
                sample_rate=wav_sample_rate,
                num_channels=1,
                mime_type="audio/pcm",
            )
            output_emitter.push(pcm_data)
