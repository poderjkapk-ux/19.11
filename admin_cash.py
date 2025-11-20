# admin_cash.py

import html
from datetime import datetime
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import joinedload

from models import Employee, CashShift, Settings, Role
from templates import ADMIN_HTML_TEMPLATE
from dependencies import get_db_session, check_credentials
from cash_service import open_new_shift, get_open_shift, get_shift_statistics, close_active_shift, add_shift_transaction

router = APIRouter()

@router.get("/admin/cash", response_class=HTMLResponse)
async def cash_dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    settings = await session.get(Settings, 1) or Settings()
    
    # Шукаємо будь-яку відкриту зміну
    active_shift_res = await session.execute(
        select(CashShift).where(CashShift.is_closed == False).options(joinedload(CashShift.employee))
    )
    active_shift = active_shift_res.scalars().first()
    
    body = ""
    
    # Кнопка історії
    history_btn = """
    <div style="text-align: right; margin-bottom: 20px;">
        <a href="/admin/cash/history" class="button secondary">📜 Історія змін (Z-звіти)</a>
    </div>
    """
    
    if not active_shift:
        # Зміна закрита. Форма відкриття.
        employees = (await session.execute(select(Employee).where(Employee.is_on_shift == True))).scalars().all()
        emp_options = "".join([f'<option value="{e.id}">{html.escape(e.full_name)}</option>' for e in employees])
        
        body = f"""
        {history_btn}
        <div class="card">
            <h2>🔴 Каса закрита</h2>
            <p>Щоб почати роботу, відкрийте нову касову зміну.</p>
            <form action="/admin/cash/open" method="post" style="max_width: 400px;">
                <label>Касир (хто відкриває):</label>
                <select name="employee_id" required>
                    {emp_options or '<option value="" disabled>Немає працівників на зміні</option>'}
                </select>
                
                <label>Залишок в касі (грн):</label>
                <input type="number" step="0.01" name="start_cash" value="0.00" required>
                
                <button type="submit" class="button">🟢 Відкрити зміну</button>
            </form>
        </div>
        """
    else:
        # Зміна відкрита. X-звіт та дії.
        stats = await get_shift_statistics(session, active_shift.id)
        
        x_report_html = f"""
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px;">
            <div style="background: #e3f2fd; padding: 15px; border-radius: 8px;">
                <h3>💰 Готівка в касі (Теорія)</h3>
                <div style="font-size: 2em; font-weight: bold; color: #0d47a1;">{stats['theoretical_cash']:.2f} грн</div>
                <small>Початок ({stats['start_cash']}) + Продажі ({stats['total_sales_cash']}) + Внесення ({stats['service_in']}) - Вилучення ({stats['service_out']})</small>
            </div>
            <div style="background: #f3e5f5; padding: 15px; border-radius: 8px;">
                <h3>💳 Термінал (Картка)</h3>
                <div style="font-size: 2em; font-weight: bold; color: #4a148c;">{stats['total_sales_card']:.2f} грн</div>
            </div>
        </div>
        
        <table style="width: 100%; margin-bottom: 20px;">
            <tr><td><b>Початок зміни:</b></td><td>{stats['start_time'].strftime('%d.%m.%Y %H:%M')}</td></tr>
            <tr><td><b>Касир:</b></td><td>{html.escape(active_shift.employee.full_name)}</td></tr>
            <tr><td><b>Продажі (Всього):</b></td><td>{stats['total_sales']:.2f} грн</td></tr>
        </table>
        """
        
        actions_html = f"""
        <div class="card">
            <h3>Службові операції</h3>
            <form action="/admin/cash/transaction" method="post" class="inline-form">
                <input type="hidden" name="shift_id" value="{active_shift.id}">
                <select name="transaction_type" style="width: 150px;">
                    <option value="in">📥 Внесення</option>
                    <option value="out">📤 Вилучення</option>
                </select>
                <input type="number" step="0.01" name="amount" placeholder="Сума" required style="width: 120px;">
                <input type="text" name="comment" placeholder="Коментар (напр. Розмін)" required>
                <button type="submit">Виконати</button>
            </form>
        </div>

        <div class="card" style="border-color: #f44336;">
            <h3 style="color: #d32f2f;">🛑 Закриття зміни (Z-звіт)</h3>
            <p>Перерахуйте фактичну готівку в касі перед закриттям.</p>
            <form action="/admin/cash/close" method="post" onsubmit="return confirm('Ви впевнені, що хочете закрити зміну?');">
                <input type="hidden" name="shift_id" value="{active_shift.id}">
                <label>Фактичний залишок готівки:</label>
                <input type="number" step="0.01" name="end_cash_actual" required placeholder="Скільки грошей по факту?">
                <button type="submit" class="button danger">🖨️ Закрити зміну (Зберегти Z-звіт)</button>
            </form>
        </div>
        """
        
        body = f"""
        {history_btn}
        <div class="card">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h2>🟢 Зміна відкрита #{active_shift.id}</h2>
                <span style="color:gray;">{active_shift.start_time.strftime('%H:%M')}</span>
            </div>
            {x_report_html}
        </div>
        {actions_html}
        """

    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["reports_active"] = "active"

    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Каса", 
        body=body, 
        site_title=settings.site_title or "Назва", 
        main_active="",
        **active_classes
    ))

# --- ІСТОРІЯ ЗМІН ---
@router.get("/admin/cash/history", response_class=HTMLResponse)
async def cash_history(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await session.get(Settings, 1) or Settings()
    
    # Останні 20 закритих змін
    shifts_res = await session.execute(
        select(CashShift)
        .where(CashShift.is_closed == True)
        .options(joinedload(CashShift.employee))
        .order_by(desc(CashShift.end_time))
        .limit(20)
    )
    shifts = shifts_res.scalars().all()
    
    rows = ""
    for s in shifts:
        # Різниця (нестача/надлишок)
        # Теоретичний залишок в кінці = (start + cash_sales + in - out)
        theoretical = s.start_cash + s.total_sales_cash + s.service_in - s.service_out
        diff = s.end_cash_actual - theoretical
        
        diff_color = "green" if abs(diff) < 1 else ("red" if diff < 0 else "blue")
        diff_str = f"{diff:+.2f}"
        
        rows += f"""
        <tr>
            <td>#{s.id}</td>
            <td>{s.start_time.strftime('%d.%m %H:%M')} <br> {s.end_time.strftime('%d.%m %H:%M')}</td>
            <td>{html.escape(s.employee.full_name)}</td>
            <td>{s.total_sales_cash + s.total_sales_card:.2f} грн</td>
            <td>{s.end_cash_actual:.2f} грн</td>
            <td style="color:{diff_color}; font-weight:bold;">{diff_str}</td>
            <td>
                <a href="/admin/cash/z_report/{s.id}" target="_blank" class="button-sm">🖨️ Чек</a>
            </td>
        </tr>
        """
        
    body = f"""
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <h2>📜 Історія касових змін (Останні 20)</h2>
            <a href="/admin/cash" class="button secondary">⬅️ Поточна зміна</a>
        </div>
        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Час (Відкр/Закр)</th>
                        <th>Касир</th>
                        <th>Виручка</th>
                        <th>Готівка (факт)</th>
                        <th>Різниця</th>
                        <th>Дії</th>
                    </tr>
                </thead>
                <tbody>
                    {rows or "<tr><td colspan='7'>Історія порожня</td></tr>"}
                </tbody>
            </table>
        </div>
    </div>
    """
    
    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["reports_active"] = "active"
    
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(title="Історія змін", body=body, site_title=settings.site_title, main_active="", **active_classes))

# --- ДРУК Z-ЗВІТУ ---
@router.get("/admin/cash/z_report/{shift_id}", response_class=HTMLResponse)
async def print_z_report(shift_id: int, session: AsyncSession = Depends(get_db_session)):
    shift = await session.get(CashShift, shift_id, options=[joinedload(CashShift.employee)])
    if not shift: return HTMLResponse("Зміну не знайдено", status_code=404)
    
    settings = await session.get(Settings, 1) or Settings()
    
    # Розрахунок теоретичного залишку
    theoretical = shift.start_cash + shift.total_sales_cash + shift.service_in - shift.service_out
    diff = shift.end_cash_actual - theoretical
    
    html_report = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Z-звіт #{shift.id}</title>
        <style>
            body {{ font-family: 'Courier New', monospace; width: 300px; margin: 0 auto; padding: 10px; }}
            .header {{ text-align: center; margin-bottom: 10px; border-bottom: 1px dashed #000; padding-bottom: 5px; }}
            .row {{ display: flex; justify-content: space-between; margin-bottom: 3px; }}
            .total {{ font-weight: bold; border-top: 1px dashed #000; margin-top: 5px; padding-top: 5px; }}
            .footer {{ text-align: center; margin-top: 20px; font-size: 0.8em; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h3>{settings.site_title}</h3>
            <div>Z-ЗВІТ (Зміна #{shift.id})</div>
            <div>{shift.end_time.strftime('%d.%m.%Y %H:%M:%S')}</div>
            <div>Касир: {shift.employee.full_name}</div>
        </div>
        
        <div class="row"><span>Початок зміни:</span><span>{shift.start_time.strftime('%H:%M')}</span></div>
        <div class="row"><span>Початковий залишок:</span><span>{shift.start_cash:.2f}</span></div>
        <br>
        <div class="row"><span>Продажі (Готівка):</span><span>+{shift.total_sales_cash:.2f}</span></div>
        <div class="row"><span>Продажі (Картка):</span><span>+{shift.total_sales_card:.2f}</span></div>
        <div class="row total"><span>ВСЬОГО ПРОДАЖІВ:</span><span>{(shift.total_sales_cash + shift.total_sales_card):.2f}</span></div>
        <br>
        <div class="row"><span>Службове внесення:</span><span>+{shift.service_in:.2f}</span></div>
        <div class="row"><span>Службове вилучення:</span><span>-{shift.service_out:.2f}</span></div>
        <br>
        <div class="row" style="font-weight:bold;"><span>Готівка в касі (факт):</span><span>{shift.end_cash_actual:.2f}</span></div>
        <div class="row"><span>Різниця:</span><span>{diff:+.2f}</span></div>
        
        <div class="footer">
            <p>Зміна закрита.</p>
            <p>--- ФІСКАЛЬНИЙ ЧЕК (ТЕСТ) ---</p>
        </div>
        
        <script>window.print();</script>
    </body>
    </html>
    """
    return HTMLResponse(html_report)


@router.post("/admin/cash/open")
async def web_open_shift(
    employee_id: int = Form(...),
    start_cash: float = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    try:
        await open_new_shift(session, employee_id, start_cash)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    return RedirectResponse("/admin/cash", status_code=303)

@router.post("/admin/cash/transaction")
async def web_cash_transaction(
    shift_id: int = Form(...),
    transaction_type: str = Form(...),
    amount: float = Form(...),
    comment: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    await add_shift_transaction(session, shift_id, amount, transaction_type, comment)
    return RedirectResponse("/admin/cash", status_code=303)

@router.post("/admin/cash/close")
async def web_close_shift(
    shift_id: int = Form(...),
    end_cash_actual: float = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    try:
        await close_active_shift(session, shift_id, end_cash_actual)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
        
    return RedirectResponse("/admin/cash/history", status_code=303) # Перенаправляємо на історію