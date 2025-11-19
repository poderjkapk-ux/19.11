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

# --- ОНОВЛЕНИЙ ШАБЛОН HTML ФОРМИ ---
# Додано секцію "Підвал" та нові поля кольорів
ADMIN_DESIGN_SETTINGS_BODY = """
<div class="card">
    <form action="/admin/design_settings" method="post">
        <h2><i class="fa-solid fa-file-signature"></i> Назви та SEO</h2>
        
        <label for="site_title">Назва сайту/закладу:</label>
        <input type="text" id="site_title" name="site_title" value="{site_title}" placeholder="Назва, що відображається на сайті та в адмін-панелі">
        
        <label for="seo_description">SEO Опис (Description):</label>
        <textarea id="seo_description" name="seo_description" rows="3" placeholder="Короткий опис для пошукових систем (до 160 символів)">{seo_description}</textarea>
        
        <label for="seo_keywords">SEO Ключові слова (Keywords):</label>
        <input type="text" id="seo_keywords" name="seo_keywords" value="{seo_keywords}" placeholder="Наприклад: доставка їжі, ресторан, назва">

        <h2 style="margin-top: 2rem;"><i class="fa-solid fa-palette"></i> Дизайн та Кольори</h2>
        
        <div class="form-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 15px;">
            <div>
                <label for="primary_color">Основний колір (Акцент):</label>
                <input type="color" id="primary_color" name="primary_color" value="{primary_color}" style="width: 100%; height: 40px;">
            </div>
            <div>
                <label for="secondary_color">Додатковий колір:</label>
                <input type="color" id="secondary_color" name="secondary_color" value="{secondary_color}" style="width: 100%; height: 40px;">
            </div>
            <div>
                <label for="background_color">Колір фону сторінки:</label>
                <input type="color" id="background_color" name="background_color" value="{background_color}" style="width: 100%; height: 40px;">
            </div>
            <div>
                <label for="text_color">Колір основного тексту:</label>
                <input type="color" id="text_color" name="text_color" value="{text_color}" style="width: 100%; height: 40px;">
            </div>
            <div>
                <label for="footer_bg_color">Фон підвалу (Footer):</label>
                <input type="color" id="footer_bg_color" name="footer_bg_color" value="{footer_bg_color}" style="width: 100%; height: 40px;">
            </div>
            <div>
                <label for="footer_text_color">Текст підвалу:</label>
                <input type="color" id="footer_text_color" name="footer_text_color" value="{footer_text_color}" style="width: 100%; height: 40px;">
            </div>
        </div>
        
        <div style="margin-top: 1rem;">
            <label for="font_family_sans">Основний шрифт (Без засічок):</label>
            <select id="font_family_sans" name="font_family_sans">
                {font_options_sans}
            </select>
            
            <label for="font_family_serif">Шрифт заголовків (Із засічками):</label>
            <select id="font_family_serif" name="font_family_serif">
                {font_options_serif}
            </select>
        </div>

        <h2 style="margin-top: 2rem;"><i class="fa-solid fa-circle-info"></i> Підвал сайту (Контакти)</h2>
        <div class="form-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
            <div>
                <label for="footer_address"><i class="fa-solid fa-location-dot"></i> Адреса:</label>
                <input type="text" id="footer_address" name="footer_address" value="{footer_address}" placeholder="вул. Прикладна, 10">
            </div>
            <div>
                <label for="footer_phone"><i class="fa-solid fa-phone"></i> Телефон:</label>
                <input type="text" id="footer_phone" name="footer_phone" value="{footer_phone}" placeholder="+380 XX XXX XX XX">
            </div>
            <div>
                <label for="working_hours"><i class="fa-solid fa-clock"></i> Час роботи:</label>
                <input type="text" id="working_hours" name="working_hours" value="{working_hours}" placeholder="Пн-Нд: 10:00 - 22:00">
            </div>
        </div>
        <div class="form-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 10px;">
            <div>
                <label for="instagram_url"><i class="fa-brands fa-instagram"></i> Instagram (посилання):</label>
                <input type="text" id="instagram_url" name="instagram_url" value="{instagram_url}" placeholder="https://instagram.com/...">
            </div>
            <div>
                <label for="facebook_url"><i class="fa-brands fa-facebook"></i> Facebook (посилання):</label>
                <input type="text" id="facebook_url" name="facebook_url" value="{facebook_url}" placeholder="https://facebook.com/...">
            </div>
        </div>
        
        <h2 style="margin-top: 2rem;"><i class="fa-brands fa-telegram"></i> Тексти Telegram-бота</h2>
        
        <label for="telegram_welcome_message">Привітальне повідомлення (Клієнт-бот):</label>
        <textarea id="telegram_welcome_message" name="telegram_welcome_message" rows="5" placeholder="Текст, який бачить користувач при старті бота.">{telegram_welcome_message}</textarea>
        <p style="font-size: 0.8rem; margin-top: -0.5rem; margin-bottom: 1rem;">Використовуйте <code>{{user_name}}</code>, щоб вставити ім'я користувача.</p>

        <div style="margin-top: 2rem;">
            <button type="submit">Зберегти налаштування</button>
        </div>
    </form>
</div>
"""

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
            options_html += f'<option value="{html.escape(font)}" {is_selected}>{html.escape(font)} {is_default}</option>\n'
        return options_html
    # -----------------------------------------------------

    font_options_sans = get_font_options(FONT_FAMILIES_SANS, settings.font_family_sans, DEFAULT_FONT_SANS)
    font_options_serif = get_font_options(FONT_FAMILIES_SERIF, settings.font_family_serif, DEFAULT_FONT_SERIF)

    body = ADMIN_DESIGN_SETTINGS_BODY.format(
        site_title=html.escape(settings.site_title or "Назва"),
        seo_description=html.escape(settings.seo_description or ""),
        seo_keywords=html.escape(settings.seo_keywords or ""),
        
        # --- Кольори (безпечні значення за замовчуванням) ---
        primary_color=settings.primary_color or "#5a5a5a",
        secondary_color=settings.secondary_color or "#eeeeee",
        background_color=settings.background_color or "#f4f4f4",
        text_color=settings.text_color or "#333333",
        footer_bg_color=settings.footer_bg_color or "#333333",
        footer_text_color=settings.footer_text_color or "#ffffff",
        # -------------------------------

        # --- Шрифти (HTML опції) ---
        font_options_sans=font_options_sans,
        font_options_serif=font_options_serif,
        # ---------------------------

        # --- Контакти (Підвал) ---
        footer_address=html.escape(settings.footer_address or ""),
        footer_phone=html.escape(settings.footer_phone or ""),
        working_hours=html.escape(settings.working_hours or ""),
        instagram_url=html.escape(settings.instagram_url or ""),
        facebook_url=html.escape(settings.facebook_url or ""),
        # -------------------------

        telegram_welcome_message=html.escape(settings.telegram_welcome_message or "Шановний {user_name}, ласкаво просимо! 👋\n\nМи раді вас бачити. Оберіть опцію:"),
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
    text_color: str = Form("#333333"),
    footer_bg_color: str = Form("#333333"),
    footer_text_color: str = Form("#ffffff"),
    # -------------------------------

    # --- Поля підвалу ---
    footer_address: str = Form(""),
    footer_phone: str = Form(""),
    working_hours: str = Form(""),
    instagram_url: str = Form(""),
    facebook_url: str = Form(""),
    # --------------------

    font_family_sans: str = Form(...),
    font_family_serif: str = Form(...),
    telegram_welcome_message: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Зберігає налаштування дизайну, SEO, контактів та текстів."""
    settings = await session.get(Settings, 1)
    if not settings:
        settings = Settings(id=1)
        session.add(settings)

    settings.site_title = site_title
    settings.seo_description = seo_description
    settings.seo_keywords = seo_keywords
    
    # --- Збереження кольорів ---
    settings.primary_color = primary_color
    settings.secondary_color = secondary_color
    settings.background_color = background_color
    settings.text_color = text_color
    settings.footer_bg_color = footer_bg_color
    settings.footer_text_color = footer_text_color
    # --------------------------------

    # --- Збереження контактів ---
    settings.footer_address = footer_address
    settings.footer_phone = footer_phone
    settings.working_hours = working_hours
    settings.instagram_url = instagram_url
    settings.facebook_url = facebook_url
    # ----------------------------

    settings.font_family_sans = font_family_sans
    settings.font_family_serif = font_family_serif
    settings.telegram_welcome_message = telegram_welcome_message

    await session.commit()
    
    return RedirectResponse(url="/admin/design_settings?saved=true", status_code=303)