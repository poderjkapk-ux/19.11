# admin_order_management.py

import html
import logging
import os
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from aiogram import Bot
from urllib.parse import quote_plus
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
import re

from models import Order, OrderStatus, Employee, Role, OrderStatusHistory, Settings, Product
from templates import ADMIN_HTML_TEMPLATE, ADMIN_ORDER_MANAGE_BODY
from dependencies import get_db_session, check_credentials
from notification_manager import notify_all_parties_on_status_change


router = APIRouter()
logger = logging.getLogger(__name__)

async def get_bot_instances(session: AsyncSession) -> tuple[Bot | None, Bot | None]:
    """Допоміжна функція для отримання екземплярів ботів на основі змінних оточення."""
    admin_bot_token = os.environ.get('ADMIN_BOT_TOKEN')
    client_bot_token = os.environ.get('CLIENT_BOT_TOKEN')

    if not all([admin_bot_token, client_bot_token]):
        logging.warning("Токени ботів (ADMIN_BOT_TOKEN/CLIENT_BOT_TOKEN) не налаштовані в .env.")
        return None, None
    
    from aiogram.enums import ParseMode
    from aiogram.client.default import DefaultBotProperties

    admin_bot = Bot(token=admin_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    client_bot = Bot(token=client_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return admin_bot, client_bot

def parse_products_str(products_str: str) -> dict:
    """Парсить рядок продуктів у словник {'Назва': кількість}."""
    if not products_str: return {}
    result = {}
    for part in products_str.split(", "):
        try:
            if " x " in part:
                name, qty = part.rsplit(" x ", 1)
                # strip() видаляє пробіли, що може викликати розбіжність з БД, якщо там вони є
                result[name.strip()] = int(qty)
        except ValueError: continue
    return result

@router.get("/admin/order/manage/{order_id}", response_class=HTMLResponse)
async def get_manage_order_page(
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Відображає сторінку керування для конкретного замовлення."""
    settings = await session.get(Settings, 1) or Settings()
    
    order = await session.get(
        Order,
        order_id,
        options=[
            joinedload(Order.status),
            joinedload(Order.courier),
            joinedload(Order.history).joinedload(OrderStatusHistory.status)
        ]
    )
    if not order:
        raise HTTPException(status_code=404, detail="Замовлення не знайдено")

    # --- Формування списку товарів з іконками цехів ---
    products_map = parse_products_str(order.products)
    products_html_list = []
    
    if products_map:
        product_names = list(products_map.keys())
        # Отримуємо інформацію про цех для кожного товару
        # Шукаємо товари, ігноруючи можливі розбіжності в пробілах
        products_res = await session.execute(select(Product))
        all_products = products_res.scalars().all()
        
        # Створюємо мапу, де ключ - це назва товару БЕЗ пробілів по краях
        db_products = {p.name.strip(): p for p in all_products}

        for name, qty in products_map.items():
            icon = "❓"
            # Шукаємо по "чистій" назві
            if prod := db_products.get(name.strip()):
                if prod.preparation_area == 'kitchen':
                    icon = "🍳" # Кухня
                elif prod.preparation_area == 'bar':
                    icon = "🍹" # Бар
            
            products_html_list.append(f"<li>{icon} {html.escape(name)} x {qty}</li>")
    
    products_html = "<ul>" + "".join(products_html_list) + "</ul>" if products_html_list else "<i>Товарів немає</i>"
    # ---------------------------------------------------

    statuses_res = await session.execute(select(OrderStatus).order_by(OrderStatus.id))
    all_statuses = statuses_res.scalars().all()
    status_options = "".join([f'<option value="{s.id}" {"selected" if s.id == order.status_id else ""}>{html.escape(s.name)}</option>' for s in all_statuses])

    courier_role_res = await session.execute(select(Role.id).where(Role.can_be_assigned == True))
    courier_role_ids = courier_role_res.scalars().all()
    
    couriers_on_shift = []
    if courier_role_ids:
        couriers_res = await session.execute(
            select(Employee)
            .where(Employee.role_id.in_(courier_role_ids), Employee.is_on_shift == True)
            .order_by(Employee.full_name)
        )
        couriers_on_shift = couriers_res.scalars().all()
        
    courier_options = '<option value="0">Не призначено</option>'
    courier_options += "".join([f'<option value="{c.id}" {"selected" if c.id == order.courier_id else ""}>{html.escape(c.full_name)}</option>' for c in couriers_on_shift])

    history_html = "<ul class='status-history'>"
    sorted_history = sorted(order.history, key=lambda h: h.timestamp, reverse=True)
    for entry in sorted_history:
        timestamp = entry.timestamp.strftime('%d.%m.%Y %H:%M')
        history_html += f"<li><b>{entry.status.name}</b> (Ким: {html.escape(entry.actor_info)}) - {timestamp}</li>"
    history_html += "</ul>"
    
    body = ADMIN_ORDER_MANAGE_BODY.format(
        order_id=order.id,
        customer_name=html.escape(order.customer_name or "Не вказано"),
        phone_number=html.escape(order.phone_number or "Не вказано"),
        address=html.escape(order.address or "Самовивіз"),
        total_price=order.total_price,
        products_html=products_html,
        status_options=status_options,
        courier_options=courier_options,
        history_html=history_html or "<p>Історія статусів порожня.</p>"
    )

    active_classes = {key: "" for key in ["clients_active", "main_active", "products_active", "categories_active", "statuses_active", "settings_active", "employees_active", "reports_active", "menu_active", "tables_active", "design_active"]}
    active_classes["orders_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title=f"Керування замовленням #{order.id}", 
        body=body, 
        site_title=settings.site_title or "Назва", 
        **active_classes
    ))


@router.post("/admin/order/manage/{order_id}/set_status")
async def web_set_order_status(
    order_id: int,
    status_id: int = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Обробляє зміну статусу замовлення з веб-панелі."""
    order = await session.get(Order, order_id, options=[joinedload(Order.status)])
    if not order:
        raise HTTPException(status_code=404, detail="Замовлення не знайдено")
    
    if order.status_id == status_id:
        return RedirectResponse(url=f"/admin/order/manage/{order_id}", status_code=303)

    old_status_name = order.status.name if order.status else "Невідомий"
    order.status_id = status_id
    actor_info = "Адміністратор веб-панелі"
    
    history_entry = OrderStatusHistory(order_id=order.id, status_id=status_id, actor_info=actor_info)
    session.add(history_entry)
    
    await session.commit()

    admin_bot, client_bot = await get_bot_instances(session)
    if admin_bot:
        try:
            await notify_all_parties_on_status_change(
                order=order,
                old_status_name=old_status_name,
                actor_info=actor_info,
                admin_bot=admin_bot,
                client_bot=client_bot,
                session=session
            )
        finally:
            await admin_bot.session.close()
            if client_bot: await client_bot.session.close()

    return RedirectResponse(url=f"/admin/order/manage/{order_id}", status_code=303)


@router.post("/admin/order/manage/{order_id}/assign_courier")
async def web_assign_courier(
    order_id: int,
    courier_id: int = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Обробляє призначення кур'єра на замовлення з веб-панелі."""
    order = await session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Замовлення не знайдено")

    admin_bot, _ = await get_bot_instances(session)
    if not admin_bot:
         raise HTTPException(status_code=500, detail="Бот не налаштований для відправки сповіщень.")
         
    admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')

    try:
        old_courier_id = order.courier_id
        new_courier_name = "Не призначено"

        if old_courier_id and old_courier_id != courier_id:
            old_courier = await session.get(Employee, old_courier_id)
            if old_courier and old_courier.telegram_user_id:
                try:
                    await admin_bot.send_message(old_courier.telegram_user_id, f"❗️ Замовлення #{order.id} було знято з вас оператором.")
                except Exception as e:
                    logger.error(f"Не вдалося сповістити колишнього кур'єра {old_courier.id}: {e}")

        if courier_id == 0:
            order.courier_id = None
        else:
            new_courier = await session.get(Employee, courier_id)
            if not new_courier:
                raise HTTPException(status_code=404, detail="Кур'єра не знайдено")
            
            order.courier_id = courier_id
            new_courier_name = new_courier.full_name
            
            if new_courier.telegram_user_id:
                try:
                    kb_courier = InlineKeyboardBuilder()
                    statuses_res = await session.execute(select(OrderStatus).where(OrderStatus.visible_to_courier == True).order_by(OrderStatus.id))
                    statuses = statuses_res.scalars().all()
                    kb_courier.row(*[InlineKeyboardButton(text=s.name, callback_data=f"courier_set_status_{order.id}_{s.id}") for s in statuses])
                    
                    if order.is_delivery and order.address:
                        encoded_address = quote_plus(order.address)
                        map_url = f"http://googleusercontent.com/maps/google.com/0{encoded_address}"
                        kb_courier.row(InlineKeyboardButton(text="🗺️ На карті", url=map_url))
                        
                    await admin_bot.send_message(
                        new_courier.telegram_user_id,
                        f"🔔 Вам призначено нове замовлення!\n\n<b>Замовлення #{order.id}</b>\nАдреса: {html.escape(order.address or 'Самовивіз')}\nТелефон: {html.escape(order.phone_number)}\nСума: {order.total_price} грн.",
                        reply_markup=kb_courier.as_markup()
                    )
                except Exception as e:
                    logger.error(f"Не вдалося сповістити нового кур'єра {new_courier.telegram_user_id}: {e}")
        
        await session.commit()

        if admin_chat_id_str:
            await admin_bot.send_message(admin_chat_id_str, f"👤 Замовленню #{order.id} призначено кур'єра: <b>{html.escape(new_courier_name)}</b> (через веб-панель)")
            
    finally:
        await admin_bot.session.close()
    
    return RedirectResponse(url=f"/admin/order/manage/{order_id}", status_code=303)