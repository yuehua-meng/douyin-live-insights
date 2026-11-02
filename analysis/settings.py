from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass
class Settings:
    recordings: Path
    data: Path
    origin: str = "https://localhost"
    cookie_path: str = "/recorder-admin/"
    secure_cookie: bool = True
    password_hash: str = ""
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    asr_model: str = ""
    vad_model: str = ""
    punc_model: str = ""
    model_version: str = "paraformer-onnx-int8-v1"
    threads: int = 2
    deepseek_model: str = "deepseek-flash"
    daily_budget: int = 1_000_000  # micro USD
    task_budget: int = 200_000
    input_price: float = 0.3  # USD / million tokens; update with provider pricing
    output_price: float = 1.2
    min_free_bytes: int = 5_000_000_000
    daily_batch_mode: bool = False
    daily_process_hour: int = 9
    daily_send_hour: int = 11
    wecom_digest_enabled: bool = False
    wecom_webhook_url: str = field(default='', repr=False)
    wecom_transport: str = 'webhook'
    wecom_bot_id: str = field(default='', repr=False)
    wecom_bot_secret: str = field(default='', repr=False)
    wecom_bind_code: str = field(default='', repr=False)

    @classmethod
    def load(cls):
        data = Path(os.environ.get("ANALYSIS_DATA", "/srv/douyin-recorder/analysis"))
        return cls(
            recordings=Path(os.environ.get("RECORDINGS_DIR", "/srv/douyin-recorder/recordings")),
            data=data, origin=os.environ.get("ADMIN_ORIGIN", "https://localhost"),
            password_hash=os.environ.get("ADMIN_PASSWORD_HASH", ""),
            asr_model=str(data / "models/asr"), vad_model=str(data / "models/vad"),
            punc_model=str(data / "models/punc"),
            deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
            daily_budget=int(os.environ.get("DAILY_BUDGET_MICRO_USD", "1000000")),
            task_budget=int(os.environ.get("TASK_BUDGET_MICRO_USD", "200000")),
            input_price=float(os.environ.get("INPUT_USD_PER_MILLION", "0.3")),
            output_price=float(os.environ.get("OUTPUT_USD_PER_MILLION", "1.2")),
            daily_batch_mode=os.environ.get('DAILY_BATCH_MODE', '0') == '1',
            daily_process_hour=int(os.environ.get('DAILY_PROCESS_HOUR', '9')),
            daily_send_hour=int(os.environ.get('DAILY_SEND_HOUR', '11')),
            wecom_digest_enabled=os.environ.get('WECOM_DIGEST_ENABLED', '0') == '1',
            wecom_webhook_url=os.environ.get('WECOM_WEBHOOK_URL', ''),
            wecom_transport=os.environ.get('WECOM_TRANSPORT', 'webhook'),
            wecom_bot_id=os.environ.get('WECOM_BOT_ID', ''),
            wecom_bot_secret=os.environ.get('WECOM_BOT_SECRET', ''),
            wecom_bind_code=os.environ.get('WECOM_BIND_CODE', ''),
        )
