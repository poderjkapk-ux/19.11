# dependencies.py
import secrets
import os  # <-- Імпорт 'os'
import logging  # <-- Імпорт 'logging'
from typing import Generator
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from models import async_session_maker

security = HTTPBasic()

# --- Функція check_credentials оновлена ---
def check_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """Перевіряє облікові дані адміністратора зі змінних оточення."""
    
    # Отримуємо еталонні логін/пароль з .env
    # 'admin' буде логіном за замовчуванням, якщо в .env не заданий ADMIN_USER
    env_user = os.environ.get("ADMIN_USER", "admin")
    env_pass = os.environ.get("ADMIN_PASS") # Пароля за замовчуванням НЕМАЄ!
    
    if not env_pass:
        # Якщо пароль не заданий в .env, ніхто не зможе увійти.
        # Це критична помилка конфігурації.
        logging.error("КРИТИЧНА ПОМИЛКА: ADMIN_PASS не встановлено у змінних оточення!")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Система не налаштована (відсутній пароль адміністратора)",
        )

    is_user_ok = secrets.compare_digest(credentials.username, env_user)
    is_pass_ok = secrets.compare_digest(credentials.password, env_pass)
    
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неправильний логін або пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
# --- КІНЕЦЬ check_credentials ---


async def get_db_session() -> Generator[AsyncSession, None, None]:
    """Створює та надає сесію бази даних для ендпоінта."""
    async with async_session_maker() as session:
        yield session