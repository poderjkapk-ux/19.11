# admin_design_settings.py

import html
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import Settings
from templates import ADMIN_HTML_TEMPLATE, ADMIN_DESIGN_SETTINGS_BODY
from dependencies import get_db_session, check_credentials

router = APIRouter()

# --- Словники шрифтів для легкого керування ---
FONT_FAMILIES_SANS = [
    "Golos Text", "Inter", "Roboto", "Open Sans", "Montserrat", "Lato", "Nunito"
]
DEFAULT_FONT_SANS = "Golos Text"

FONT_FAMILIES_SERIF = [
    "Playfair Display", "Lora", "Merriweather", "EB Garamond", "PT Serif", "Cormorant"
]
DEFAULT_FONT_SERIF = "Playfair Display"
# -----------------------------------------------

@router.get("/admin/design_settings", response_class=HTMLResponse)
async def get_design_settings_page(
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Відображає сторінку налаштувань дизайну, SEO та текстів."""
    settings = await session.get(Settings, 1)
    if not settings:
        settings = Settings() # Provide default values if no settings exist

    # --- Функція для генерації HTML <option> для <select> ---
    def get_font_options(font_list: list, selected_font: str, default_font: str) -> str:
        options_html = ""
        current_font = selected_font or default_font
        for font in font_list:
            is_default = "(За замовчуванням)" if font == default_font else ""
            is_selected = "selected" if font == current_font else ""
            # Використовуємо ім'я шрифту як value
            options_html += f'<option value="{html.escape(font)}" {is_selected}>{html.escape(font)} {is_default}</option>\n'
        return options_html
    # -----------------------------------------------------

    body = ADMIN_DESIGN_SETTINGS_BODY.format(
        site_title=settings.site_title or "Назва",
        seo_description=settings.seo_description or "",
        seo_keywords=settings.seo_keywords or "",
        
        # --- Оновлені поля кольорів ---
        primary_color=settings.primary_color or "#5a5a5a",
        secondary_color=settings.secondary_color or "#eeeeee",
        background_color=settings.background_color or "#f4f4f4",
        # -------------------------------

        # --- Динамічна генерація списків шрифтів ---
        **{f"font_select_sans_{font.replace(' ', '_')}": "selected" if (settings.font_family_sans or DEFAULT_FONT_SANS) == font else "" for font in FONT_FAMILIES_SANS},
        **{f"font_select_serif_{font.replace(' ', '_')}": "selected" if (settings.font_family_serif or DEFAULT_FONT_SERIF) == font else "" for font in FONT_FAMILIES_SERIF},
        # ------------------------------------------

        telegram_welcome_message=settings.telegram_welcome_message or "Шановний {user_name}, ласкаво просимо! 👋\n\nМи раді вас бачити. Оберіть опцію:",
    )

    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active"]}
    active_classes["design_active"] = "active"
    
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Дизайн та SEO", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@router.post("/admin/design_settings")
async def save_design_settings(
    site_title: str = Form(...),
    seo_description: str = Form(""),
    seo_keywords: str = Form(""),
    
    # --- Оновлені поля кольорів ---
    primary_color: str = Form(...),
    secondary_color: str = Form(...),
    background_color: str = Form(...),
    # -------------------------------

    font_family_sans: str = Form(...),
    font_family_serif: str = Form(...),
    telegram_welcome_message: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Зберігає налаштування дизайну, SEO та текстів."""
    settings = await session.get(Settings, 1)
    if not settings:
        settings = Settings(id=1)
        session.add(settings)

    settings.site_title = site_title
    settings.seo_description = seo_description
    settings.seo_keywords = seo_keywords
    
    # --- Збереження нових кольорів ---
    settings.primary_color = primary_color
    settings.secondary_color = secondary_color
    settings.background_color = background_color
    # --------------------------------

    settings.font_family_sans = font_family_sans
    settings.font_family_serif = font_family_serif
    settings.telegram_welcome_message = telegram_welcome_message

    await session.commit()
    
    return RedirectResponse(url="/admin/design_settings?saved=true", status_code=303)