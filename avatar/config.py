"""Environment-only cloud profile; paid operations remain disabled by default."""
from dataclasses import dataclass
import os
import re
from urllib.parse import urlsplit
from cloud.security import validate_token


@dataclass(frozen=True, repr=False)
class Settings:
    enabled: bool = False
    dispatch_isolation_confirmed: bool = False
    owner_key: str = ''
    livekit_url: str = ''
    livekit_api_key: str = ''
    livekit_api_secret: str = ''
    tavus_key: str = ''
    face_id: str = ''
    elevenlabs_key: str = ''
    voice_id: str = ''
    tts_model: str = 'eleven_v4_turbo'
    stt_model: str = 'scribe_v2_realtime'
    duration: int = 100

    def __post_init__(self):
        validate_token(self.owner_key)
        if type(self.duration) is not int or not 1 <= self.duration <= 100:
            raise ValueError('invalid_duration_budget')
        if self.enabled:
            url = urlsplit(self.livekit_url)
            if (url.scheme != 'wss' or not url.hostname or not url.hostname.endswith('.livekit.cloud')
                    or url.username or url.password or url.path not in {'','/'} or url.query or url.fragment):
                raise ValueError('invalid_livekit_url')
            for value in [self.livekit_api_key,self.livekit_api_secret,self.tavus_key,self.elevenlabs_key]:
                if not re.fullmatch(r'[!-~]{16,512}',value): raise ValueError('provider_configuration_required')
            for value in [self.face_id,self.voice_id]:
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value): raise ValueError('provider_identity_required')


def load_settings():
    return Settings(enabled=os.environ.get('AVATAR_ENABLE_PAID','false') == 'true',
        dispatch_isolation_confirmed=os.environ.get('AVATAR_ISOLATION_CONFIRMED','false') == 'true',
        owner_key=os.environ['AGENT_API_TOKEN'],livekit_url=os.environ.get('LIVEKIT_URL',''),
        livekit_api_key=os.environ.get('LIVEKIT_API_KEY',''),livekit_api_secret=os.environ.get('LIVEKIT_API_SECRET',''),
        tavus_key=os.environ.get('TAVUS_API_KEY',''),face_id=os.environ.get('TAVUS_FACE_ID',''),
        elevenlabs_key=os.environ.get('ELEVENLABS_API_KEY',''),voice_id=os.environ.get('ELEVENLABS_VOICE_ID',''),
        duration=int(os.environ.get('AVATAR_MAX_SECONDS','100')))
