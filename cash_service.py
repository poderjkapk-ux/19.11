# cash_service.py

import logging
from datetime import datetime
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from models import CashShift, CashTransaction, Order

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

    # Перетворюємо start_cash на Decimal для сумісності з БД
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
    Прив'язує замовлення до відкритої зміни співробітника.
    Якщо employee_id=None (наприклад, через адмінку), шукає будь-яку відкриту зміну.
    """
    if order.cash_shift_id:
        return # Вже прив'язано

    shift = None
    if employee_id:
        shift = await get_open_shift(session, employee_id)
    
    if not shift:
        # Якщо у цього співробітника немає зміни, або це адмін через сайт,
        # шукаємо будь-яку активну зміну, щоб гроші "впали" в касу.
        shift = await get_any_open_shift(session)
    
    if shift:
        order.cash_shift_id = shift.id
        # session.commit() робитиме той, хто викликав цю функцію
        logger.info(f"Замовлення #{order.id} прив'язано до зміни #{shift.id}")
    else:
        logger.warning(f"УВАГА: Замовлення #{order.id} оплачено, але немає відкритих змін! Гроші не будуть враховані в звіті.")

async def get_shift_statistics(session: AsyncSession, shift_id: int):
    """Рахує статистику зміни (X-звіт)."""
    shift = await session.get(CashShift, shift_id)
    if not shift:
        return None

    # 1. Продажі
    sales_query = select(
        Order.payment_method,
        func.sum(Order.total_price)
    ).where(
        Order.cash_shift_id == shift_id
    ).group_by(Order.payment_method)

    sales_res = await session.execute(sales_query)
    sales_data = sales_res.all()

    # Використовуємо Decimal для уникнення помилок TypeError при додаванні до shift.start_cash (який є Decimal)
    total_cash_sales = Decimal(0)
    total_card_sales = Decimal(0)

    for method, amount in sales_data:
        amount_decimal = Decimal(amount) if amount else Decimal(0)
        if method == 'cash':
            total_cash_sales += amount_decimal
        elif method == 'card':
            total_card_sales += amount_decimal

    # 2. Службові операції
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

    for t_type, amount in trans_data:
        amount_decimal = Decimal(amount) if amount else Decimal(0)
        if t_type == 'in':
            service_in += amount_decimal
        elif t_type == 'out':
            service_out += amount_decimal

    # Розрахунок теоретичної готівки:
    # Початок + Продажі Готівкою + Внесення - Вилучення
    # (Картка не впливає на готівку в касі)
    start_cash_decimal = shift.start_cash if shift.start_cash is not None else Decimal(0)
    theoretical_cash = start_cash_decimal + total_cash_sales + service_in - service_out

    return {
        "shift_id": shift.id,
        "start_time": shift.start_time,
        "start_cash": start_cash_decimal,
        "total_sales_cash": total_cash_sales,
        "total_sales_card": total_card_sales,
        "total_sales": total_cash_sales + total_card_sales,
        "service_in": service_in,
        "service_out": service_out,
        "theoretical_cash": theoretical_cash
    }

async def close_active_shift(session: AsyncSession, shift_id: int, end_cash_actual: float):
    """Закриває зміну (Z-звіт)."""
    shift = await session.get(CashShift, shift_id)
    if not shift or shift.is_closed:
        raise ValueError("Зміна не знайдена або вже закрита.")

    stats = await get_shift_statistics(session, shift_id)
    
    shift.end_time = datetime.now()
    # Конвертуємо float в Decimal
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
        amount=Decimal(str(amount)), # Конвертація
        transaction_type=t_type,
        comment=comment
    )
    session.add(tx)
    await session.commit()