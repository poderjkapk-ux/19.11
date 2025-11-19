# admin_tables.py

import html
import qrcode
import io
import json
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload
from typing import List, Optional # <--- Додано List, Optional

from models import Table, Employee, Role, Settings # <-- NEW: Import Settings
from templates import ADMIN_HTML_TEMPLATE, ADMIN_TABLES_BODY
from dependencies import get_db_session, check_credentials

router = APIRouter()

@router.get("/admin/tables", response_class=HTMLResponse)
async def admin_tables_list(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Відображає сторінку управління столиками."""
    # NEW: Отримуємо налаштування
    settings = await session.get(Settings, 1) or Settings()
    
    tables_res = await session.execute(
        select(Table).options(
            # ЗМІНЕНО: Використовуємо selectinload для M2M
            selectinload(Table.assigned_waiters)
        ).order_by(Table.name)
    )
    tables = tables_res.scalars().all()

    # Отримуємо ID всіх ролей, які можуть обслуговувати столики
    waiter_roles_res = await session.execute(select(Role.id).where(Role.can_serve_tables == True))
    waiter_role_ids = waiter_roles_res.scalars().all()

    waiters_on_shift = []
    if waiter_role_ids:
        waiters_res = await session.execute(
            select(Employee).where(
                Employee.role_id.in_(waiter_role_ids),
                Employee.is_on_shift == True
            ).order_by(Employee.full_name)
        )
        waiters_on_shift = [{"id": w.id, "full_name": w.full_name} for w in waiters_res.scalars().all()]

    waiters_json = json.dumps(waiters_on_shift)

    rows = []
    for table in tables:
        # ЗМІНЕНО: Виводимо список офіціантів
        waiter_names = ", ".join([html.escape(w.full_name) for w in table.assigned_waiters])
        if not waiter_names:
            waiter_names = "<i>Не призначено</i>"

        # ЗМІНЕНО: Передаємо список ID призначених офіціантів у модальне вікно
        assigned_waiter_ids = json.dumps([w.id for w in table.assigned_waiters])

        rows.append(f"""
        <tr>
            <td>{table.id}</td>
            <td>{html.escape(table.name)}</td>

            <td><a href="/menu/table/{table.access_token}" target="_blank"><img src="/qr/{table.access_token}" alt="QR Code" class="qr-code-img"></a></td>
            <td>{waiter_names}</td>
            <td class="actions">
                <button class="button-sm" onclick='openAssignWaiterModal({table.id}, "{html.escape(table.name)}", {waiters_json}, {assigned_waiter_ids})'>👤 Призначити</button>
                <a href="/admin/tables/delete/{table.id}" onclick="return confirm('Ви впевнені? Видалення столика призведе до видалення QR коду.');" class="button-sm danger">🗑️</a>
            </td>
        </tr>
        """)

    body = ADMIN_TABLES_BODY.format(rows="".join(rows) or "<tr><td colspan='5'>Столиків ще не додано.</td></tr>")

    # NEW: Додано "design_active"
    active_classes = {key: "" for key in ["main_active", "orders_active", "clients_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["tables_active"] = "active"

    # ЗМІНЕНО: Заголовок став більш загальним
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Столики", 
        body=body, 
        site_title=settings.site_title or "Назва", # <-- NEW
        **active_classes
    ))

@router.post("/admin/tables/add")
async def add_table(
    name: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Додає новий столик."""
    # access_token згенерується автоматично завдяки default= у моделі
    new_table = Table(name=name)
    session.add(new_table)
    await session.commit()
    return RedirectResponse(url="/admin/tables", status_code=303)

@router.get("/admin/tables/delete/{table_id}")
async def delete_table(
    table_id: int,
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Видаляє столик."""
    table = await session.get(Table, table_id)
    if table:
        await session.delete(table)
        await session.commit()
    return RedirectResponse(url="/admin/tables", status_code=303)

# ПОВНІСТЮ ОНОВЛЕНИЙ ЕНДПОІНТ
@router.post("/admin/tables/assign_waiter/{table_id}")
async def assign_waiter_to_table(
    table_id: int,
    # Приймаємо список ID з форми
    waiter_ids: Optional[List[int]] = Form(None),
    session: AsyncSession = Depends(get_db_session),
    username: str = Depends(check_credentials)
):
    """Призначає кількох офіціантів на столик."""
    # Використовуємо selectinload для завантаження поточного списку офіціантів
    table = await session.get(Table, table_id, options=[selectinload(Table.assigned_waiters)])
    if not table:
        raise HTTPException(status_code=404, detail="Столик не знайдено")

    # Якщо форма не передала жодного ID (наприклад, зняли виділення з усіх),
    # `waiter_ids` буде None. Ініціалізуємо його як порожній список.
    if waiter_ids is None:
        waiter_ids = []

    # Очищуємо поточний список
    table.assigned_waiters.clear()

    if waiter_ids:
        # Отримуємо ID ролей офіціантів
        waiter_roles_res = await session.execute(select(Role.id).where(Role.can_serve_tables == True))
        waiter_role_ids = waiter_roles_res.scalars().all()

        if waiter_role_ids:
            # Завантажуємо об'єкти Employee, які є офіціантами
            waiters_res = await session.execute(
                select(Employee).where(
                    Employee.id.in_(waiter_ids),
                    Employee.role_id.in_(waiter_role_ids)
                )
            )
            waiters_to_assign = waiters_res.scalars().all()

            # Додаємо нових офіціантів до списку
            for waiter in waiters_to_assign:
                table.assigned_waiters.append(waiter)

    await session.commit()
    return RedirectResponse(url="/admin/tables", status_code=303)


# --- Ендпоінт тепер приймає access_token ---
@router.get("/qr/{access_token}")
async def get_qr_code(request: Request, access_token: str):
    """Генерує та повертає QR-код для столика."""
    base_url = str(request.base_url)

    # --- URL тепер використовує access_token ---
    url = f"{base_url}menu/table/{access_token}"

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")