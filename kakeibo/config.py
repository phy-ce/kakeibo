"""Secret (.env) save/load — kept in the user's home, outside the project folder.

The distributed code is left untouched; personal secrets such as API keys are
stored by this module in the user's home (`~/.kakeibo/.env`, on Windows
`%USERPROFILE%\\.kakeibo\\.env`).

- If `FLASK_SECRET_KEY` is missing, it is auto-generated and saved.
- If `GEMINI_API_KEY` is missing, it is entered via the console on first run and saved.
- Gmail credentials (credentials.json) are uploaded from the settings page -> saved to the same folder.

The storage location can be overridden with the `KAKEIBO_HOME` environment variable.
"""
import os
import sys
import secrets as _secrets

from dotenv import load_dotenv, set_key


def user_config_dir() -> str:
    """User folder for storing secrets/settings (default: ~/.kakeibo)."""
    override = os.environ.get("KAKEIBO_HOME")
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.expanduser("~"), ".kakeibo")


def user_env_path() -> str:
    return os.path.join(user_config_dir(), ".env")


def user_path(filename: str) -> str:
    """Path to a file inside the user's home folder."""
    return os.path.join(user_config_dir(), filename)


# No data is kept in the project folder (engine). The files below all live in ~/.kakeibo.
# If an older version created some in the project root, move them to this folder once.
_DATA_FILES = [
    "kakeibo.db", "kakeibo.db-wal", "kakeibo.db-shm",
    "categories.json", "auto_rules.json",
]


def migrate_project_data() -> None:
    """Move data files from the old location (project root) to the user folder (~/.kakeibo) once.

    If a file already exists at the new location, it is left untouched (to prevent overwriting).
    """
    import shutil
    from .paths import app_dir

    root = app_dir()
    dest = user_config_dir()
    if os.path.abspath(root) == os.path.abspath(dest):
        return  # In the edge case where the source and data folders are the same, no move is needed
    os.makedirs(dest, exist_ok=True)
    for name in _DATA_FILES:
        old = os.path.join(root, name)
        new = os.path.join(dest, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                shutil.move(old, new)
                print(f"[migrate] {name} → {dest}")
            except Exception as e:
                print(f"[migrate] {name} 이동 실패: {e}")


def gmail_status() -> dict:
    """Gmail integration status. If token.json exists, it is already authenticated (= connected)."""
    has_cred = os.path.exists(resolve_secret_file("credentials.json"))
    has_token = os.path.exists(resolve_secret_file("token.json"))
    return {
        "has_credentials": has_cred,
        "has_token": has_token,
        "connected": has_token or has_cred,   # True if usable (i.e. not unset)
    }


def resolve_secret_file(filename: str) -> str:
    """Resolve the path of a secret file.

    Prefers the user home (`~/.kakeibo/<filename>`); if absent, looks at the project root (backward compat).
    If neither exists, returns the user home path where it 'should be' (for error messages/creation).
    """
    user_p = user_path(filename)
    if os.path.exists(user_p):
        return user_p
    from .paths import app_dir
    legacy = os.path.join(app_dir(), filename)
    if os.path.exists(legacy):
        return legacy
    return user_p


def _ensure_env_file() -> str:
    d = user_config_dir()
    os.makedirs(d, exist_ok=True)
    path = user_env_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# kakeibo 시크릿 - 프로젝트 폴더 밖에 보관됩니다. 이 파일을 공유하지 마세요.\n")
    return path


def load_env() -> None:
    """Load the env files and fill in missing secrets (the ones that can be auto-generated).

    Being non-interactive, it is safe to call at app import time. Calling it multiple times is fine.
    Priority: project .env -> user .env (override).
    """
    # Backward compat: if a .env exists in the project root, load it first
    load_dotenv()
    env_path = _ensure_env_file()
    load_dotenv(env_path, override=True)

    # FLASK_SECRET_KEY is silently auto-generated/persisted
    if not os.environ.get("FLASK_SECRET_KEY"):
        val = _secrets.token_hex(32)
        set_key(env_path, "FLASK_SECRET_KEY", val)
        os.environ["FLASK_SECRET_KEY"] = val


def _prompt_api_key() -> str:
    print()
    print("=" * 64)
    print("  Gemini API 키가 설정되어 있지 않습니다.")
    print("  영수증 OCR 기능에 필요합니다. (건너뛰려면 그냥 Enter)")
    print("  키 발급(무료): https://aistudio.google.com/app/apikey")
    print("=" * 64)
    try:
        return input("  GEMINI_API_KEY 입력: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def prompt_missing_keys() -> None:
    """First-run interactive setup. Must be called before importing the app.

    (Order matters because the gemini/app modules read env at import time.)
    Gmail credentials are uploaded from the settings page, not the console.
    """
    load_env()
    env_path = user_env_path()

    if not os.environ.get("GEMINI_API_KEY"):
        # Only prompt for input when a console (tty) is available. Otherwise skip silently (only OCR disabled).
        if sys.stdin is not None and sys.stdin.isatty():
            key = _prompt_api_key()
            if key:
                set_key(env_path, "GEMINI_API_KEY", key)
                os.environ["GEMINI_API_KEY"] = key
                print(f"  저장 완료 -> {env_path}\n")
            else:
                print("  건너뜀 - OCR 기능은 키를 넣기 전까지 비활성화됩니다.\n")
