# cash_service.py

import logging
from datetime import datetime
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

async def open_new_shift(session: AsyncSession, employee_id: int, start_cash: float) -> CashShift:
    """Відкриває нову касову зміну."""
    # Перевіряємо, чи немає вже відкритої зміни
    active_shift = await get_open_shift(session, employee_id)
    if active_shift:
        raise ValueError("У цього співробітника вже є відкрита зміна.")

    new_shift = CashShift(
        employee_id=employee_id,
        start_time=datetime.now(),
        start_cash=start_cash,
        is_closed=False
    )
    session.add(new_shift)
    await session.commit()
    await session.refresh(new_shift)
    return new_shift

async def get_shift_statistics(session: AsyncSession, shift_id: int):
    """
    Рахує статистику зміни (X-звіт):
    - Продажі (готівка/картка)
    - Внесення/Вилучення
    - Розрахунковий залишок в касі
    """
    shift = await session.get(CashShift, shift_id)
    if not shift:
        return None

    # 1. Рахуємо продажі за зміну (тільки замовлення, прив'язані до цієї зміни)
    sales_query = select(
        Order.payment_method,
        func.sum(Order.total_price)
    ).where(
        Order.cash_shift_id == shift_id
    ).group_by(Order.payment_method)

    sales_res = await session.execute(sales_query)
    sales_data = sales_res.all()

    total_cash_sales = 0.0
    total_card_sales = 0.0

    for method, amount in sales_data:
        if method == 'cash':
            total_cash_sales += (amount or 0)
        elif method == 'card':
            total_card_sales += (amount or 0)

    # 2. Рахуємо службові операції
    trans_query = select(
        CashTransaction.transaction_type,
        func.sum(CashTransaction.amount)
    ).where(
        CashTransaction.shift_id == shift_id
    ).group_by(CashTransaction.transaction_type)

    trans_res = await session.execute(trans_query)
    trans_data = trans_res.all()

    service_in = 0.0
    service_out = 0.0

    for t_type, amount in trans_data:
        if t_type == 'in':
            service_in += (amount or 0)
        elif t_type == 'out':
            service_out += (amount or 0)

    # 3. Розрахунковий залишок у сейфі/касі
    # Формула: Початок + Продажі(Готівка) + Внесення - Вилучення
    theoretical_cash = shift.start_cash + total_cash_sales + service_in - service_out

    return {
        "shift_id": shift.id,
        "start_time": shift.start_time,
        "start_cash": shift.start_cash,
        "total_sales_cash": total_cash_sales,
        "total_sales_card": total_card_sales,
        "total_sales": total_cash_sales + total_card_sales,
        "service_in": service_in,
        "service_out": service_out,
        "theoretical_cash": theoretical_cash
    }

async def close_active_shift(session: AsyncSession, shift_id: int, end_cash_actual: float):
    """Закриває зміну (Z-звіт). Фіксує підсумки в базі."""
    shift = await session.get(CashShift, shift_id)
    if not shift or shift.is_closed:
        raise ValueError("Зміна не знайдена або вже закрита.")

    stats = await get_shift_statistics(session, shift_id)
    
    shift.end_time = datetime.now()
    shift.end_cash_actual = end_cash_actual
    
    # Фіксуємо фінальні цифри в історії
    shift.total_sales_cash = stats['total_sales_cash']
    shift.total_sales_card = stats['total_sales_card']
    shift.service_in = stats['service_in']
    shift.service_out = stats['service_out']
    shift.is_closed = True
    
    await session.commit()
    return shift

async def add_shift_transaction(session: AsyncSession, shift_id: int, amount: float, t_type: str, comment: str):
    """Додає службове внесення або вилучення грошей."""
    tx = CashTransaction(
        shift_id=shift_id,
        amount=amount,
        transaction_type=t_type,
        comment=comment
    )
    session.add(tx)
    await session.commit()