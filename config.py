import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Redis настройки
    REDIS_URL: str = "redis://localhost:6379"
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0
    REDIS_KEY_PREFIX: str = "video_task:"
    
    # Пути
    TEMP_DIR: str = "./temp"
    OUTPUT_DIR: str = "./output"
    SCRIPT_PATH: str = "./process_all.sh"
    
    # Время хранения файлов (в часах)
    FILE_RETENTION_HOURS: int = 1
    
    # API ключ для авторизации
    API_KEY: str = ""
    
    # Webhook URL для уведомлений
    WEBHOOK_URL: str = ""
    
    class Config:
        env_file = ".env"
        case_sensitive = True

config = Settings()