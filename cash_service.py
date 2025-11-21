# cash_service.py

import logging
from datetime import datetime
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from sqlalchemy.orm import joinedload
from models import CashShift, CashTransaction, Order, Employee

logger = logging.getLogger(__name__)

async def get_open_shift(session: AsyncSession, employee_id: int) -> CashShift | None:
    """Повертає відкриту зміну співробітника або None."""
    result = await session.execute(
        select(CashShift).where(
            CashShift.employee_id == employee_id,
            CashShift.is_closed == False
        )
    )
    return result.scalars().first()

async def get_any_open_shift(session: AsyncSession) -> CashShift | None:
    """Повертає першу ліпшу відкриту зміну (для веб-адмінки)."""
    result = await session.execute(
        select(CashShift).where(CashShift.is_closed == False).limit(1)
    )
    return result.scalars().first()

async def open_new_shift(session: AsyncSession, employee_id: int, start_cash: float) -> CashShift:
    """Відкриває нову касову зміну."""
    active_shift = await get_open_shift(session, employee_id)
    if active_shift:
        raise ValueError("У цього співробітника вже є відкрита зміна.")

    new_shift = CashShift(
        employee_id=employee_id,
        start_time=datetime.now(),
        start_cash=Decimal(str(start_cash)),
        is_closed=False
    )
    session.add(new_shift)
    await session.commit()
    await session.refresh(new_shift)
    return new_shift

async def link_order_to_shift(session: AsyncSession, order: Order, employee_id: int | None):
    """
    Прив'язує замовлення до відкритої зміни.
    Це важливо для статистики продажів (Z-звіт).
    """
    if order.cash_shift_id:
        return 

    shift = None
    # Якщо це касир/оператор закриває замовлення, шукаємо його зміну
    if employee_id:
        shift = await get_open_shift(session, employee_id)
    
    if not shift:
        # Якщо не знайдено, беремо будь-яку активну зміну
        shift = await get_any_open_shift(session)
    
    if shift:
        order.cash_shift_id = shift.id
        # session.commit() робитиме викликаючий код
        logger.info(f"Замовлення #{order.id} прив'язано до зміни #{shift.id} для статистики.")
    else:
        logger.warning(f"УВАГА: Замовлення #{order.id} не прив'язано до зміни (немає відкритих змін).")

async def register_employee_debt(session: AsyncSession, order: Order, employee_id: int):
    """
    Фіксує, що співробітник (кур'єр/офіціант) отримав готівку за замовлення.
    Збільшує його баланс (борг перед касою).
    """
    if order.payment_method != 'cash':
        return # Борг виникає тільки при готівці

    employee = await session.get(Employee, employee_id)
    if not employee:
        logger.error(f"Співробітника {employee_id} не знайдено при реєстрації боргу.")
        return

    # Перетворюємо в Decimal для точності
    amount = Decimal(order.total_price)
    
    # Оновлюємо баланс співробітника
    employee.cash_balance = Decimal(employee.cash_balance) + amount
    
    # Позначаємо, що гроші за це замовлення ще не в касі
    order.is_cash_turned_in = False
    
    logger.info(f"Співробітник {employee.full_name} отримав {amount} грн за замовлення #{order.id}. Поточний борг: {employee.cash_balance}")

async def process_handover(session: AsyncSession, cashier_shift_id: int, employee_id: int, order_ids: list[int]):
    """
    Касир приймає гроші від співробітника за конкретні замовлення.
    """
    shift = await session.get(CashShift, cashier_shift_id)
    if not shift or shift.is_closed:
        raise ValueError("Зміна касира не знайдена або закрита.")

    employee = await session.get(Employee, employee_id)
    if not employee:
        raise ValueError("Співробітника не знайдено.")

    orders_res = await session.execute(
        select(Order).where(Order.id.in_(order_ids), Order.is_cash_turned_in == False)
    )
    orders = orders_res.scalars().all()

    if not orders:
        raise ValueError("Немає доступних замовлень для здачі виручки.")

    total_amount = Decimal(0)
    
    for order in orders:
        # Перевіряємо, чи дійсно це замовлення цього співробітника (завершене ним або він кур'єр)
        # Для спрощення, якщо касир обрав ці ID, ми довіряємо вибору
        
        amount = Decimal(order.total_price)
        total_amount += amount
        
        order.is_cash_turned_in = True
        # Прив'язуємо замовлення до зміни касира, який прийняв гроші (якщо ще не прив'язано)
        if not order.cash_shift_id:
            order.cash_shift_id = shift.id

    # Зменшуємо борг співробітника
    employee.cash_balance = Decimal(employee.cash_balance) - total_amount
    if employee.cash_balance < 0:
        employee.cash_balance = Decimal(0) # Захист від мінуса

    # Додаємо транзакцію в касу
    tx = CashTransaction(
        shift_id=shift.id,
        amount=total_amount,
        transaction_type='handover',
        comment=f"Здача виручки: {employee.full_name} (Зам: {', '.join(map(str, order_ids))})"
    )
    session.add(tx)
    
    # Оновлюємо статистику зміни ("Внесення" або окреме поле, тут додамо до service_in для простоти, або краще рахувати окремо при get_shift_statistics)
    # В цій моделі ми рахуємо статистику "на льоту" в get_shift_statistics, тому тут просто зберігаємо транзакцію.
    
    await session.commit()
    return total_amount

async def get_shift_statistics(session: AsyncSession, shift_id: int):
    """Рахує статистику зміни (X-звіт)."""
    shift = await session.get(CashShift, shift_id)
    if not shift:
        return None

    # 1. Продажі (замовлення, закриті безпосередньо в цю зміну або прив'язані до неї)
    # УВАГА: Для готівки ми враховуємо ТІЛЬКИ ті, що `is_cash_turned_in == True` І прив'язані до цієї зміни?
    # АБО ми рахуємо `handover` транзакції як прихід готівки.
    # Найкращий підхід: 
    #  - Продажі (Total Sales) - це сума всіх чеків за зміну (статистика бізнесу).
    #  - Готівка в касі (Cash Drawer) - це стартова + прямі оплати + здача виручки - вилучення.

    # Отримуємо всі замовлення, прив'язані до цієї зміни
    sales_query = select(
        Order.payment_method,
        func.sum(Order.total_price)
    ).where(
        Order.cash_shift_id == shift_id
    ).group_by(Order.payment_method)

    sales_res = await session.execute(sales_query)
    sales_data = sales_res.all()

    total_sales_cash_orders = Decimal(0) # Готівкові замовлення (всього продано)
    total_card_sales = Decimal(0)

    for method, amount in sales_data:
        amount_decimal = Decimal(amount) if amount else Decimal(0)
        if method == 'cash':
            total_sales_cash_orders += amount_decimal
        elif method == 'card':
            total_card_sales += amount_decimal

    # 2. Службові операції та ЗДАЧА ВИРУЧКИ
    trans_query = select(
        CashTransaction.transaction_type,
        func.sum(CashTransaction.amount)
    ).where(
        CashTransaction.shift_id == shift_id
    ).group_by(CashTransaction.transaction_type)

    trans_res = await session.execute(trans_query)
    trans_data = trans_res.all()

    service_in = Decimal(0)
    service_out = Decimal(0)
    handover_in = Decimal(0) # Гроші, здані кур'єрами/офіціантами

    for t_type, amount in trans_data:
        amount_decimal = Decimal(amount) if amount else Decimal(0)
        if t_type == 'in':
            service_in += amount_decimal
        elif t_type == 'out':
            service_out += amount_decimal
        elif t_type == 'handover':
            handover_in += amount_decimal

    # 3. Готівка, отримана "на пряму" (наприклад, якщо касир сам закрив замовлення)
    # Логіка: Якщо замовлення `cash` і `cash_shift_id == shift.id`, то чи це гроші в касі?
    # Якщо `is_cash_turned_in == True` - значить гроші в касі.
    # Якщо гроші прийшли через `handover`, вони вже враховані в `handover_in`.
    # Нам треба знайти замовлення, які оплачені готівкою ПРЯМО В КАСУ (без кур'єра), або де касир був виконавцем.
    # АЛЕ, для спрощення, ми можемо вважати, що `handover` покриває кур'єрів.
    # А прямі продажі (якщо касир сам продав) треба додати.
    # Спрощена модель: Готівка в касі = Start + Handover (від інших) + Service In - Service Out + (Direct Cash Sales).
    
    # Direct Cash Sales = Total Cash Sales (змінна вище) - (сума замовлень, які пройшли через Handover).
    # Це може бути складно порахувати точно без додаткових полів.
    
    # АЛЬТЕРНАТИВНИЙ (НАДІЙНІШИЙ) МЕТОД РОЗРАХУНКУ ГОТІВКИ В КАСІ:
    # Cash In Drawer = Start + Transactions(in) + Transactions(handover) - Transactions(out) 
    # + Замовлення, які: (cash_shift_id == this_shift) AND (is_cash_turned_in == True) AND (НЕМАЄ транзакції handover для них???)
    
    # Давайте зробимо так: 
    # Якщо касир закриває замовлення сам -> is_cash_turned_in стає True, але Handover транзакція НЕ створюється (бо він сам собі не здає).
    # Тоді нам треба знайти суму замовлень (cash, is_turned_in=True, shift=this), які НЕ були частиною Handover.
    # Це складно.
    
    # ПРОСТІШЕ:
    # Вважаємо, що будь-яке закриття замовлення готівкою, прив'язане до зміни, додає гроші в касу, ЯКЩО це не борг.
    # Якщо це борг (кур'єр), то гроші прийдуть пізніше через Handover.
    # Отже:
    # Теоретична готівка = Start 
    # + (Orders CASH де is_cash_turned_in=True і cash_shift_id=this_shift)
    # + Service In
    # - Service Out
    # А Handover транзакції ми використовуємо тільки для логування? Ні, вони дублюватимуть суму.
    
    # РІШЕННЯ:
    # Ми будемо використовувати `CashTransaction` типу `handover` як джерело правди про прихід від кур'єрів.
    # А прямі продажі касира (де він сам прийняв гроші) треба додати.
    # Як їх розрізнити? 
    # В `register_employee_debt` ми ставимо `is_cash_turned_in = False`.
    # Якщо касир закриває замовлення (наприклад, самовивіз), ми повинні ставити `is_cash_turned_in = True` відразу.
    # І ці гроші повинні плюсуватися.
    
    # Давайте перерахуємо так:
    # 1. Гроші від прямих продажів (Самовивіз / Офіціант розрахував на касі):
    #    Це замовлення, де `is_cash_turned_in = True`, `payment_method='cash'`, `cash_shift_id = shift.id`.
    #    Але сюди потраплять і ті, що здані через Handover.
    # 2. Щоб не двоїти, ми при Handover НЕ змінюємо `cash_shift_id` на зміну касира? Ні, треба міняти.
    
    # ДАВАЙТЕ ТАК:
    # Cash Drawer = Start + Service In - Service Out + SUM(Order.total where shift=this AND cash AND turned_in=True).
    # Транзакції `handover` ігноруємо в математиці балансу, вони просто показують факт передачі.
    # Чому? Тому що коли ми робимо Handover, ми ставимо `is_cash_turned_in = True` і `cash_shift_id = active_shift`.
    # Отже, ці замовлення потраплять в суму `SUM(...)`.
    # А прямі продажі касира відразу стають `turned_in=True` і теж потрапляють в суму.
    # Все сходиться!
    
    query_collected_cash = select(func.sum(Order.total_price)).where(
        Order.cash_shift_id == shift_id,
        Order.payment_method == 'cash',
        Order.is_cash_turned_in == True
    )
    collected_cash_res = await session.execute(query_collected_cash)
    total_collected_cash_orders = collected_cash_res.scalar() or Decimal(0)

    start_cash_decimal = shift.start_cash if shift.start_cash is not None else Decimal(0)
    
    theoretical_cash = start_cash_decimal + total_collected_cash_orders + service_in - service_out

    return {
        "shift_id": shift.id,
        "start_time": shift.start_time,
        "start_cash": start_cash_decimal,
        "total_sales_cash": total_sales_cash_orders, # Продажі за зміну (включно з погашеними боргами)
        "total_sales_card": total_card_sales,
        "total_sales": total_sales_cash_orders + total_card_sales,
        "service_in": service_in,
        "service_out": service_out,
        "handover_in": handover_in, # Просто для інфо
        "theoretical_cash": theoretical_cash
    }

async def close_active_shift(session: AsyncSession, shift_id: int, end_cash_actual: float):
    """Закриває зміну (Z-звіт)."""
    shift = await session.get(CashShift, shift_id)
    if not shift or shift.is_closed:
        raise ValueError("Зміна не знайдена або вже закрита.")

    stats = await get_shift_statistics(session, shift_id)
    
    shift.end_time = datetime.now()
    shift.end_cash_actual = Decimal(str(end_cash_actual))
    
    shift.total_sales_cash = stats['total_sales_cash']
    shift.total_sales_card = stats['total_sales_card']
    shift.service_in = stats['service_in']
    shift.service_out = stats['service_out']
    shift.is_closed = True
    
    await session.commit()
    return shift

async def add_shift_transaction(session: AsyncSession, shift_id: int, amount: float, t_type: str, comment: str):
    """Додає транзакцію."""
    tx = CashTransaction(
        shift_id=shift_id,
        amount=Decimal(str(amount)),
        transaction_type=t_type,
        comment=comment
    )
    session.add(tx)
    await session.commit()