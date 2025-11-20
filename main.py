# main.py

import asyncio
import logging
import sys
from os import getenv
from contextlib import asynccontextmanager
import secrets
import re
import os
import aiofiles
from typing import Dict, Any, Generator, Optional, List
from datetime import date, datetime, timedelta
import html
import json
from dotenv import load_dotenv  # <-- --- Завантаження .env ---
from urllib.parse import quote_plus as url_quote_plus

# --- FastAPI & Uvicorn ---
from fastapi import FastAPI, Form, Request, Depends, HTTPException, status, Query, File, UploadFile, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# --- Aiogram ---
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, FSInputFile, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- SQLAlchemy ---
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError
import sqlalchemy as sa
from sqlalchemy import func, and_

# --- Локальні імпорти ---
from templates import (
    ADMIN_HTML_TEMPLATE, WEB_ORDER_HTML, ADMIN_EMPLOYEE_BODY, 
    ADMIN_ROLES_BODY, ADMIN_REPORTS_BODY, ADMIN_ORDER_FORM_BODY, 
    ADMIN_SETTINGS_BODY, ADMIN_MENU_BODY, ADMIN_ORDER_MANAGE_BODY, 
    ADMIN_TABLES_BODY
)
from models import *
from admin_handlers import register_admin_handlers, parse_products_string
from courier_handlers import register_courier_handlers
from notification_manager import notify_new_order_to_staff
from admin_clients import router as clients_router
from dependencies import get_db_session, check_credentials

# --- ІМПОРТИ РОУТЕРІВ ---
from admin_order_management import router as admin_order_router
from admin_tables import router as admin_tables_router
from in_house_menu import router as in_house_menu_router
from admin_design_settings import router as admin_design_router
from admin_inventory import router as admin_inventory_router # Склад
from admin_cash import router as admin_cash_router # <--- ДОДАНО: КАСА
# -----------------------------------------------

# --- КОНФІГУРАЦІЯ ---
# Завантажуємо .env В САМОМУ ПОЧАТКУ
load_dotenv()

PRODUCTS_PER_PAGE = 5

class CheckoutStates(StatesGroup):
    waiting_for_delivery_type = State()
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_address = State()
    confirm_data = State()
    waiting_for_order_time = State()
    waiting_for_specific_time = State()

# --- TELEGRAM БОТИ ---
dp = Dispatcher()
dp_admin = Dispatcher()

async def get_main_reply_keyboard(session: AsyncSession):
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🍽️ Меню"), KeyboardButton(text="🛒 Кошик"))
    builder.row(KeyboardButton(text="📋 Мої замовлення"), KeyboardButton(text="❓ Допомога"))

    menu_items_res = await session.execute(
        sa.select(MenuItem).where(MenuItem.show_in_telegram == True).order_by(MenuItem.sort_order)
    )
    menu_items = menu_items_res.scalars().all()
    if menu_items:
        dynamic_buttons = [KeyboardButton(text=item.title.strip()) for item in menu_items]
        for i in range(0, len(dynamic_buttons), 2):
            builder.row(*dynamic_buttons[i:i+2])

    return builder.as_markup(resize_keyboard=True)

async def handle_dynamic_menu_item(message: Message, session: AsyncSession):
    menu_item_res = await session.execute(
        sa.select(MenuItem.content).where(func.trim(MenuItem.title) == message.text, MenuItem.show_in_telegram == True)
    )
    content = menu_item_res.scalar_one_or_none()

    if content is not None:
        if not content.strip():
            await message.answer("Ця сторінка наразі порожня.")
            return

        try:
            await message.answer(content, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            try:
                await message.answer(content, parse_mode=None)
            except Exception as e:
                logging.error(f"Не вдалося надіслати вміст пункту меню '{message.text}': {e}")
                await message.answer("Вибачте, сталася помилка під час відображення цієї сторінки.")
    else:
        await message.answer("Вибачте, я не зрозумів цю команду.")


@dp.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    
    # --- MODIFIED: Fetch settings to get custom welcome message ---
    settings = await session.get(Settings, 1) or Settings()
    
    default_welcome = f"Шановний {{user_name}}, ласкаво просимо! 👋\n\nМи раді вас бачити. Оберіть опцію:"
    welcome_template = settings.telegram_welcome_message or default_welcome
    
    # Replace placeholder with user's name
    try:
        caption = welcome_template.format(user_name=html.escape(message.from_user.full_name))
    except (KeyError, ValueError):
        # Fallback if the admin made a mistake in the template string
        caption = default_welcome.format(user_name=html.escape(message.from_user.full_name))
    # -------------------------------------------------------------

    keyboard = await get_main_reply_keyboard(session)
    await message.answer(caption, reply_markup=keyboard)


@dp.message(F.text == "🍽️ Меню")
async def handle_menu_message(message: Message, session: AsyncSession):
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await show_menu(message, session)

@dp.message(F.text == "🛒 Кошик")
async def handle_cart_message(message: Message, session: AsyncSession):
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await show_cart(message, session)

@dp.message(F.text == "📋 Мої замовлення")
async def handle_my_orders_message(message: Message, session: AsyncSession):
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await show_my_orders(message, session)

@dp.message(F.text == "❓ Допомога")
async def handle_help_message(message: Message):
    text = "Шановний клієнте, ось інструкція:\n- /start: Розпочати роботу з ботом\n- Додайте страви до кошика\n- Оформлюйте замовлення з доставкою\n- Переглядайте свої замовлення\nМи завжди раді допомогти!"
    await message.answer(text)

@dp.message(Command("cancel"))
async def cancel_checkout(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Шановний клієнте, оформлення замовлення скасовано. Будь ласка, звертайтеся, якщо потрібна допомога.")

@dp.callback_query(F.data == "start_menu")
async def back_to_start_menu(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        logging.warning(f"Не вдалося видалити повідомлення в back_to_start_menu: {e}")

    # --- MODIFIED: Use the same logic as command_start_handler ---
    settings = await session.get(Settings, 1) or Settings()
    default_welcome = f"Шановний {{user_name}}, ласкаво просимо! 👋\n\nМи раді вас бачити. Оберіть опцію:"
    welcome_template = settings.telegram_welcome_message or default_welcome
    try:
        caption = welcome_template.format(user_name=html.escape(callback.from_user.full_name))
    except (KeyError, ValueError):
        caption = default_welcome.format(user_name=html.escape(callback.from_user.full_name))
    # -------------------------------------------------------------

    keyboard = await get_main_reply_keyboard(session)
    await callback.message.answer(caption, reply_markup=keyboard)
    await callback.answer()

async def show_my_orders(message_or_callback: Message | CallbackQuery, session: AsyncSession):
    is_callback = isinstance(message_or_callback, CallbackQuery)
    message = message_or_callback.message if is_callback else message_or_callback
    user_id = message_or_callback.from_user.id

    orders_result = await session.execute(
        sa.select(Order).options(joinedload(Order.status)).where(Order.user_id == user_id).order_by(Order.id.desc())
    )
    orders = orders_result.scalars().all()

    if not orders:
        text = "Шановний клієнте, у вас поки що немає замовлень. Чекаємо на ваше перше!"
        if is_callback:
            await message_or_callback.answer(text, show_alert=True)
        else:
            await message.answer(text)
        return

    text = "📋 <b>Ваші замовлення:</b>\n\n"
    for order in orders:
        status_name = order.status.name if order.status else 'Невідомий'
        text += f"<b>Замовлення #{order.id} ({status_name})</b>\nСтрави: {html.escape(order.products)}\nСума: {order.total_price} грн\n\n"

    kb = InlineKeyboardBuilder().add(InlineKeyboardButton(text="⬅️ Головне меню", callback_data="start_menu")).as_markup()

    if is_callback:
        try:
            await message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await message.delete()
            await message.answer(text, reply_markup=kb)
        await message_or_callback.answer()
    else:
        await message.answer(text, reply_markup=kb)

# --- Функція show_menu ---
async def show_menu(message_or_callback: Message | CallbackQuery, session: AsyncSession):
    is_callback = isinstance(message_or_callback, CallbackQuery)
    message = message_or_callback.message if is_callback else message_or_callback

    keyboard = InlineKeyboardBuilder()
    # Додано .where(Category.show_on_delivery_site == True)
    categories_result = await session.execute(
        sa.select(Category)
        .where(Category.show_on_delivery_site == True)
        .order_by(Category.sort_order, Category.name)
    )
    categories = categories_result.scalars().all()

    if not categories:
        text = "Шановний клієнте, меню поки що порожнє. Зачекайте на оновлення!"
        if is_callback: await message_or_callback.answer(text, show_alert=True)
        else: await message.answer(text)
        return

    for category in categories:
        keyboard.add(InlineKeyboardButton(text=category.name, callback_data=f"show_category_{category.id}_1"))
    keyboard.add(InlineKeyboardButton(text="⬅️ Головне меню", callback_data="start_menu"))
    keyboard.adjust(1)

    text = "Шановний клієнте, ось категорії страв:"

    if is_callback:
        try:
            await message.edit_text(text, reply_markup=keyboard.as_markup())
        except TelegramBadRequest:
            await message.delete()
            await message.answer(text, reply_markup=keyboard.as_markup())
        await message_or_callback.answer()
    else:
        await message.answer(text, reply_markup=keyboard.as_markup())
# --- КІНЕЦЬ show_menu ---

@dp.callback_query(F.data == "menu")
async def show_menu_callback(callback: CallbackQuery, session: AsyncSession):
    await show_menu(callback, session)

@dp.callback_query(F.data.startswith("show_category_"))
async def show_category_paginated(callback: CallbackQuery, session: AsyncSession):
    await callback.answer("⏳ Завантаження...")
    parts = callback.data.split("_")
    category_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 1

    category = await session.get(Category, category_id)
    if not category:
        await callback.answer("Категорію не знайдено!", show_alert=True)
        return

    offset = (page - 1) * PRODUCTS_PER_PAGE
    query_total = sa.select(sa.func.count(Product.id)).where(Product.category_id == category_id, Product.is_active == True)
    query_products = sa.select(Product).where(Product.category_id == category_id, Product.is_active == True).order_by(Product.name).offset(offset).limit(PRODUCTS_PER_PAGE)

    total_products_res = await session.execute(query_total)
    total_products = total_products_res.scalar_one_or_none() or 0 # Ensure total is not None

    total_pages = (total_products + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE

    products_result = await session.execute(query_products)
    products_on_page = products_result.scalars().all()

    keyboard = InlineKeyboardBuilder()
    for product in products_on_page:
        keyboard.add(InlineKeyboardButton(text=f"{product.name} - {product.price} грн", callback_data=f"show_product_{product.id}"))

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"show_category_{category_id}_{page-1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"show_category_{category_id}_{page+1}"))
    if nav_buttons:
        keyboard.row(*nav_buttons)

    keyboard.row(InlineKeyboardButton(text="Меню категорій", callback_data="menu"))
    keyboard.adjust(1)

    text = f"<b>{html.escape(category.name)}</b> (Сторінка {page}):"

    try:
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    except TelegramBadRequest as e:
        if "there is no text in the message to edit" in str(e):
            await callback.message.delete()
            await callback.message.answer(text, reply_markup=keyboard.as_markup())
        else:
            logging.error(f"Неочікувана помилка TelegramBadRequest у show_category_paginated: {e}")

async def get_photo_input(image_url: str):
    if image_url and os.path.exists(image_url) and os.path.getsize(image_url) > 0:
        return FSInputFile(image_url)
    return None

@dp.callback_query(F.data.startswith("show_product_"))
async def show_product(callback: CallbackQuery, session: AsyncSession):
    await callback.answer("⏳ Завантаження...")
    product_id = int(callback.data.split("_")[2])
    product = await session.get(Product, product_id)

    if not product or not product.is_active:
        await callback.answer("Страву не знайдено або вона тимчасово недоступна!", show_alert=True)
        return

    text = (f"<b>{html.escape(product.name)}</b>\n\n"
            f"<i>{html.escape(product.description or 'Опис відсутній.')}</i>\n\n"
            f"<b>Ціна: {product.price} грн</b>")

    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="➕ Додати в кошик", callback_data=f"add_to_cart_{product.id}"))
    kb.add(InlineKeyboardButton(text="⬅️ Назад до страв", callback_data=f"show_category_{product.category_id}_1"))
    kb.adjust(1)

    photo_input = await get_photo_input(product.image_url)
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        logging.warning(f"Не вдалося видалити повідомлення в show_product: {e}")

    if photo_input:
        await callback.message.answer_photo(photo=photo_input, caption=text, reply_markup=kb.as_markup())
    else:
        await callback.message.answer(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("add_to_cart_"))
async def add_to_cart(callback: CallbackQuery, session: AsyncSession):
    try:
        product_id = int(callback.data.split("_")[3])
    except (IndexError, ValueError):
        await callback.answer("Помилка! Не вдалося обробити запит.", show_alert=True)
        logging.error(f"Не вдалося розпарсити product_id з даних колбеку: {callback.data}")
        return

    user_id = callback.from_user.id

    product = await session.get(Product, product_id)
    if not product or not product.is_active:
        await callback.answer("Ця страва тимчасово недоступна.", show_alert=True)
        return

    result = await session.execute(sa.select(CartItem).where(CartItem.user_id == user_id, CartItem.product_id == product_id))
    cart_item = result.scalars().first()

    if cart_item:
        cart_item.quantity += 1
    else:
        cart_item = CartItem(user_id=user_id, product_id=product_id, quantity=1)
        session.add(cart_item)

    await session.commit()
    await callback.answer(f"✅ {html.escape(product.name)} додано до кошика!", show_alert=False)

async def show_cart(message_or_callback: Message | CallbackQuery, session: AsyncSession):
    is_callback = isinstance(message_or_callback, CallbackQuery)
    message = message_or_callback.message if is_callback else message_or_callback
    user_id = message_or_callback.from_user.id

    cart_items_result = await session.execute(sa.select(CartItem).options(joinedload(CartItem.product)).where(CartItem.user_id == user_id).order_by(CartItem.id))
    cart_items = cart_items_result.scalars().all()

    if not cart_items:
        text = "Шановний клієнте, ваш кошик порожній. Оберіть щось смачненьке з меню!"
        if is_callback:
            await message_or_callback.answer(text, show_alert=True)
            await show_menu(message_or_callback, session)
        else:
            await message.answer(text)
        return

    text = "🛒 <b>Ваш кошик:</b>\n\n"
    total_price = 0
    kb = InlineKeyboardBuilder()

    for item in cart_items:
        if item.product:
            item_total = item.product.price * item.quantity
            total_price += item_total
            text += f"<b>{html.escape(item.product.name)}</b>\n"
            text += f"<i>{item.quantity} шт. x {item.product.price} грн</i> = <code>{item_total} грн</code>\n\n"
            kb.row(
                InlineKeyboardButton(text="➖", callback_data=f"change_qnt_{item.product.id}_-1"),
                InlineKeyboardButton(text=f"{item.quantity}", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data=f"change_qnt_{item.product.id}_1"),
                InlineKeyboardButton(text="❌", callback_data=f"delete_item_{item.product.id}")
            )

    text += f"\n<b>Разом до сплати: {total_price} грн</b>"

    kb.row(InlineKeyboardButton(text="✅ Оформити замовлення", callback_data="checkout"))
    kb.row(InlineKeyboardButton(text="🗑️ Очистити кошик", callback_data="clear_cart"))
    kb.row(InlineKeyboardButton(text="⬅️ Продовжити покупки", callback_data="menu"))

    if is_callback:
        try:
            await message.edit_text(text, reply_markup=kb.as_markup())
        except TelegramBadRequest:
            await message.delete()
            await message.answer(text, reply_markup=kb.as_markup())
        await message_or_callback.answer()
    else:
        await message.answer(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data == "cart")
async def show_cart_callback(callback: CallbackQuery, session: AsyncSession):
    await show_cart(callback, session)

@dp.callback_query(F.data.startswith("change_qnt_"))
async def change_quantity(callback: CallbackQuery, session: AsyncSession):
    await callback.answer("⏳ Оновлюю...")
    product_id, change = map(int, callback.data.split("_")[2:])
    cart_item_res = await session.execute(sa.select(CartItem).where(CartItem.user_id == callback.from_user.id, CartItem.product_id == product_id))
    cart_item = cart_item_res.scalars().first()


    if not cart_item: return

    cart_item.quantity += change
    if cart_item.quantity < 1:
        await session.delete(cart_item)
    await session.commit()
    await show_cart(callback, session)

@dp.callback_query(F.data.startswith("delete_item_"))
async def delete_from_cart(callback: CallbackQuery, session: AsyncSession):
    await callback.answer("⏳ Видаляю...")
    product_id = int(callback.data.split("_")[2])
    await session.execute(sa.delete(CartItem).where(CartItem.user_id == callback.from_user.id, CartItem.product_id == product_id))
    await session.commit()
    await show_cart(callback, session)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: CallbackQuery, session: AsyncSession):
    await session.execute(sa.delete(CartItem).where(CartItem.user_id == callback.from_user.id))
    await session.commit()
    await callback.answer("Кошик очищено!", show_alert=True)
    await show_menu(callback, session)

@dp.callback_query(F.data == "checkout")
async def start_checkout(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    user_id = callback.from_user.id
    cart_items_result = await session.execute(
        sa.select(CartItem).options(joinedload(CartItem.product)).where(CartItem.user_id == user_id)
    )
    cart_items = cart_items_result.scalars().all()

    if not cart_items:
        await callback.answer("Шановний клієнте, кошик порожній! Оберіть щось з меню.", show_alert=True)
        return

    total_price = sum(item.product.price * item.quantity for item in cart_items if item.product)
    products_str = [f"{item.product.name} x {item.quantity}" for item in cart_items if item.product]

    await state.update_data(
        total_price=total_price,
        products=", ".join(products_str),
        user_id=user_id,
        username=callback.from_user.username,
        order_type='delivery' # За замовчуванням
    )
    await state.set_state(CheckoutStates.waiting_for_delivery_type)
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="🚚 Доставка", callback_data="delivery_type_delivery"))
    kb.add(InlineKeyboardButton(text="🏠 Самовивіз", callback_data="delivery_type_pickup"))
    kb.adjust(1)

    try:
        await callback.message.edit_text("Шановний клієнте, оберіть тип отримання замовлення:", reply_markup=kb.as_markup())
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer("Шановний клієнте, оберіть тип отримання замовлення:", reply_markup=kb.as_markup())

    await callback.answer()

@dp.callback_query(F.data.startswith("delivery_type_"))
async def process_delivery_type(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    delivery_type = callback.data.split("_")[2]
    is_delivery = delivery_type == "delivery"
    await state.update_data(is_delivery=is_delivery, order_type=delivery_type)
    customer = await session.get(Customer, callback.from_user.id)
    if customer and customer.name and customer.phone_number and (not is_delivery or customer.address):
        text = f"Шановний клієнте, ми маємо ваші дані:\nІм'я: {customer.name}\nТелефон: {customer.phone_number}"
        if is_delivery:
            text += f"\nАдреса: {customer.address}"
        text += "\nБажаєте використати ці дані?"
        kb = InlineKeyboardBuilder()
        kb.add(InlineKeyboardButton(text="✅ Так", callback_data="confirm_data_yes"))
        kb.add(InlineKeyboardButton(text="✏️ Змінити", callback_data="confirm_data_no"))
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
        await state.set_state(CheckoutStates.confirm_data)
    else:
        await state.set_state(CheckoutStates.waiting_for_name)
        await callback.message.edit_text("Шановний клієнте, будь ласка, введіть ваше ім'я (наприклад, Іван):")
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_data_"))
async def process_confirm_data(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    confirm = callback.data.split("_")[2]
    try:
        await callback.message.delete()
    except TelegramBadRequest as e:
        logging.warning(f"Не вдалося видалити повідомлення в process_confirm_data: {e}")

    message = callback.message

    if confirm == "yes":
        customer = await session.get(Customer, callback.from_user.id)
        data_to_update = {"customer_name": customer.name, "phone_number": customer.phone_number}
        if (await state.get_data()).get("is_delivery"):
            data_to_update["address"] = customer.address
        await state.update_data(**data_to_update)

        await ask_for_order_time(message, state, session)
    else:
        await state.set_state(CheckoutStates.waiting_for_name)
        await message.answer("Шановний клієнте, будь ласка, введіть ваше ім'я (наприклад, Іван Іванов):")
    await callback.answer()

@dp.message(CheckoutStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) < 3:
        await message.answer("Шановний клієнте, ім'я повинно бути не менше 3 символів! Спробуйте ще раз.")
        return
    await state.update_data(customer_name=name)
    await state.set_state(CheckoutStates.waiting_for_phone)
    await message.answer("Будь ласка, введіть номер телефону (наприклад, +380XXXXXXXXX):")

@dp.message(CheckoutStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext, session: AsyncSession):
    phone = message.text.strip()
    if not re.match(r'^\+?\d{10,15}$', phone):
        await message.answer("Шановний клієнте, некоректний номер телефону! Він повинен бути у форматі +380XXXXXXXXX. Спробуйте ще раз.")
        return
    await state.update_data(phone_number=phone)
    data = await state.get_data()
    if data.get('is_delivery'):
        await state.set_state(CheckoutStates.waiting_for_address)
        await message.answer("Будь ласка, введіть вулицю та номер будинку для доставки (наприклад, вул. Головна, 1):")
    else:
        await ask_for_order_time(message, state, session)

@dp.message(CheckoutStates.waiting_for_address)
async def process_address(message: Message, state: FSMContext, session: AsyncSession):
    address = message.text.strip()
    if not address or len(address) < 5:
        await message.answer("Шановний клієнте, адреса повинна бути не менше 5 символів! Спробуйте ще раз.")
        return
    await state.update_data(address=address)
    await ask_for_order_time(message, state, session)

async def ask_for_order_time(message_or_callback: Message | CallbackQuery, state: FSMContext, session: AsyncSession):
    await state.set_state(CheckoutStates.waiting_for_order_time)
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="🚀 Якнайшвидше", callback_data="order_time_asap"))
    kb.add(InlineKeyboardButton(text="🕒 На конкретний час", callback_data="order_time_specific"))
    text = "Чудово! Останній крок: коли доставити замовлення?"

    current_message = message_or_callback if isinstance(message_or_callback, Message) else message_or_callback.message
    await current_message.answer(text, reply_markup=kb.as_markup())
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.answer()

@dp.callback_query(CheckoutStates.waiting_for_order_time, F.data.startswith("order_time_"))
async def process_order_time(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    time_choice = callback.data.split("_")[2]

    if time_choice == "asap":
        await state.update_data(delivery_time="Якнайшвидше")
        try:
            await callback.message.delete()
        except TelegramBadRequest as e:
            logging.warning(f"Не вдалося видалити повідомлення в process_order_time: {e}")
        await finalize_order(callback.message, state, session)
    else: # "specific"
        await state.set_state(CheckoutStates.waiting_for_specific_time)
        await callback.message.edit_text("Будь ласка, введіть бажаний час доставки (наприклад, '18:30' або 'на 14:00'):")
    await callback.answer()

@dp.message(CheckoutStates.waiting_for_specific_time)
async def process_specific_time(message: Message, state: FSMContext, session: AsyncSession):
    specific_time = message.text.strip()
    if not specific_time:
        await message.answer("Час не може бути порожнім. Спробуйте ще раз.")
        return
    await state.update_data(delivery_time=specific_time)
    await finalize_order(message, state, session)

async def finalize_order(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    user_id = data.get('user_id')
    admin_bot = dp_admin.get("bot_instance")
    
    order = Order(
        user_id=data['user_id'], username=data.get('username'), products=data['products'],
        total_price=data['total_price'], customer_name=data['customer_name'],
        phone_number=data['phone_number'], address=data.get('address'),
        is_delivery=data.get('is_delivery', True),
        delivery_time=data.get('delivery_time', 'Якнайшвидше'),
        order_type=data.get('order_type', 'delivery')
    )
    session.add(order)

    if user_id:
        customer = await session.get(Customer, user_id)
        if not customer:
            customer = Customer(user_id=user_id)
            session.add(customer)
        customer.name, customer.phone_number = data['customer_name'], data['phone_number']
        if 'address' in data and data['address'] is not None:
            customer.address = data.get('address')
        await session.execute(sa.delete(CartItem).where(CartItem.user_id == user_id))

    await session.commit()
    await session.refresh(order)

    if admin_bot:
        await notify_new_order_to_staff(admin_bot, order, session)

    await message.answer("Шановний клієнте, ваше замовлення оформлено! Дякуємо за вибір. Смачного!")

    await state.clear()
    await command_start_handler(message, state, session)

# --- Функція start_bot ---
async def start_bot(client_dp: Dispatcher, admin_dp: Dispatcher):
    try:
        # Читаємо токени напряму з змінних оточення
        client_token = os.environ.get('CLIENT_BOT_TOKEN')
        admin_token = os.environ.get('ADMIN_BOT_TOKEN')

        if not all([client_token, admin_token]):
            logging.warning("Токени CLIENT_BOT_TOKEN або ADMIN_BOT_TOKEN не встановлені в .env! Боти не будуть запущені.")
            return

        bot = Bot(token=client_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        admin_bot = Bot(token=admin_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

        admin_dp["client_bot"] = bot
        admin_dp["bot_instance"] = admin_bot
        client_dp["admin_bot_instance"] = admin_bot
        client_dp["session_factory"] = async_session_maker
        admin_dp["session_factory"] = async_session_maker

        # Реєструємо динамічний обробник меню (вимагає сесії)
        client_dp.message.register(handle_dynamic_menu_item, F.text)

        register_admin_handlers(admin_dp)
        register_courier_handlers(admin_dp)

        client_dp.callback_query.middleware(DbSessionMiddleware(session_pool=async_session_maker))
        client_dp.message.middleware(DbSessionMiddleware(session_pool=async_session_maker))
        admin_dp.callback_query.middleware(DbSessionMiddleware(session_pool=async_session_maker))
        admin_dp.message.middleware(DbSessionMiddleware(session_pool=async_session_maker))

        await bot.delete_webhook(drop_pending_updates=True)
        await admin_bot.delete_webhook(drop_pending_updates=True)

        logging.info("Запускаємо ботів...")
        await asyncio.gather(
            client_dp.start_polling(bot),
            admin_dp.start_polling(admin_bot)
        )
    except Exception as e:
        logging.critical(f"Не вдалося запустити ботів: {e}", exc_info=True)
# --- КІНЕЦЬ ОНОВЛЕННЯ start_bot ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Запуск...")
    os.makedirs("static/images", exist_ok=True)
    os.makedirs("static/favicons", exist_ok=True)
    await create_db_tables()
    bot_task = asyncio.create_task(start_bot(dp, dp_admin))
    yield
    logging.info("Зупинка...")
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        logging.info("Завдання бота успішно скасовано.")

app = FastAPI(lifespan=lifespan)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- ПІДКЛЮЧЕННЯ НОВИХ РОУТЕРІВ ---
app.include_router(in_house_menu_router) # Для QR-меню
app.include_router(clients_router)
app.include_router(admin_order_router)
app.include_router(admin_tables_router) # Для адмінки столиків
app.include_router(admin_design_router) # <-- NEW ROUTER FOR DESIGN
app.include_router(admin_inventory_router) # Склад
app.include_router(admin_cash_router) # <--- ДОДАНО: КАСА
# ------------------------------------

class DbSessionMiddleware:
    def __init__(self, session_pool): self.session_pool = session_pool
    async def __call__(self, handler, event, data: Dict[str, Any]):
        async with self.session_pool() as session:
            data['session'] = session
            return await handler(event, data)

# --- FastAPI ендпоінти ---
@app.get("/", response_class=HTMLResponse)
async def get_web_ordering_page(session: AsyncSession = Depends(get_db_session)):
    settings = await get_settings(session)
    logo_html = f'<img src="/{settings.logo_url}" alt="Логотип" class="header-logo">' if settings.logo_url else ''

    menu_items_res = await session.execute(
        sa.select(MenuItem).where(MenuItem.show_on_website == True).order_by(MenuItem.sort_order)
    )
    menu_items = menu_items_res.scalars().all()
    menu_links_html = "".join(
        [f'<a href="#" class="menu-popup-trigger" data-item-id="{item.id}">{html.escape(item.title)}</a>' for item in menu_items]
    )

    # --- NEW: Prepare design/SEO variables ---
    site_title = settings.site_title or "Назва"
    primary_color_val = settings.primary_color or "#5a5a5a"
    
    # ДОДАНО: Нові кольори
    secondary_color_val = settings.secondary_color or "#eeeeee"
    background_color_val = settings.background_color or "#f4f4f4"
    
    # --- NEW: Advanced Colors ---
    text_color_val = settings.text_color or "#333333"
    footer_bg_color_val = settings.footer_bg_color or "#333333"
    footer_text_color_val = settings.footer_text_color or "#ffffff"
    # ----------------------------
    
    font_family_sans_val = settings.font_family_sans or "Golos Text"
    font_family_serif_val = settings.font_family_serif or "Playfair Display"

    # --- NEW: Footer Social Links ---
    social_links = []
    if settings.instagram_url:
        social_links.append(f'<a href="{html.escape(settings.instagram_url)}" target="_blank"><i class="fa-brands fa-instagram"></i></a>')
    if settings.facebook_url:
        social_links.append(f'<a href="{html.escape(settings.facebook_url)}" target="_blank"><i class="fa-brands fa-facebook"></i></a>')
    
    social_links_html = "".join(social_links)
    # ---------------------------------------

    return HTMLResponse(content=WEB_ORDER_HTML.format(
        logo_html=logo_html,
        menu_links_html=menu_links_html,
        site_title=html.escape(site_title),
        seo_description=html.escape(settings.seo_description or ""),
        seo_keywords=html.escape(settings.seo_keywords or ""),
        
        primary_color_val=primary_color_val,
        secondary_color_val=secondary_color_val,
        background_color_val=background_color_val,
        text_color_val=text_color_val, # NEW
        footer_bg_color_val=footer_bg_color_val, # NEW
        footer_text_color_val=footer_text_color_val, # NEW

        font_family_sans_val=font_family_sans_val,
        font_family_serif_val=font_family_serif_val,
        font_family_sans_encoded=url_quote_plus(font_family_sans_val),
        font_family_serif_encoded=url_quote_plus(font_family_serif_val),
        
        # --- NEW: Footer Content ---
        footer_address=html.escape(settings.footer_address or "Адреса не вказана"),
        footer_phone=html.escape(settings.footer_phone or ""),
        working_hours=html.escape(settings.working_hours or ""),
        social_links_html=social_links_html
        # ---------------------------
    ))


@app.get("/api/page/{item_id}", response_class=JSONResponse)
async def get_menu_page_content(item_id: int, session: AsyncSession = Depends(get_db_session)):
    menu_item = await session.get(MenuItem, item_id)
    if not menu_item or not menu_item.show_on_website:
        raise HTTPException(status_code=404, detail="Сторінку не знайдено")
    return {"title": menu_item.title, "content": menu_item.content}

# --- Функція /api/menu ---
@app.get("/api/menu")
async def get_menu_data(session: AsyncSession = Depends(get_db_session)):
    # Додано .where(Category.show_on_delivery_site == True)
    categories_res = await session.execute(
        sa.select(Category)
        .where(Category.show_on_delivery_site == True)
        .order_by(Category.sort_order, Category.name)
    )
    # Додано join и фільтрація
    products_res = await session.execute(
        sa.select(Product)
        .join(Category, Product.category_id == Category.id)
        .where(Product.is_active == True, Category.show_on_delivery_site == True)
    )

    categories = [{"id": c.id, "name": c.name} for c in categories_res.scalars().all()]
    products = [{"id": p.id, "name": p.name, "description": p.description, "price": p.price, "image_url": p.image_url, "category_id": p.category_id} for p in products_res.scalars().all()]

    return {"categories": categories, "products": products}
# --- КІНЕЦЬ /api/menu ---

@app.get("/api/customer_info/{phone_number}")
async def get_customer_info(phone_number: str, session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        sa.select(Order).where(Order.phone_number == phone_number).order_by(Order.id.desc()).limit(1)
    )
    last_order = result.scalars().first()
    if last_order:
        return {"customer_name": last_order.customer_name, "phone_number": last_order.phone_number, "address": last_order.address}
    raise HTTPException(status_code=404, detail="Клієнта не знайдено")

@app.post("/api/place_order")
async def place_web_order(order_data: dict = Body(...), session: AsyncSession = Depends(get_db_session)):
    items = order_data.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="Кошик порожній")

    total_price = sum(item['price'] * item['quantity'] for item in items)
    products_str = ", ".join([f"{item['name']} x {item['quantity']}" for item in items])

    is_delivery = order_data.get('is_delivery', True)
    address = order_data.get('address') if is_delivery else None
    order_type = 'delivery' if is_delivery else 'pickup'

    order = Order(
        customer_name=order_data.get('customer_name'), phone_number=order_data.get('phone_number'),
        address=address, products=products_str, total_price=total_price,
        is_delivery=is_delivery, delivery_time=order_data.get('delivery_time', "Якнайшвидше"),
        order_type=order_type
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)

    admin_bot = dp_admin.get("bot_instance")
    if admin_bot:
        await notify_new_order_to_staff(admin_bot, order, session)

    return JSONResponse(content={"message": "Замовлення успішно розміщено", "order_id": order.id})

# --- ВЕБ АДМІН-ПАНЕЛЬ ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session) # <-- NEW: Fetch settings for title
    orders_res = await session.execute(sa.select(Order).order_by(Order.id.desc()).limit(5))
    orders_count_res = await session.execute(sa.select(sa.func.count(Order.id)))
    products_count_res = await session.execute(sa.select(sa.func.count(Product.id)))
    orders_count = orders_count_res.scalar_one_or_none() or 0
    products_count = products_count_res.scalar_one_or_none() or 0


    body = f"""
    <div class="card"><strong>Ласкаво просимо, {username}!</strong></div>
    <div class="card"><h2>📈 Швидка статистика</h2><p><strong>Всього страв:</strong> {products_count}</p><p><strong>Всього замовлень:</strong> {orders_count}</p></div>
    <div class="card"><h2>📦 5 останніх замовлень</h2>
        <table><thead><tr><th>ID</th><th>Клієнт</th><th>Телефон</th><th>Сума</th></tr></thead><tbody>
        {''.join([f"<tr><td><a href='/admin/orders?search=%23{o.id}'>#{o.id}</a></td><td>{html.escape(o.customer_name)}</td><td>{html.escape(o.phone_number)}</td><td>{o.total_price} грн</td></tr>" for o in orders_res.scalars().all()]) or "<tr><td colspan='4'>Немає замовлень</td></tr>"}
        </tbody></table></div>"""

    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["main_active"] = "active"

    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Головна панель", 
        body=body, 
        site_title=settings.site_title or "Назва", # <-- NEW: Pass title
        **active_classes
    ))

# --- Функція admin_products ---
@app.get("/admin/products", response_class=HTMLResponse)
async def admin_products(page: int = Query(1, ge=1), q: str = Query(None, alias="search"), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    per_page = 10
    offset = (page - 1) * per_page

    query = sa.select(Product).options(joinedload(Product.category)).order_by(Product.id.desc())
    if q:
        query = query.where(Product.name.ilike(f"%{q}%"))

    total_res = await session.execute(sa.select(sa.func.count()).select_from(query.subquery()))
    total = total_res.scalar_one_or_none() or 0
    products_res = await session.execute(query.limit(per_page).offset(offset))
    products = products_res.scalars().all()

    pages = (total // per_page) + (1 if total % per_page > 0 else 0)

    product_rows = "".join([f"""
    <tr>
        <td>{p.id}</td>
        <td><img src="/{p.image_url or ''}" class="table-img" alt="" loading="lazy"> {html.escape(p.name)}</td>
        <td>{p.price} грн</td>
        <td>{html.escape(p.category.name if p.category else '–')}</td>
        <td>{html.escape('🍳 Кухня' if p.preparation_area == 'kitchen' else '🍹 Бар')}</td> <td>{'✅' if p.is_active else '❌'}</td>
        <td class='actions'>
            <a href='/admin/product/toggle_active/{p.id}' class='button-sm'>{'🔴' if p.is_active else '🟢'}</a>
            <a href='/admin/edit_product/{p.id}' class='button-sm'>✏️</a>
            <a href='/admin/delete_product/{p.id}' onclick="return confirm('Ви впевнені?');" class='button-sm danger'>🗑️</a>
        </td>
    </tr>""" for p in products])

    categories_res = await session.execute(sa.select(Category))
    category_options = "".join([f'<option value="{c.id}">{html.escape(c.name)}</option>' for c in categories_res.scalars().all()])
    
    links_products = []
    for i in range(1, pages + 1):
        search_part = f'&search={q}' if q else ''
        class_part = 'active' if i == page else ''
        links_products.append(f'<a href="/admin/products?page={i}{search_part}" class="{class_part}">{i}</a>')
    
    pagination = f"<div class='pagination'>{' '.join(links_products)}</div>"
    
    body = f"""
    <div class="card"><h2>📝 Додати нову страву</h2><form action="/admin/add_product" method="post" enctype="multipart/form-data">
        <label for="name">Назва страви:</label><input type="text" id="name" name="name" required>
        <label for="description">Опис:</label><textarea id="description" name="description" rows="4"></textarea>
        <label for="image">Зображення:</label><input type="file" id="image" name="image" accept="image/*">
        <label for="price">Ціна (в грн):</label><input type="number" id="price" name="price" min="1" required>
        
        <label for="preparation_area">Цех приготування:</label>
        <select id="preparation_area" name="preparation_area">
            <option value="kitchen">🍳 Кухня</option>
            <option value="bar">🍹 Бар</option>
        </select>
        
        <label for="category_id">Категорія:</label><select id="category_id" name="category_id" required>{category_options}</select><button type="submit">Додати страву</button></form></div>
    <div class="card">
        <h2>🛍️ Список страв</h2>
        <form action="/admin/products" method="get" class="search-form">
            <input type="text" name="search" placeholder="Пошук за назвою..." value="{q or ''}">
            <button type="submit">🔍 Знайти</button>
        </form>
        <table><thead><tr><th>ID</th><th>Назва</th><th>Ціна</th><th>Категорія</th><th>Цех</th><th>Статус</th><th>Дії</th></tr></thead><tbody>
        {product_rows or "<tr><td colspan='7'>Немає страв</td></tr>"}
        </tbody></table>{pagination if pages > 1 else ''}
    </div>"""

    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"

    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Управління стравами", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))
# --- КІНЕЦЬ admin_products ---


@app.post("/admin/add_product")
async def add_product(
    name: str = Form(...), 
    price: int = Form(...), 
    description: str = Form(""), 
    category_id: int = Form(...),
    preparation_area: str = Form("kitchen"), # <-- NEW FIELD
    image: UploadFile = File(None),
    session: AsyncSession = Depends(get_db_session), 
    username: str = Depends(check_credentials)
):
    if price <= 0: raise HTTPException(status_code=400, detail="Ціна повинна бути позитивною")
    image_url = None
    if image and image.filename:
        ext = image.filename.split('.')[-1] if '.' in image.filename else 'jpg'
        path = f"static/images/{secrets.token_hex(8)}.{ext}"
        try:
            async with aiofiles.open(path, 'wb') as f: await f.write(await image.read())
            image_url = path
        except Exception as e:
            logging.error(f"Не вдалося зберегти зображення: {e}")

    session.add(Product(
        name=name, 
        price=price, 
        description=description, 
        image_url=image_url, 
        category_id=category_id,
        preparation_area=preparation_area # <-- SAVE FIELD
    ))
    await session.commit()
    return RedirectResponse(url="/admin/products", status_code=303)

@app.get("/admin/edit_product/{product_id}", response_class=HTMLResponse)
async def get_edit_product_form(product_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    product = await session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Товар не знайдено")

    categories_res = await session.execute(sa.select(Category))
    category_options = "".join([f'<option value="{c.id}" {"selected" if c.id == product.category_id else ""}>{html.escape(c.name)}</option>' for c in categories_res.scalars().all()])

    # Select current preparation area
    prep_options = f"""
    <option value="kitchen" {'selected' if product.preparation_area == 'kitchen' else ''}>🍳 Кухня</option>
    <option value="bar" {'selected' if product.preparation_area == 'bar' else ''}>🍹 Бар</option>
    """

    body = f"""
    <div class="card">
      <h2>Редагування страви: {html.escape(product.name)}</h2>
      <form action="/admin/edit_product/{product_id}" method="post" enctype="multipart/form-data">
        <label for="name">Назва страви:</label>
        <input type="text" id="name" name="name" value="{html.escape(product.name)}" required>
        <label for="description">Опис:</label>
        <textarea id="description" name="description" rows="4">{html.escape(product.description or '')}</textarea>
        <label for="image">Нове зображення (залишіть порожнім, щоб не змінювати):</label>
        <input type="file" id="image" name="image" accept="image/*">
        {f'<p>Поточне зображення: <img src="/{product.image_url}" class="table-img"></p>' if product.image_url else ''}
        <label for="price">Ціна (в грн):</label>
        <input type="number" id="price" name="price" min="1" value="{product.price}" required>
        
        <label for="preparation_area">Цех приготування:</label>
        <select id="preparation_area" name="preparation_area">
            {prep_options}
        </select>
        
        <label for="category_id">Категорія:</label>
        <select id="category_id" name="category_id" required>{category_options}</select>
        <button type="submit">Зберегти зміни</button>
      </form>
    </div>
    """
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Редагування страви", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.post("/admin/edit_product/{product_id}")
async def edit_product(
    product_id: int, 
    name: str = Form(...), 
    price: int = Form(...), 
    description: str = Form(""), 
    category_id: int = Form(...),
    preparation_area: str = Form(...), # <-- NEW ARGUMENT
    image: UploadFile = File(None),
    session: AsyncSession = Depends(get_db_session), 
    username: str = Depends(check_credentials)
):
    product = await session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Товар не знайдено")

    product.name = name
    product.price = price
    product.description = description
    product.category_id = category_id
    product.preparation_area = preparation_area # <-- UPDATE FIELD

    if image and image.filename:
        if product.image_url and os.path.exists(product.image_url):
            try:
                os.remove(product.image_url)
            except OSError as e:
                logging.error(f"Не вдалося видалити старе зображення {product.image_url}: {e}")


        ext = image.filename.split('.')[-1] if '.' in image.filename else 'jpg'
        path = f"static/images/{secrets.token_hex(8)}.{ext}"
        try:
            async with aiofiles.open(path, 'wb') as f: await f.write(await image.read())
            product.image_url = path
        except Exception as e:
            logging.error(f"Не вдалося зберегти нове зображення {path}: {e}")

    await session.commit()
    return RedirectResponse(url="/admin/products", status_code=303)

@app.get("/admin/product/toggle_active/{product_id}")
async def toggle_product_active(product_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    product = await session.get(Product, product_id)
    if product:
        product.is_active = not product.is_active
        await session.commit()
    return RedirectResponse(url="/admin/products", status_code=303)

@app.get("/admin/delete_product/{product_id}")
async def delete_product(product_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    product = await session.get(Product, product_id)
    if product:
        image_to_delete = product.image_url
        await session.delete(product)
        await session.commit()
        if image_to_delete and os.path.exists(image_to_delete):
            try:
                os.remove(image_to_delete)
            except OSError as e:
                logging.error(f"Не вдалося видалити зображення {image_to_delete} після видалення продукту: {e}")

    return RedirectResponse(url="/admin/products", status_code=303)

# --- Функція admin_categories ---
@app.get("/admin/categories", response_class=HTMLResponse)
async def admin_categories(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    categories_res = await session.execute(sa.select(Category).order_by(Category.sort_order, Category.name))
    categories = categories_res.scalars().all()

    def bool_to_icon(val):
        return '✅' if val else '❌'

    rows = "".join([f"""
    <tr>
      <td>{c.id}</td>
      <td><form action="/admin/edit_category/{c.id}" method="post" class="inline-form">
          <input type="hidden" name="field" value="name_sort">
          <input type="text" name="name" value="{html.escape(c.name)}" style="width: 150px;">
          <input type="number" name="sort_order" value="{c.sort_order}" style="width: 80px;">
          <button type="submit">💾</button>
      </form></td>
      <td style="text-align: center;">
          <form action="/admin/edit_category/{c.id}" method="post" class="inline-form">
              <input type="hidden" name="field" value="show_on_delivery_site">
              <input type="hidden" name="value" value="{'false' if c.show_on_delivery_site else 'true'}">
              <button type="submit" class="button-sm" style="background: none; color: inherit; padding: 0; font-size: 1.2rem;">{bool_to_icon(c.show_on_delivery_site)}</button>
          </form>
      </td>
      <td style="text-align: center;">
          <form action="/admin/edit_category/{c.id}" method="post" class="inline-form">
              <input type="hidden" name="field" value="show_in_restaurant">
              <input type="hidden" name="value" value="{'false' if c.show_in_restaurant else 'true'}">
              <button type="submit" class="button-sm" style="background: none; color: inherit; padding: 0; font-size: 1.2rem;">{bool_to_icon(c.show_in_restaurant)}</button>
          </form>
      </td>
      <td class='actions'><a href='/admin/delete_category/{c.id}' onclick="return confirm('Ви впевнені?');" class='button-sm danger'>🗑️</a></td>
    </tr>""" for c in categories])

    body = f"""
    <div class="card">
        <h2>Додати нову категорію</h2>
        <form action="/admin/add_category" method="post">
            <label for="name">Назва категорії:</label><input type="text" id="name" name="name" required>
            <label for="sort_order">Порядок сортування:</label><input type="number" id="sort_order" name="sort_order" value="100">
            <div class="checkbox-group">
                <input type="checkbox" id="show_on_delivery_site" name="show_on_delivery_site" value="true" checked>
                <label for="show_on_delivery_site">Показувати на сайті та в боті (доставка)</label>
            </div>
            <div class="checkbox-group">
                <input type="checkbox" id="show_in_restaurant" name="show_in_restaurant" value="true" checked>
                <label for="show_in_restaurant">Показувати в закладі (QR-меню)</label>
            </div>
            <button type="submit">Додати</button>
        </form>
    </div>
    <div class="card">
        <h2>Список категорій</h2>
        <table><thead><tr><th>ID</th><th>Назва та сортування</th><th>Сайт/Бот</th><th>В закладі</th><th>Дії</th></tr></thead><tbody>
        {rows or "<tr><td colspan='5'>Немає категорій</td></tr>"}
        </tbody></table>
    </div>"""

    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["categories_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Категорії", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))
# --- КІНЕЦЬ admin_categories ---


# --- Функція add_category ---
@app.post("/admin/add_category")
async def add_category(name: str = Form(...),
                       sort_order: int = Form(100),
                       show_on_delivery_site: bool = Form(False),
                       show_in_restaurant: bool = Form(False),
                       session: AsyncSession = Depends(get_db_session),
                       username: str = Depends(check_credentials)):
    session.add(Category(
        name=name,
        sort_order=sort_order,
        show_on_delivery_site=show_on_delivery_site,
        show_in_restaurant=show_in_restaurant
    ))
    await session.commit()
    return RedirectResponse(url="/admin/categories", status_code=303)
# --- КІНЕЦЬ add_category ---


# --- Функція edit_category ---
@app.post("/admin/edit_category/{cat_id}")
async def edit_category(cat_id: int,
                        name: Optional[str] = Form(None),
                        sort_order: Optional[int] = Form(None),
                        field: Optional[str] = Form(None),
                        value: Optional[str] = Form(None),
                        session: AsyncSession = Depends(get_db_session),
                        username: str = Depends(check_credentials)):
    category = await session.get(Category, cat_id)
    if category:
        if field == "name_sort" and name is not None and sort_order is not None:
            category.name = name
            category.sort_order = sort_order
        elif field in ["show_on_delivery_site", "show_in_restaurant"]:
            setattr(category, field, value.lower() == 'true')
        await session.commit()
    return RedirectResponse(url="/admin/categories", status_code=303)
# --- КІНЕЦЬ edit_category ---

@app.get("/admin/delete_category/{cat_id}")
async def delete_category(cat_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    category = await session.get(Category, cat_id)
    if category:
        # Check if any product uses this category
        products_exist_res = await session.execute(sa.select(sa.func.count(Product.id)).where(Product.category_id == cat_id))
        products_exist = products_exist_res.scalar_one_or_none() > 0
        if products_exist:
             logging.warning(f"Attempted to delete category {cat_id} which is in use.")
             return RedirectResponse(url="/admin/categories?error=category_in_use", status_code=303)

        await session.delete(category)
        await session.commit()
    return RedirectResponse(url="/admin/categories", status_code=303)


@app.get("/admin/menu", response_class=HTMLResponse)
async def admin_menu_items(edit_id: Optional[int] = None, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    menu_items_res = await session.execute(sa.select(MenuItem).order_by(MenuItem.sort_order, MenuItem.title))
    menu_items = menu_items_res.scalars().all()

    item_to_edit = None
    if edit_id:
        item_to_edit = await session.get(MenuItem, edit_id)

    rows = "".join([f"""
    <tr>
        <td>{item.id}</td>
        <td>{html.escape(item.title)}</td>
        <td>{item.sort_order}</td>
        <td>{'✅' if item.show_on_website else '❌'}</td>
        <td>{'✅' if item.show_in_telegram else '❌'}</td>
        <td class="actions">
            <a href="/admin/menu?edit_id={item.id}" class="button-sm">✏️</a>
            <a href="/admin/menu/delete/{item.id}" onclick="return confirm('Ви впевнені?');" class="button-sm danger">🗑️</a>
        </td>
    </tr>
    """ for item in menu_items])

    body = ADMIN_MENU_BODY.format(
        rows=rows or "<tr><td colspan='6'>Немає пунктів меню</td></tr>",
        form_action=f"/admin/menu/edit/{edit_id}" if item_to_edit else "/admin/menu/add",
        form_title="Редагування пункту" if item_to_edit else "Додати новий пункт",
        item_title=html.escape(item_to_edit.title) if item_to_edit else "",
        item_content=html.escape(item_to_edit.content) if item_to_edit else "",
        item_sort_order=item_to_edit.sort_order if item_to_edit else 100,
        item_show_on_website_checked='checked' if item_to_edit and item_to_edit.show_on_website else "",
        item_show_in_telegram_checked='checked' if item_to_edit and item_to_edit.show_in_telegram else "",
        button_text="Зберегти зміни" if item_to_edit else "Додати пункт"
    )
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["menu_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Сторінки меню", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.post("/admin/menu/add")
async def add_menu_item(title: str = Form(...), content: str = Form(...), sort_order: int = Form(100),
                        show_on_website: bool = Form(False), show_in_telegram: bool = Form(False),
                        session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    new_item = MenuItem(title=title.strip(), content=content, sort_order=sort_order,
                        show_on_website=show_on_website, show_in_telegram=show_in_telegram)
    session.add(new_item)
    await session.commit()
    return RedirectResponse(url="/admin/menu", status_code=303)

@app.post("/admin/menu/edit/{item_id}")
async def edit_menu_item(item_id: int, title: str = Form(...), content: str = Form(...), sort_order: int = Form(100),
                         show_on_website: bool = Form(False), show_in_telegram: bool = Form(False),
                         session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    item = await session.get(MenuItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Пункт меню не знайдено")
    item.title = title.strip()
    item.content = content
    item.sort_order = sort_order
    item.show_on_website = show_on_website
    item.show_in_telegram = show_in_telegram
    await session.commit()
    return RedirectResponse(url="/admin/menu", status_code=303)

@app.get("/admin/menu/delete/{item_id}")
async def delete_menu_item(item_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    item = await session.get(MenuItem, item_id)
    if item:
        await session.delete(item)
        await session.commit()
    return RedirectResponse(url="/admin/menu", status_code=303)

# --- ОНОВЛЕНИЙ РОУТ ДЛЯ ЗАМОВЛЕНЬ ---
@app.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders(page: int = Query(1, ge=1), q: str = Query(None, alias="search"), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    per_page = 15
    offset = (page - 1) * per_page
    query = sa.select(Order).options(joinedload(Order.status)).order_by(Order.id.desc())
    if q:
        search_term = q.replace('#', '')
        if search_term.isdigit():
             query = query.where(sa.or_(Order.id == int(search_term), Order.customer_name.ilike(f"%{q}%"), Order.phone_number.ilike(f"%{q}%")))
        else:
             query = query.where(sa.or_(Order.customer_name.ilike(f"%{q}%"), Order.phone_number.ilike(f"%{q}%")))


    total_res = await session.execute(sa.select(sa.func.count()).select_from(query.subquery()))
    total = total_res.scalar_one_or_none() or 0
    orders_res = await session.execute(query.limit(per_page).offset(offset))
    orders = orders_res.scalars().all()
    pages = (total // per_page) + (1 if total % per_page > 0 else 0)


    rows = "".join([f"""
    <tr>
        <td><a href="/admin/order/manage/{o.id}" title="Керувати замовленням">#{o.id}</a></td>
        <td>{html.escape(o.customer_name or '')}</td>
        <td>{html.escape(o.phone_number or '')}</td>
        <td>{o.total_price} грн</td>
        <td><span class='status'>{o.status.name if o.status else '-'}</span></td>
        <td>{html.escape(o.products[:50] + '...' if o.products and len(o.products) > 50 else o.products or '')}</td>
        <td class='actions'>
            <a href='/admin/order/manage/{o.id}' class='button-sm' title="Керувати статусом та кур'єром">⚙️ Керувати</a>
            <a href='/admin/order/edit/{o.id}' class='button-sm' title="Редагувати склад замовлення">✏️ Редагувати</a>
        </td>
    </tr>""" for o in orders])

    links_orders = []
    for i in range(1, pages + 1):
        search_part = f'&search={q}' if q else ''
        class_part = 'active' if i == page else ''
        links_orders.append(f'<a href="/admin/orders?page={i}{search_part}" class="{class_part}">{i}</a>')
    
    pagination = f"<div class='pagination'>{' '.join(links_orders)}</div>"

    body = f"""
    <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem;">
            <h2>📋 Список замовлень</h2>
            <a href="/admin/order/new" class="button"><i class="fa-solid fa-plus"></i> Створити замовлення</a>
        </div>
        <form action="/admin/orders" method="get" class="search-form">
            <input type="text" name="search" placeholder="Пошук за ID, іменем, телефоном..." value="{q or ''}">
            <button type="submit">🔍 Знайти</button>
        </form>
        <table><thead><tr><th>ID</th><th>Клієнт</th><th>Телефон</th><th>Сума</th><th>Статус</th><th>Склад</th><th>Дії</th></tr></thead><tbody>
        {rows or "<tr><td colspan='7'>Немає замовлень</td></tr>"}
        </tbody></table>{pagination if pages > 1 else ''}
    </div>"""
    active_classes = {key: "" for key in ["main_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["orders_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Замовлення", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))
# ----------------------------------------

@app.get("/admin/statuses", response_class=HTMLResponse)
async def admin_statuses(error: Optional[str] = None, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    statuses_res = await session.execute(sa.select(OrderStatus).order_by(OrderStatus.id))
    statuses = statuses_res.scalars().all()

    error_html = ""
    if error == "in_use":
        error_html = "<div class='card' style='background-color: #f8d7da; color: #721c24;'><strong>Помилка!</strong> Неможливо видалити статус, оскільки він використовується.</div>"

    def bool_btn(id, field, val, label=""):
        icon = '✅' if val else '❌'
        return f"""
        <form action="/admin/edit_status/{id}" method="post" style="display:inline;">
            <input type="hidden" name="field" value="{field}">
            <input type="hidden" name="value" value="{'false' if val else 'true'}">
            <button type="submit" class="button-sm" style="background:none; border:none; cursor:pointer;" title="{label}">{icon}</button>
        </form>
        """

    rows = ""
    for s in statuses:
        rows += f"""
        <tr>
            <td>{s.id}</td>
            <td>
                <form action="/admin/edit_status/{s.id}" method="post" class="inline-form">
                    <input type="text" name="name" value="{html.escape(s.name)}" style="width: 140px; padding: 5px;" required>
                    <button type="submit" class="button-sm">💾</button>
                </form>
            </td>
            <td style="text-align:center; background:#f9f9f9;">{bool_btn(s.id, "visible_to_operator", s.visible_to_operator, "Оператор")}</td>
            <td style="text-align:center; background:#f9f9f9;">{bool_btn(s.id, "visible_to_courier", s.visible_to_courier, "Кур'єр")}</td>
            <td style="text-align:center; background:#f9f9f9;">{bool_btn(s.id, "visible_to_waiter", s.visible_to_waiter, "Офіціант")}</td>
            <td style="text-align:center; background:#f9f9f9;">{bool_btn(s.id, "visible_to_chef", s.visible_to_chef, "Повар (список)")}</td>
            <td style="text-align:center; background:#f9f9f9;">{bool_btn(s.id, "visible_to_bartender", s.visible_to_bartender, "Бармен (список)")}</td> <td style="text-align:center; border-left: 2px solid #eee;">{bool_btn(s.id, "requires_kitchen_notify", s.requires_kitchen_notify, "Відправити на виробництво")}</td>
            <td style="text-align:center;">{bool_btn(s.id, "notify_customer", s.notify_customer, "Сповістити клієнта")}</td>
            
            <td style="text-align:center; background:#fff0f0;">{bool_btn(s.id, "is_completed_status", s.is_completed_status, "Фінальний успіх")}</td>
            <td style="text-align:center; background:#fff0f0;">{bool_btn(s.id, "is_cancelled_status", s.is_cancelled_status, "Скасування")}</td>
            
            <td class="actions">
                <a href="/admin/delete_status/{s.id}" onclick="return confirm('Ви впевнені?');" class="button-sm danger">🗑️</a>
            </td>
        </tr>
        """

    rows_html = rows if rows else "<tr><td colspan='12'>Немає статусів</td></tr>"
    
    # Update colspan and column headers
    body = f"""
    {error_html}
    <style>
        .status-table th {{ font-size: 0.8rem; text-align: center; vertical-align: middle; }}
        .status-table td {{ vertical-align: middle; }}
        .group-header {{ background-color: #e0e0e0; text-align: center; font-weight: bold; font-size: 0.85rem; }}
    </style>

    <div class="card">
        <h2>Додати новий статус</h2>
        <form action="/admin/add_status" method="post" class="form-grid" style="grid-template-columns: 1fr 1fr 1fr; gap: 10px; align-items: end;">
            <div style="grid-column: span 3;">
                <label>Назва статусу:</label>
                <input type="text" name="name" placeholder="Наприклад: Готується" required>
            </div>
            
            <div class="checkbox-group"><input type="checkbox" id="vo" name="visible_to_operator" value="true" checked><label for="vo">Бачить Оператор</label></div>
            <div class="checkbox-group"><input type="checkbox" id="vc" name="visible_to_courier" value="true"><label for="vc">Бачить Кур'єр</label></div>
            <div class="checkbox-group"><input type="checkbox" id="vw" name="visible_to_waiter" value="true"><label for="vw">Бачить Офіціант</label></div>
            <div class="checkbox-group"><input type="checkbox" id="vch" name="visible_to_chef" value="true"><label for="vch">Бачить Повар</label></div>
            <div class="checkbox-group"><input type="checkbox" id="vbart" name="visible_to_bartender" value="true"><label for="vbart">Бачить Бармен</label></div> <div class="checkbox-group" style="background: #e8f5e9; padding: 5px; border-radius: 5px;">
                <input type="checkbox" id="rkn" name="requires_kitchen_notify" value="true">
                <label for="rkn">🔔 <b>Відправляти на виробництво</b></label>
            </div>
            <div class="checkbox-group"><input type="checkbox" id="nc" name="notify_customer" value="true" checked><label for="nc">🔔 Сповіщати клієнта</label></div>
            
            <div class="checkbox-group"><input type="checkbox" id="ics" name="is_completed_status" value="true"><label for="ics">🏁 Завершує (Успіх)</label></div>
            <div class="checkbox-group"><input type="checkbox" id="ican" name="is_cancelled_status" value="true"><label for="ican">🚫 Скасовує</label></div>

            <div style="grid-column: span 3; text-align: right;">
                <button type="submit">Додати статус</button>
            </div>
        </form>
    </div>

    <div class="card">
        <h2>Список статусів та налаштування</h2>
        <div class="table-wrapper">
            <table class="status-table">
                <thead>
                    <tr>
                        <th rowspan="2">ID</th>
                        <th rowspan="2">Назва</th>
                        <th colspan="5" class="group-header">👁️ Хто бачить у списку</th> <th colspan="2" class="group-header">🔔 Дії при переході</th>
                        <th colspan="2" class="group-header">🏁 Системні</th>
                        <th rowspan="2">Дії</th>
                    </tr>
                    <tr>
                        <th>Опер.</th><th>Кур'єр</th><th>Офіц.</th><th>Повар</th><th>Бармен</th> <th style="border-left: 2px solid #ccc;">На виробництво</th><th>Клієнту</th>
                        <th>Успіх</th><th>Відміна</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
        </div>
        <p style="margin-top: 10px; font-size: 0.9rem; color: #666;">
            * <b>На виробництво</b>: Якщо увімкнено, при переході в цей статус **повару та бармену** буде відправлено розділений чек.<br>
            * <b>Хто бачить</b>: Чи відображається замовлення з таким статусом у списку відповідного працівника.
        </p>
    </div>
    """
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["statuses_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Статуси замовлень", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.post("/admin/add_status")
async def add_status(
    name: str = Form(...),
    notify_customer: Optional[bool] = Form(False),
    visible_to_operator: Optional[bool] = Form(False),
    visible_to_courier: Optional[bool] = Form(False),
    visible_to_waiter: Optional[bool] = Form(False),
    visible_to_chef: Optional[bool] = Form(False),
    visible_to_bartender: Optional[bool] = Form(False), # <--- NEW FIELD
    requires_kitchen_notify: Optional[bool] = Form(False),
    is_completed_status: Optional[bool] = Form(False),
    is_cancelled_status: Optional[bool] = Form(False),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    new_status = OrderStatus(
        name=name,
        notify_customer=bool(notify_customer),
        visible_to_operator=bool(visible_to_operator),
        visible_to_courier=bool(visible_to_courier),
        visible_to_waiter=bool(visible_to_waiter),
        visible_to_chef=bool(visible_to_chef),
        visible_to_bartender=bool(visible_to_bartender), # <--- SAVE NEW FIELD
        requires_kitchen_notify=bool(requires_kitchen_notify),
        is_completed_status=bool(is_completed_status),
        is_cancelled_status=bool(is_cancelled_status)
    )
    session.add(new_status)
    await session.commit()
    return RedirectResponse(url="/admin/statuses", status_code=303)

@app.post("/admin/edit_status/{status_id}")
async def edit_status(
    status_id: int,
    name: Optional[str] = Form(None),
    field: Optional[str] = Form(None),
    value: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    status_to_edit = await session.get(OrderStatus, status_id)
    if not status_to_edit:
        raise HTTPException(status_code=404, detail="Статус не знайдено")

    allowed_fields = [
        "notify_customer", "visible_to_operator", "visible_to_courier", 
        "visible_to_waiter", "visible_to_chef", "visible_to_bartender", # <--- ADD NEW FIELD
        "requires_kitchen_notify", 
        "is_completed_status", "is_cancelled_status"
    ]

    if name and not field:
        status_to_edit.name = name
    elif field in allowed_fields:
        setattr(status_to_edit, field, value.lower() == 'true')

    await session.commit()
    return RedirectResponse(url="/admin/statuses", status_code=303)


@app.get("/admin/delete_status/{status_id}")
async def delete_status(status_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    status_to_delete = await session.get(OrderStatus, status_id)
    if status_to_delete:
        try:
            await session.delete(status_to_delete)
            await session.commit()
        except IntegrityError: # Catch the specific database error
            logging.warning(f"Attempted to delete status {status_id} which is in use.")
            return RedirectResponse(url="/admin/statuses?error=in_use", status_code=303) # Redirect with error flag
        except Exception as e: # Catch other potential errors
            logging.error(f"Error deleting status {status_id}: {e}")
            raise HTTPException(status_code=500, detail="Не вдалося видалити статус.")
    return RedirectResponse(url="/admin/statuses", status_code=303)


@app.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    roles_res = await session.execute(sa.select(Role).order_by(Role.id))
    roles = roles_res.scalars().all()

    # NOTE: The models.py was updated with can_receive_bar_orders. We must ensure it's loaded.
    rows = "".join([f"""
    <tr>
        <td>{r.id}</td>
        <td>{html.escape(r.name)}</td>
        <td>{'✅' if r.can_manage_orders else '❌'}</td>
        <td>{'✅' if r.can_be_assigned else '❌'}</td>
        <td>{'✅' if r.can_serve_tables else '❌'}</td>
        <td>{'✅' if r.can_receive_kitchen_orders else '❌'}</td>
        <td>{'✅' if r.can_receive_bar_orders else '❌'}</td> <td class="actions">
            <a href="/admin/edit_role/{r.id}" class="button-sm">✏️</a>
            <a href="/admin/delete_role/{r.id}" onclick="return confirm('Ви впевнені?');" class='button-sm danger'>🗑️</a>
        </td>
    </tr>""" for r in roles])

    if not rows:
        rows = "<tr><td colspan='8'>Немає ролей</td></tr>" # Colspan adjusted

    # Update HTML form to include Barman checkbox
    body = f"""
    <div class="card">
        <ul class="nav-tabs">
            <li class="nav-item"><a href="/admin/employees">Співробітники</a></li>
            <li class="nav-item"><a href="/admin/roles" class="active">Ролі</a></li>
        </ul>
        <h2>Додати нову роль</h2>
        <form action="/admin/add_role" method="post">
            <label for="name">Назва ролі:</label><input type="text" id="name" name="name" required>
            <div class="checkbox-group">
                <input type="checkbox" id="can_manage_orders" name="can_manage_orders" value="true">
                <label for="can_manage_orders">Може керувати замовленнями (Оператор)</label>
            </div>
            <div class="checkbox-group">
                <input type="checkbox" id="can_be_assigned" name="can_be_assigned" value="true">
                <label for="can_be_assigned">Може бути призначений на замовлення (Кур'єр)</label>
            </div>
             <div class="checkbox-group">
                <input type="checkbox" id="can_serve_tables" name="can_serve_tables" value="true">
                <label for="can_serve_tables">Може обслуговувати столики (Офіціант)</label>
            </div>
             <div class="checkbox-group">
                <input type="checkbox" id="can_receive_kitchen_orders" name="can_receive_kitchen_orders" value="true">
                <label for="can_receive_kitchen_orders">Отримує замовлення для приготування (Повар)</label>
            </div>
             <div class="checkbox-group">
                <input type="checkbox" id="can_receive_bar_orders" name="can_receive_bar_orders" value="true">
                <label for="can_receive_bar_orders">Отримує замовлення для бару (Бармен)</label> </div>
            <button type="submit">Додати роль</button>
        </form>
    </div>
    <div class="card">
        <h2>Список ролей</h2>
        <table><thead><tr><th>ID</th><th>Назва</th><th>Керув. замовл.</th><th>Признач. доставку</th><th>Обслуг. столики</th><th>Кухня</th><th>Бар</th><th>Дії</th></tr></thead><tbody> {rows}
        </tbody></table>
    </div>
    """
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["employees_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Ролі співробітників", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.post("/admin/add_role")
async def add_role(name: str = Form(...),
                   can_manage_orders: Optional[bool] = Form(False),
                   can_be_assigned: Optional[bool] = Form(False),
                   can_serve_tables: Optional[bool] = Form(False),
                   can_receive_kitchen_orders: Optional[bool] = Form(False), 
                   can_receive_bar_orders: Optional[bool] = Form(False), # <--- SAVE NEW FIELD
                   session: AsyncSession = Depends(get_db_session),
                   username: str = Depends(check_credentials)):
    new_role = Role(name=name,
                    can_manage_orders=bool(can_manage_orders),
                    can_be_assigned=bool(can_be_assigned),
                    can_serve_tables=bool(can_serve_tables),
                    can_receive_kitchen_orders=bool(can_receive_kitchen_orders),
                    can_receive_bar_orders=bool(can_receive_bar_orders)) # <--- SAVE NEW FIELD
    session.add(new_role)
    await session.commit()
    return RedirectResponse(url="/admin/roles", status_code=303)


@app.get("/admin/edit_role/{role_id}", response_class=HTMLResponse)
async def get_edit_role_form(role_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    role = await session.get(Role, role_id)
    if not role: raise HTTPException(status_code=404, detail="Роль не знайдено")

    # Update HTML to include Barman checkbox
    body = f"""
    <div class="card">
        <ul class="nav-tabs"><li class="nav-item"><a href="/admin/employees">Співробітники</a></li><li class="nav-item"><a href="/admin/roles" class="active">Ролі</a></li></ul>
        <h2>Редагування ролі: {html.escape(role.name)}</h2>
        <form action="/admin/edit_role/{role_id}" method="post">
            <label for="name">Назва ролі:</label><input type="text" id="name" name="name" value="{html.escape(role.name)}" required>
            <div class="checkbox-group">
                <input type="checkbox" id="can_manage_orders" name="can_manage_orders" value="true" {'checked' if role.can_manage_orders else ''}>
                <label for="can_manage_orders">Може керувати замовленнями (Оператор)</label>
            </div>
            <div class="checkbox-group">
                <input type="checkbox" id="can_be_assigned" name="can_be_assigned" value="true" {'checked' if role.can_be_assigned else ''}>
                <label for="can_be_assigned">Може бути призначений на замовлення (Кур'єр)</label>
            </div>
            <div class="checkbox-group">
                <input type="checkbox" id="can_serve_tables" name="can_serve_tables" value="true" {'checked' if role.can_serve_tables else ''}>
                <label for="can_serve_tables">Може обслуговувати столики (Офіціант)</label>
            </div>
            <div class="checkbox-group">
                <input type="checkbox" id="can_receive_kitchen_orders" name="can_receive_kitchen_orders" value="true" {'checked' if role.can_receive_kitchen_orders else ''}>
                <label for="can_receive_kitchen_orders">Отримує замовлення для приготування (Повар)</label>
            </div>
            <div class="checkbox-group">
                <input type="checkbox" id="can_receive_bar_orders" name="can_receive_bar_orders" value="true" {'checked' if role.can_receive_bar_orders else ''}>
                <label for="can_receive_bar_orders">Отримує замовлення для бару (Бармен)</label> </div>
            <button type="submit">Зберегти зміни</button>
             <a href="/admin/roles" class="button secondary">Скасувати</a>
        </form>
    </div>"""
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["employees_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Редагування ролі", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.post("/admin/edit_role/{role_id}")
async def edit_role(role_id: int, name: str = Form(...), can_manage_orders: Optional[bool] = Form(False), can_be_assigned: Optional[bool] = Form(False), can_serve_tables: Optional[bool] = Form(False), can_receive_kitchen_orders: Optional[bool] = Form(False), can_receive_bar_orders: Optional[bool] = Form(False), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    role = await session.get(Role, role_id)
    if role:
        role.name = name
        role.can_manage_orders = bool(can_manage_orders)
        role.can_be_assigned = bool(can_be_assigned)
        role.can_serve_tables = bool(can_serve_tables)
        role.can_receive_kitchen_orders = bool(can_receive_kitchen_orders)
        role.can_receive_bar_orders = bool(can_receive_bar_orders) # <--- SAVE NEW FIELD
        await session.commit()
    return RedirectResponse(url="/admin/roles", status_code=303)


@app.get("/admin/delete_role/{role_id}")
async def delete_role(role_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    role = await session.get(Role, role_id)
    if role:
        try:
            # Check if any employee uses this role
            employees_exist_res = await session.execute(sa.select(sa.func.count(Employee.id)).where(Employee.role_id == role_id))
            employees_exist = employees_exist_res.scalar_one_or_none() > 0
            if employees_exist:
                logging.warning(f"Attempted to delete role {role_id} which is in use by employees.")
                # Redirect back with an error message (optional)
                return RedirectResponse(url="/admin/roles?error=role_in_use", status_code=303)

            await session.delete(role)
            await session.commit()
        except IntegrityError: # Fallback, though the check above should prevent this
            logging.error(f"IntegrityError deleting role {role_id}, likely still in use.")
            raise HTTPException(status_code=400, detail="Неможливо видалити роль, оскільки до неї прив'язані співробітники.")
        except Exception as e:
             logging.error(f"Error deleting role {role_id}: {e}")
             raise HTTPException(status_code=500, detail="Не вдалося видалити роль.")
    return RedirectResponse(url="/admin/roles", status_code=303)


@app.get("/admin/employees", response_class=HTMLResponse)
async def admin_employees(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    employees_res = await session.execute(sa.select(Employee).options(joinedload(Employee.role)).order_by(Employee.id.desc()))
    employees = employees_res.scalars().all()
    roles_res = await session.execute(sa.select(Role).order_by(Role.id))
    roles = roles_res.scalars().all()

    role_options = "".join([f'<option value="{r.id}">{html.escape(r.name)}</option>' for r in roles])

    rows = "".join([f"""
    <tr>
        <td>{e.id}</td>
        <td>{html.escape(e.full_name)}</td>
        <td>{html.escape(e.phone_number or '-')}</td>
        <td>{html.escape(e.role.name if e.role else 'N/A')}</td>
        <td>{'🟢 На зміні' if e.is_on_shift else '🔴 Вихідний'}</td>
        <td>{e.telegram_user_id or '–'}</td>
        <td class="actions">
            <a href='/admin/edit_employee/{e.id}' class='button-sm'>✏️</a>
            <a href='/admin/delete_employee/{e.id}' onclick="return confirm('Ви впевнені?');" class='button-sm danger'>🗑️</a>
        </td>
    </tr>""" for e in employees])

    if not rows:
        rows = '<tr><td colspan="7">Немає співробітників</td></tr>'

    body = ADMIN_EMPLOYEE_BODY.format(role_options=role_options, rows=rows)
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["employees_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Співробітники", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.post("/admin/add_employee")
async def add_employee(full_name: str = Form(...), phone_number: str = Form(None), role_id: int = Form(...), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    # Basic validation for phone number if provided
    cleaned_phone = re.sub(r'\D', '', phone_number) if phone_number else None
    if cleaned_phone and not (10 <= len(cleaned_phone) <= 15):
         raise HTTPException(status_code=400, detail="Некоректний формат номеру телефону.")

    new_employee = Employee(full_name=full_name, phone_number=cleaned_phone, role_id=role_id)
    session.add(new_employee)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Співробітник з таким номером телефону вже існує.")
    return RedirectResponse(url="/admin/employees", status_code=303)

@app.get("/admin/edit_employee/{employee_id}", response_class=HTMLResponse)
async def get_edit_employee_form(employee_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    employee = await session.get(Employee, employee_id, options=[joinedload(Employee.role)]) # Eager load role
    roles_res = await session.execute(sa.select(Role).order_by(Role.id))
    roles = roles_res.scalars().all()
    if not employee: raise HTTPException(status_code=404, detail="Співробітника не знайдено")

    role_options = "".join([f'<option value="{r.id}" {"selected" if r.id == employee.role_id else ""}>{html.escape(r.name)}</option>' for r in roles])

    body = f"""
    <div class="card">
        <ul class="nav-tabs"><li class="nav-item"><a href="/admin/employees" class="active">Співробітники</a></li><li class="nav-item"><a href="/admin/roles">Ролі</a></li></ul>
        <h2>Редагування співробітника: {html.escape(employee.full_name)}</h2>
        <form action="/admin/edit_employee/{employee_id}" method="post">
            <label for="full_name">Повне ім'я:</label><input type="text" id="full_name" name="full_name" value="{html.escape(employee.full_name)}" required>
            <label for="phone_number">Номер телефону:</label><input type="text" id="phone_number" name="phone_number" value="{html.escape(employee.phone_number or '')}">
            <label for="telegram_user_id">Telegram User ID (не редагується):</label><input type="text" id="telegram_user_id" name="telegram_user_id" value="{employee.telegram_user_id or ''}" disabled>
            <label for="role_id">Роль:</label><select id="role_id" name="role_id" required>{role_options}</select>
            <button type="submit">Зберегти зміни</button>
            <a href="/admin/employees" class="button secondary">Скасувати</a>
        </form>
    </div>"""
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["employees_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Редагування співробітника", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.post("/admin/edit_employee/{employee_id}")
async def edit_employee(employee_id: int, full_name: str = Form(...), phone_number: str = Form(None), role_id: int = Form(...), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    employee = await session.get(Employee, employee_id)
    if employee:
        # Basic validation for phone number if provided
        cleaned_phone = re.sub(r'\D', '', phone_number) if phone_number else None
        if cleaned_phone and not (10 <= len(cleaned_phone) <= 15):
             raise HTTPException(status_code=400, detail="Некоректний формат номеру телефону.")

        employee.full_name = full_name
        employee.phone_number = cleaned_phone
        employee.role_id = role_id
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=400, detail="Співробітник з таким номером телефону вже існує.")
    return RedirectResponse(url="/admin/employees", status_code=303)


@app.get("/admin/delete_employee/{employee_id}")
async def delete_employee(employee_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    employee = await session.get(Employee, employee_id)
    if employee:
        # Prevent deleting if assigned to orders or tables? (Optional check)
        # Check active orders assigned (courier)
        active_orders_count_res = await session.execute(sa.select(func.count(Order.id)).where(Order.courier_id == employee_id, Order.status.has(OrderStatus.is_completed_status == False), Order.status.has(OrderStatus.is_cancelled_status == False) ))
        active_orders_count = active_orders_count_res.scalar_one_or_none() or 0

        # Check assigned tables (waiter) - need to load relationship
        await session.refresh(employee, ['assigned_tables'])
        assigned_tables_count = len(employee.assigned_tables)

        if active_orders_count > 0 or assigned_tables_count > 0:
             logging.warning(f"Attempted to delete employee {employee_id} who has active assignments.")
             # Redirect back with an error message (optional)
             return RedirectResponse(url="/admin/employees?error=employee_assigned", status_code=303)

        await session.delete(employee)
        await session.commit()
    return RedirectResponse(url="/admin/employees", status_code=303)


@app.get("/admin/reports", response_class=HTMLResponse)
async def admin_reports_menu(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)): # Added session
    settings = await get_settings(session)
    body = """
    <div class="card">
        <h2>Доступні звіти</h2>
        <ul>
            <li><a href="/admin/reports/couriers">Звіт по замовленнях кур'єрів</a></li>
            </ul>
    </div>
    """
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "settings_active", "design_active"]}
    active_classes["reports_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Звіти", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.get("/admin/reports/couriers", response_class=HTMLResponse)
async def report_couriers(
    date_from_str: str = Query(None, alias="date_from"),
    date_to_str: str = Query(None, alias="date_to"),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    settings = await get_settings(session)
    report_data = []
    # Default date range to last 7 days including today
    date_to = datetime.strptime(date_to_str, "%Y-%m-%d").date() if date_to_str else date.today()
    date_from = datetime.strptime(date_from_str, "%Y-%m-%d").date() if date_from_str else date_to - timedelta(days=6) # 6 days before + today = 7 days


    completed_status_res = await session.execute(
        sa.select(OrderStatus.id).where(OrderStatus.is_completed_status == True) # Select all completed statuses
    )
    completed_status_ids = completed_status_res.scalars().all()

    if completed_status_ids:
        # Ensure dates are inclusive by adding one day to date_to for the comparison
        date_to_inclusive = date_to + timedelta(days=1)
        report_query = (
            sa.select(
                Employee.full_name,
                func.count(Order.id).label("completed_orders")
            )
            .join(Employee, Order.completed_by_courier_id == Employee.id) # Use completed_by_courier_id
            .where(
                and_(
                    Order.created_at >= date_from, # Use >= for start date
                    Order.created_at < date_to_inclusive, # Use < for end date + 1 day
                    Order.status_id.in_(completed_status_ids) # Check against all completed statuses
                )
            )
            .group_by(Employee.full_name)
            .order_by(func.count(Order.id).desc())
        )
        result = await session.execute(report_query)
        report_data = result.all() # Fetch all results

    report_rows = "".join([f'<tr><td>{html.escape(row.full_name)}</td><td>{row.completed_orders}</td></tr>' for row in report_data])
    if not report_data and (date_from_str or date_to_str): # Show message only if dates were selected
        report_rows = '<tr><td colspan="2">Немає даних за вибраний період.</td></tr>'
    elif not report_data:
         report_rows = '<tr><td colspan="2">Оберіть період та сформуйте звіт (за замовчуванням останні 7 днів).</td></tr>'


    body = ADMIN_REPORTS_BODY.format(
        date_from=date_from.strftime("%Y-%m-%d"),
        date_to=date_to.strftime("%Y-%m-%d"),
        date_from_formatted=date_from.strftime("%d.%m.%Y"),
        date_to_formatted=date_to.strftime("%d.%m.%Y"),
        report_rows=report_rows
    )
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "settings_active", "design_active"]}
    active_classes["reports_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Звіт по кур'єрах", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))



@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)

    current_logo_html = f'<p>Поточне лого: <img src="/{settings.logo_url}" class="table-img"></p>' if settings.logo_url else '<p>Логотип не завантажено.</p>'

    body = ADMIN_SETTINGS_BODY.format(
        client_bot_token="", # os.environ.get('CLIENT_BOT_TOKEN') - НЕ ПОКАЗУВАТИ В HTML!
        admin_bot_token="", # os.environ.get('ADMIN_BOT_TOKEN') - НЕ ПОКАЗУВАТИ В HTML!
        admin_chat_id="",   # os.environ.get('ADMIN_CHAT_ID') - НЕ ПОКАЗУВАТИ В HTML!
        current_logo_html=current_logo_html,
        cache_buster=secrets.token_hex(4) # Add cache buster for favicons
    )
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "design_active"]}
    active_classes["settings_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Налаштування", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

# --- Функція save_admin_settings оновлена ---
@app.post("/admin/settings")
async def save_admin_settings(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials),
                               logo_file: UploadFile = File(None), 
                               apple_touch_icon: UploadFile = File(None), favicon_32x32: UploadFile = File(None),
                               favicon_16x16: UploadFile = File(None), favicon_ico: UploadFile = File(None),
                               site_webmanifest: UploadFile = File(None)):
    settings = await get_settings(session)
    
    if logo_file and logo_file.filename:
        if settings.logo_url and os.path.exists(settings.logo_url):
            try:
                os.remove(settings.logo_url)
            except OSError as e:
                 logging.error(f"Не вдалося видалити старе лого {settings.logo_url}: {e}")
        filename_base, file_ext = os.path.splitext(logo_file.filename)
        safe_filename = secrets.token_hex(8) + file_ext
        path = os.path.join("static/images", safe_filename)

        try:
            async with aiofiles.open(path, 'wb') as f: await f.write(await logo_file.read())
            settings.logo_url = path
        except Exception as e:
            logging.error(f"Не вдалося зберегти лого {path}: {e}")


    favicon_dir = "static/favicons"
    os.makedirs(favicon_dir, exist_ok=True)

    favicon_files = {
        "apple-touch-icon.png": apple_touch_icon,
        "favicon-32x32.png": favicon_32x32,
        "favicon-16x16.png": favicon_16x16,
        "favicon.ico": favicon_ico,
        "site.webmanifest": site_webmanifest,
    }

    for filename, file in favicon_files.items():
        if file and file.filename: # Check if file was uploaded
            path = os.path.join(favicon_dir, filename) # Use the correct, fixed filename
            try:
                # Overwrite existing file
                async with aiofiles.open(path, 'wb') as f:
                    await f.write(await file.read())
                logging.info(f"Збережено favicon: {path}")
            except Exception as e:
                logging.error(f"Не вдалося зберегти favicon {filename}: {e}")

    await session.commit()
    return RedirectResponse(url="/admin/settings?saved=true", status_code=303)
# --- КІНЕЦЬ save_admin_settings ---


# --- Функція get_settings оновлена ---
async def get_settings(session: AsyncSession) -> Settings:
    settings = await session.get(Settings, 1) # Use get() for primary key lookup
    if not settings:
        settings = Settings(id=1) # Initialize with ID
        session.add(settings)
        try:
            await session.commit()
            await session.refresh(settings) # Refresh to get default values if any
            logging.info("Створено початкові налаштування в БД.")
        except Exception as e:
            logging.error(f"Не вдалося створити початкові налаштування: {e}")
            await session.rollback()
            return Settings(id=1) # Return an empty settings object
    
    # NEW: Populate default welcome message if empty
    if not settings.telegram_welcome_message:
        settings.telegram_welcome_message = f"Шановний {{user_name}}, ласкаво просимо! 👋\n\nМи раді вас бачити. Оберіть опцію:"

    return settings
# --- КІНЕЦЬ get_settings ---

@app.get("/api/admin/products", response_class=JSONResponse)
async def api_get_products(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    res = await session.execute(
        sa.select(Product.id, Product.name, Product.price, Category.name.label("category"))
        .join(Category, Product.category_id == Category.id, isouter=True) # Use outer join in case category is missing
        .where(Product.is_active == True)
        .order_by(Category.sort_order, Product.name)
    )
    products = [{"id": row.id, "name": row.name, "price": row.price, "category": row.category or "Без категорії"} for row in res.mappings().all()]
    return JSONResponse(content=products)

@app.get("/admin/order/new", response_class=HTMLResponse)
async def get_add_order_form(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)): # Added session
    settings = await get_settings(session)
    initial_data = {
        "items": {},
        "action": "/api/admin/order/new",
        "submit_text": "Створити замовлення",
        "form_values": None
    }
    script_data_injection = f"""
    <script>
        document.addEventListener('DOMContentLoaded', () => {{
            if (typeof window.initializeForm === 'function' && !window.orderFormInitialized) {{
                 window.initializeForm({json.dumps(initial_data)});
                 window.orderFormInitialized = true;
             }} else if (!window.initializeForm) {{
                 document.addEventListener('formScriptLoaded', () => {{
                     if (!window.orderFormInitialized) {{
                         window.initializeForm({json.dumps(initial_data)});
                         window.orderFormInitialized = true;
                     }}
                 }});
             }}
        }});
    </script>
    """
    body = ADMIN_ORDER_FORM_BODY + script_data_injection
    active_classes = {key: "" for key in ["main_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["orders_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Нове замовлення", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))

@app.get("/admin/order/edit/{order_id}", response_class=HTMLResponse)
async def get_edit_order_form(order_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await get_settings(session)
    order = await session.get(Order, order_id)
    if not order: raise HTTPException(404, "Замовлення не знайдено")

    products_dict = parse_products_string(order.products)
    initial_items = {}
    if products_dict:
        products_res = await session.execute(sa.select(Product).where(Product.name.in_(list(products_dict.keys()))))
        db_products_map = {p.name: p for p in products_res.scalars().all()}
        for name, quantity in products_dict.items():
            if p := db_products_map.get(name):
                initial_items[p.id] = {"name": p.name, "price": p.price, "quantity": quantity}

    initial_data = {
        "items": initial_items,
        "action": f"/api/admin/order/edit/{order_id}",
        "submit_text": "Зберегти зміни",
        "form_values": {
            "phone_number": order.phone_number or "",
            "customer_name": order.customer_name or "",
            "is_delivery": order.is_delivery,
            "address": order.address or ""
        }
    }
    script_injection = f"""
    <script>
         document.addEventListener('DOMContentLoaded', () => {{
             if (typeof window.initializeForm === 'function' && !window.orderFormInitialized) {{
                 window.initializeForm({json.dumps(initial_data)});
                 window.orderFormInitialized = true;
             }} else if (!window.initializeForm) {{
                 document.addEventListener('formScriptLoaded', () => {{
                     if (!window.orderFormInitialized) {{
                         window.initializeForm({json.dumps(initial_data)});
                         window.orderFormInitialized = true;
                     }}
                 }});
             }}
         }});
    </script>
    """
    body = ADMIN_ORDER_FORM_BODY + script_injection
    active_classes = {key: "" for key in ["main_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["orders_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title=f"Редагування замовлення #{order.id}", 
        body=body, 
        site_title=settings.site_title or "Назва",
        **active_classes
    ))


async def _process_and_save_order(order: Order, data: dict, session: AsyncSession):
    """
    Обробляє та зберігає дані замовлення, отримані з веб-інтерфейсу адміністратора.
    """
    is_new_order = order.id is None
    actor_info = "Адміністративна панель"

    order.customer_name = data.get("customer_name")
    order.phone_number = data.get("phone_number")
    order.is_delivery = data.get("delivery_type") == "delivery"
    order.address = data.get("address") if order.is_delivery else None
    order.order_type = "delivery" if order.is_delivery else "pickup"


    items_from_js = data.get("items", {})

    if not items_from_js:
        order.products = ""
        order.total_price = 0
    else:
        valid_product_ids = [int(pid) for pid in items_from_js.keys() if pid.isdigit()]

        if not valid_product_ids:
            order.products = ""
            order.total_price = 0
        else:
            products_res = await session.execute(sa.select(Product).where(Product.id.in_(valid_product_ids)))
            db_products_map = {p.id: p for p in products_res.scalars().all()}

            total_price = 0
            product_strings = []

            for pid_str, item_data in items_from_js.items():
                if not pid_str.isdigit(): continue # Skip non-numeric keys
                pid = int(pid_str)
                if product := db_products_map.get(pid):
                    try:
                         quantity = int(item_data.get('quantity', 0))
                    except (ValueError, TypeError):
                         quantity = 0 # Default to 0 if quantity is invalid

                    if quantity > 0:
                        total_price += product.price * quantity
                        product_strings.append(f"{product.name} x {quantity}")

            order.products = ", ".join(product_strings)
            order.total_price = total_price

    if is_new_order:
        session.add(order)
        if not order.status_id:
            new_status_res = await session.execute(sa.select(OrderStatus.id).where(OrderStatus.name == "Новий").limit(1))
            new_status_id = new_status_res.scalar_one_or_none() or 1 # Default to 1 if not found
            order.status_id = new_status_id


    await session.commit() # Commit changes for both new and existing orders
    await session.refresh(order) # Refresh to get ID and updated timestamp

    if is_new_order:
        try:
             history_entry = OrderStatusHistory(
                 order_id=order.id, # Use the generated ID
                 status_id=order.status_id,
                 actor_info=actor_info
             )
             session.add(history_entry)
             await session.commit() # Commit history separately
             logging.info(f"Створено запис історії для нового замовлення #{order.id}")
        except Exception as e:
             logging.error(f"Не вдалося створити запис історії для замовлення #{order.id}: {e}")
             await session.rollback() # Rollback history commit if it fails


        admin_bot = dp_admin.get("bot_instance")
        if admin_bot:
            await notify_new_order_to_staff(admin_bot, order, session)
    else:
         logging.info(f"Замовлення #{order.id} оновлено через веб-панель.")


@app.post("/api/admin/order/new", response_class=JSONResponse)
async def api_create_order(request: Request, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Недійсний JSON")

    new_order = Order() # Create an empty order object
    try:
        await _process_and_save_order(new_order, data, session)
        return JSONResponse(content={"message": "Замовлення створено успішно", "redirect_url": "/admin/orders"})
    except Exception as e:
        logging.error(f"Помилка при створенні замовлення через API: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не вдалося створити замовлення")

@app.post("/api/admin/order/edit/{order_id}", response_class=JSONResponse)
async def api_update_order(order_id: int, request: Request, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Недійсний JSON")

    order = await session.get(Order, order_id)
    if not order: raise HTTPException(404, "Замовлення не знайдено")
    try:
        await _process_and_save_order(order, data, session)
        return JSONResponse(content={"message": "Замовлення оновлено успішно", "redirect_url": "/admin/orders"})
    except Exception as e:
        logging.error(f"Помилка при оновленні замовлення #{order_id} через API: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не вдалося оновити замовлення")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)