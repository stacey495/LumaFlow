from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
MAX_BYTES = 10 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 6_000_000
MAX_SIDE = 5000
TIME_LIMIT = 45
MIN_WORKERS = 2
MAX_WORKERS = 4
MAX_ACTIVE_USER = 6
MAX_ACTIVE_SYSTEM = 16
MAX_HISTORY_USER = 20
MAX_HISTORY_SYSTEM = 80
MAX_USERS = 20
SESSION_SECONDS = 8 * 3600
ACTIVE = ("queued", "running", "cancelling")
TERMINAL = ("completed", "cancelled", "failed")
PROFILES = {
    "light": {"name": "Легкий", "kernel": 3, "passes": 1, "seconds_mp": 0.48},
    "balanced": {"name": "Збалансований", "kernel": 5, "passes": 1, "seconds_mp": 1.45},
    "deep": {"name": "Глибокий", "kernel": 7, "passes": 2, "seconds_mp": 5.1},
}
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
