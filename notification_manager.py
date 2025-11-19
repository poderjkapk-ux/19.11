# notification_manager.py
import logging
import os
from aiogram import Bot, html
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import Order, Settings, OrderStatus, Employee, Role, Product

logger = logging.getLogger(__name__)

def _parse_products_str(products_str: str) -> dict:
    """
    Парсить рядок продуктів у словник {'Назва': кількість}.
    Формат: 'Назва x 1, Назва 2 x 2'
    """
    if not products_str:
        return {}
    result = {}
    for part in products_str.split(", "):
        try:
            if " x " in part:
                name, qty = part.rsplit(" x ", 1)
                result[name.strip()] = int(qty)
        except ValueError:
            continue
    return result

async def notify_new_order_to_staff(admin_bot: Bot, order: Order, session: AsyncSession):
    """
    Надсилає сповіщення про НОВЕ замовлення в загальний чат, операторам, поварам та барменам.
    Використовується для веб-доставки та замовлень, створених офіціантом.
    """
    admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')
    
    await session.refresh(order, ['status'])
    is_delivery = order.is_delivery # Визначаємо тип замовлення

    # Генеруємо текст та клавіатуру для керування (для оператора/адміна)
    status_name = order.status.name if order.status else 'Невідомий'
    delivery_info = f"Адреса: {html.quote(order.address or 'Не вказана')}" if is_delivery else 'Самовивіз'
    time_info = f"Час: {html.quote(order.delivery_time)}"
    source = f"Джерело: {'Веб-сайт' if order.user_id is None else 'Telegram-бот'}"
    products_formatted = "- " + html.quote(order.products or '').replace(", ", "\n- ")
    
    admin_text = (f"<b>Замовлення #{order.id}</b> ({source})\n\n"
                  f"<b>Клієнт:</b> {html.quote(order.customer_name)}\n<b>Телефон:</b> {html.quote(order.phone_number)}\n"
                  f"<b>{delivery_info}</b>\n<b>{time_info}</b>\n\n"
                  f"<b>Страви:</b>\n{products_formatted}\n\n"
                  f"<b>Сума:</b> {order.total_price} грн\n\n"
                  f"<b>Статус:</b> {status_name}")

    # --- КЛАВІАТУРА ДЛЯ ОПЕРАТОРА ---
    kb_admin = InlineKeyboardBuilder()
    statuses_res = await session.execute(
        select(OrderStatus).where(OrderStatus.visible_to_operator == True).order_by(OrderStatus.id)
    )
    status_buttons = [
        InlineKeyboardButton(text=s.name, callback_data=f"change_order_status_{order.id}_{s.id}")
        for s in statuses_res.scalars().all()
    ]
    for i in range(0, len(status_buttons), 2):
        kb_admin.row(*status_buttons[i:i+2])
    kb_admin.row(InlineKeyboardButton(text="👤 Призначити кур'єра", callback_data=f"select_courier_{order.id}"))
    kb_admin.row(InlineKeyboardButton(text="✏️ Редагувати замовлення", callback_data=f"edit_order_{order.id}"))
    # --------------------------------------------------------

    # 1. Відправка в загальний адмін-чат та операторам
    target_chat_ids = set()
    if admin_chat_id_str:
        try:
            target_chat_ids.add(int(admin_chat_id_str))
        except ValueError:
            logger.warning(f"Некоректний ADMIN_CHAT_ID: {admin_chat_id_str}")

    operator_roles_res = await session.execute(select(Role.id).where(Role.can_manage_orders == True))
    operator_role_ids = operator_roles_res.scalars().all()

    operators_on_shift_res = await session.execute(
        select(Employee).where(
            Employee.role_id.in_(operator_role_ids),
            Employee.is_on_shift == True,
            Employee.telegram_user_id.is_not(None)
        )
    )
    for operator in operators_on_shift_res.scalars().all():
        if operator.telegram_user_id not in target_chat_ids:
            target_chat_ids.add(operator.telegram_user_id)
            
    for chat_id in target_chat_ids:
        try:
            await admin_bot.send_message(chat_id, admin_text, reply_markup=kb_admin.as_markup())
        except Exception as e:
            logger.error(f"Не вдалося відправити нове замовлення оператору/адміну {chat_id}: {e}")

    # 2. РОЗПОДІЛ НА ВИРОБНИЦТВО (Кухня/Бар)
    # Перевіряємо налаштування статусу перед відправкою
    if order.status and order.status.requires_kitchen_notify:
        await distribute_order_to_production(admin_bot, order, session)
    else:
        logger.info(f"Замовлення #{order.id} НЕ відправлено на виробництво (налаштування статусу '{order.status.name}').")


async def distribute_order_to_production(bot: Bot, order: Order, session: AsyncSession):
    """
    Розподіляє товари замовлення між Кухнею та Баром і надсилає відповідним працівникам.
    Викликається з place_in_house_order та notify_new_order_to_staff.
    """
    # 1. Парсимо товари
    products_map = _parse_products_str(order.products)
    if not products_map:
        return

    # 2. Отримуємо деталі товарів з БД (щоб знати preparation_area)
    products_res = await session.execute(select(Product))
    all_products = products_res.scalars().all()
    
    # Словник для швидкого пошуку продукту за "чистою" назвою
    db_products = {p.name.strip(): p for p in all_products}

    kitchen_items = []
    bar_items = []

    for name, qty in products_map.items():
        product = db_products.get(name.strip())
        
        if product:
            item_str = f"- {html.quote(name)} x {qty}"
            if product.preparation_area == 'bar':
                bar_items.append(item_str)
            else:
                # За замовчуванням або якщо kitchen
                kitchen_items.append(item_str)
        else:
            # Якщо продукт не знайдено в БД, відправляємо на кухню як fallback
            kitchen_items.append(f"- {html.quote(name)} x {qty}")

    # 3. Відправляємо на Кухню
    if kitchen_items:
        await send_group_notification(
            bot=bot,
            order=order,
            items=kitchen_items,
            role_filter=Role.can_receive_kitchen_orders == True,
            title="🧑‍🍳 ЗАМОВЛЕННЯ НА КУХНЮ",
            session=session,
            area="kitchen" # <-- Додано маркер цеху
        )

    # 4. Відправляємо на Бар
    if bar_items:
        await send_group_notification(
            bot=bot,
            order=order,
            items=bar_items,
            role_filter=Role.can_receive_bar_orders == True,
            title="🍹 ЗАМОВЛЕННЯ НА БАР",
            session=session,
            area="bar" # <-- Додано маркер цеху
        )


async def send_group_notification(bot: Bot, order: Order, items: list, role_filter, title: str, session: AsyncSession, area: str = "kitchen"):
    """
    Універсальна функція для відправки чека групі співробітників (повари або бармени).
    """
    # Шукаємо ролі
    roles_res = await session.execute(select(Role.id).where(role_filter))
    role_ids = roles_res.scalars().all()

    if not role_ids:
        return

    # Шукаємо працівників на зміні
    employees_res = await session.execute(
        select(Employee).where(
            Employee.role_id.in_(role_ids),
            Employee.is_on_shift == True,
            Employee.telegram_user_id.is_not(None)
        )
    )
    employees = employees_res.scalars().all()

    if employees:
        is_delivery = order.is_delivery
        items_formatted = "\n".join(items)
        
        table_info = ""
        if order.order_type == 'in_house' and order.table_id:
            if 'table' not in order.__dict__:
                await session.refresh(order, ['table'])
            if order.table:
                table_info = f" (Стіл: {html.quote(order.table.name)})"
        
        text = (f"{title}: <b>#{order.id}</b>{table_info}\n"
                f"<b>Тип:</b> {'Доставка' if is_delivery else 'В закладі / Самовивіз'}\n"
                f"<b>Час:</b> {html.quote(order.delivery_time)}\n\n"
                f"<b>СКЛАД:</b>\n{items_formatted}\n\n"
                f"<i>Натисніть 'Видача', коли буде готове.</i>")
        
        kb = InlineKeyboardBuilder()
        # Додаємо area (kitchen/bar) в callback_data
        kb.row(InlineKeyboardButton(text=f"✅ Видача #{order.id}", callback_data=f"chef_ready_{order.id}_{area}"))
        
        for emp in employees:
            try:
                await bot.send_message(emp.telegram_user_id, text, reply_markup=kb.as_markup())
            except Exception as e:
                logger.error(f"Не вдалося відправити замовлення працівнику {emp.id}: {e}")


async def notify_all_parties_on_status_change(
    order: Order,
    old_status_name: str,
    actor_info: str,
    admin_bot: Bot,
    client_bot: Bot | None,
    session: AsyncSession
):
    """
    Централізована функція для надсилання всіх сповіщень при зміні статусу.
    """
    await session.refresh(order, ['status', 'courier', 'accepted_by_waiter', 'table'])
    admin_chat_id_str = os.environ.get('ADMIN_CHAT_ID')
    
    new_status = order.status
    
    # 1. Сповіщення в головний АДМІН-ЧАТ (Лог)
    if admin_chat_id_str:
        log_message = (
            f"🔄 <b>[Статус змінено]</b> Замовлення #{order.id}\n"
            f"<b>Ким:</b> {html.quote(actor_info)}\n"
            f"<b>Статус:</b> `{html.quote(old_status_name)}` → `{html.quote(new_status.name)}`"
        )
        try:
            await admin_bot.send_message(admin_chat_id_str, log_message)
        except Exception as e:
            logger.error(f"Не вдалося відправити лог в адмін-чат: {e}")

    # 2. ЛОГІКА ДЛЯ ВИРОБНИЦТВА (Кухня/Бар)
    # Перевіряємо, чи вимагає новий статус відправки на кухню
    if new_status.requires_kitchen_notify:
        # Відправляємо розподілене замовлення
        await distribute_order_to_production(admin_bot, order, session)

    # 3. СПОВІЩЕННЯ ПІД ЧАС ВИДАЧІ ("Готовий до видачі")
    if new_status.name == "Готовий до видачі":
        # --- ВИЗНАЧЕННЯ ДЖЕРЕЛА (Хто приготував?) ---
        source_label = ""
        if "Кухня" in actor_info or "Повар" in actor_info:
            source_label = " (🍳 КУХНЯ)"
        elif "Бар" in actor_info or "Бармен" in actor_info:
            source_label = " (🍹 БАР)"
        
        ready_message = f"📢 <b>ГОТОВО ДО ВИДАЧІ{source_label}: #{order.id}</b>! \n"
        
        target_employees = []
        # Якщо є офіціант (для замовлення в закладі)
        if order.order_type == 'in_house' and order.accepted_by_waiter and order.accepted_by_waiter.is_on_shift:
            target_employees.append(order.accepted_by_waiter)
            ready_message += f"Стіл: {html.quote(order.table.name if order.table else 'N/A')}. Прийняв: {html.quote(order.accepted_by_waiter.full_name)}"
        
        # Якщо є кур'єр (для доставки)
        if order.is_delivery and order.courier and order.courier.is_on_shift:
            target_employees.append(order.courier)
            ready_message += f"Призначений кур'єр: {html.quote(order.courier.full_name)}"

        # Якщо нікого немає, сповіщаємо операторів
        if not target_employees:
             operator_roles_res = await session.execute(select(Role.id).where(Role.can_manage_orders == True))
             operator_role_ids = operator_roles_res.scalars().all()
             operators_on_shift_res = await session.execute(
                 select(Employee).where(
                     Employee.role_id.in_(operator_role_ids),
                     Employee.is_on_shift == True,
                     Employee.telegram_user_id.is_not(None)
                 )
             )
             target_employees.extend(operators_on_shift_res.scalars().all())
             ready_message += f"Тип: {'Самовивіз' if order.order_type == 'pickup' else 'Доставка'}. Потрібна видача."
             
        for employee in target_employees:
            if employee.telegram_user_id:
                try:
                    await admin_bot.send_message(employee.telegram_user_id, ready_message)
                except Exception as e:
                    logger.error(f"Не вдалося сповістити {employee.telegram_user_id} про готовність: {e}")

    # 4. Сповіщення призначеному КУР'ЄРУ (про інші зміни статусу)
    if order.courier and order.courier.telegram_user_id and "Кур'єр" not in actor_info and new_status.name != "Готовий до видачі":
        if new_status.visible_to_courier: # Тільки якщо статус видимий кур'єру
            courier_text = f"❗️ Статус вашого замовлення #{order.id} змінено на: <b>{new_status.name}</b>"
            try:
                await admin_bot.send_message(order.courier.telegram_user_id, courier_text)
            except Exception: pass

    # 5. Сповіщення призначеному ОФІЦІАНТУ (про інші зміни статусу)
    if order.order_type != 'delivery' and order.accepted_by_waiter and order.accepted_by_waiter.telegram_user_id and "Офіціант" not in actor_info and new_status.name != "Готовий до видачі":
        waiter_text = f"📢 Замовлення #{order.id} (Стіл: {html.quote(order.table.name if order.table else 'N/A')}) має новий статус: <b>{new_status.name}</b>"
        try:
            await admin_bot.send_message(order.accepted_by_waiter.telegram_user_id, waiter_text)
        except Exception: pass

    # 6. Сповіщення КЛІЄНТУ
    if new_status.notify_customer and order.user_id and client_bot:
        client_text = f"Статус вашого замовлення #{order.id} змінено на: <b>{new_status.name}</b>"
        try:
            await client_bot.send_message(order.user_id, client_text)
        except Exception as e:
            logger.error(f"Не вдалося сповістити клієнта {order.user_id}: {e}")