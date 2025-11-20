# admin_inventory.py

import html
import json
from fastapi import APIRouter, Depends, Form, HTTPException, Body, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from models import Ingredient, Product, ProductIngredient, Supply, SupplyItem, Settings
from templates import ADMIN_HTML_TEMPLATE
from dependencies import get_db_session, check_credentials
from inventory_service import add_supply_items, process_manual_write_off

router = APIRouter()

@router.get("/admin/inventory", response_class=HTMLResponse)
async def inventory_dashboard(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    """Головна сторінка складу: залишки та швидкі дії."""
    settings = await session.get(Settings, 1) or Settings()
    
    ingredients_res = await session.execute(select(Ingredient).order_by(Ingredient.name))
    ingredients = ingredients_res.scalars().all()
    
    ing_rows = ""
    for ing in ingredients:
        color = "red" if ing.stock_quantity <= 0 else ("orange" if ing.stock_quantity < 5 else "green")
        ing_rows += f"""
        <tr>
            <td>{html.escape(ing.name)}</td>
            <td><strong style="color:{color}">{ing.stock_quantity:.3f}</strong> {html.escape(ing.unit)}</td>
            <td>{ing.price_per_unit:.2f} грн</td>
            <td class="actions">
                <a href="/admin/inventory/edit/{ing.id}" class="button-sm" title="Редагувати">✏️</a>
            </td>
        </tr>
        """

    products_res = await session.execute(select(Product).order_by(Product.name))
    products = products_res.scalars().all()
    product_options = "".join([f'<option value="{p.id}">{html.escape(p.name)}</option>' for p in products])

    body = f"""
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
            <h2>📦 Складські залишки</h2>
            <div>
                <a href="/admin/inventory/add_ingredient" class="button"><i class="fa-solid fa-plus"></i> Додати інгредієнт</a>
                <a href="/admin/inventory/supply" class="button secondary"><i class="fa-solid fa-truck"></i> Поставка (Прихід)</a>
                <a href="/admin/inventory/write_off" class="button danger" style="background-color:#dc3545;"><i class="fa-solid fa-trash"></i> Списання (Витрати)</a>
            </div>
        </div>
        
        <div class="table-wrapper">
            <table>
                <thead><tr><th>Назва</th><th>Залишок</th><th>Собівартість (од.)</th><th>Дії</th></tr></thead>
                <tbody>{ing_rows or "<tr><td colspan='4' style='text-align:center;'>Склад порожній. Додайте перші інгредієнти.</td></tr>"}</tbody>
            </table>
        </div>
    </div>
    
    <div class="card">
        <h2>📑 Технологічні карти (Калькуляція)</h2>
        <p>Оберіть страву, щоб налаштувати її склад (списання продуктів):</p>
        <form action="/admin/inventory/select_product_for_tech" method="get" class="inline-form">
             <select name="product_id" required style="max-width:300px;">
                <option value="" disabled selected>-- Оберіть страву --</option>
                {product_options}
             </select>
             <button type="submit">Налаштувати техкарту</button>
        </form>
    </div>
    """
    
    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"
    
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(
        title="Склад", 
        body=body, 
        site_title=settings.site_title or "Назва", 
        main_active="", 
        **active_classes
    ))

# --- СПИСАННЯ (Manual Write-off) ---

@router.get("/admin/inventory/write_off", response_class=HTMLResponse)
async def write_off_page(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await session.get(Settings, 1) or Settings()
    ingredients = (await session.execute(select(Ingredient).order_by(Ingredient.name))).scalars().all()
    
    js_script = """
    <script>
        function addRow() {
            const container = document.getElementById('write-off-rows');
            const index = container.children.length;
            const options = document.getElementById('hidden-options').innerHTML;
            
            const rowDiv = document.createElement('div');
            rowDiv.className = 'form-grid';
            rowDiv.style.cssText = 'margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; grid-template-columns: 2fr 1fr 50px; align-items: end;';
            
            rowDiv.innerHTML = `
                <div>
                    <label style="font-size:0.85rem;">Інгредієнт</label>
                    <select name="items[${index}][ingredient_id]" required style="margin-bottom:0;">${options}</select>
                </div>
                <div>
                    <label style="font-size:0.85rem;">Кількість для списання</label>
                    <input type="number" step="0.001" name="items[${index}][quantity]" placeholder="К-сть" required style="margin-bottom:0;">
                </div>
                <div>
                    <button type="button" class="button-sm danger" onclick="this.parentElement.parentElement.remove()" style="height:42px; width:100%;">🗑️</button>
                </div>
            `;
            container.appendChild(rowDiv);
        }
    </script>
    """
    
    ing_options = "".join([f'<option value="{i.id}">{html.escape(i.name)} ({i.unit}) | Залишок: {i.stock_quantity}</option>' for i in ingredients])
    
    body = f"""
    {js_script}
    <div id="hidden-options" style="display:none;">
        <option value="" disabled selected>-- Оберіть --</option>
        {ing_options}
    </div>
    
    <div class="card">
        <h2>🗑️ Акт списання продуктів</h2>
        <p>Використовуйте для фіксації порчі, проробки меню, харчування персоналу тощо.</p>
        
        <form id="writeOffForm">
            <label>Причина списання:</label>
            <select name="reason" required>
                <option value="Порча">Порча / Термін придатності</option>
                <option value="Проробка">Проробка нових страв</option>
                <option value="Персонал">Харчування персоналу</option>
                <option value="Помилка">Помилка кухаря</option>
                <option value="Інше">Інше</option>
            </select>
            
            <label>Коментар (необов'язково):</label>
            <input type="text" name="comment" placeholder="Наприклад: Впало на підлогу">
            
            <div style="background: #fff0f0; padding: 10px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #ffcccc;">
                <div id="write-off-rows">
                    <div class="form-grid" style="margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; grid-template-columns: 2fr 1fr 50px; align-items: end;">
                        <div>
                            <label style="font-size:0.85rem;">Інгредієнт</label>
                            <select required style="margin-bottom:0;">
                                <option value="" disabled selected>-- Оберіть --</option>
                                {ing_options}
                            </select>
                        </div>
                        <div>
                            <label style="font-size:0.85rem;">Кількість для списання</label>
                            <input type="number" step="0.001" placeholder="К-сть" required style="margin-bottom:0;">
                        </div>
                        <div></div>
                    </div>
                </div>
                <button type="button" class="button secondary" onclick="addRow()" style="margin-top:10px;">+ Додати рядок</button>
            </div>
            
            <div style="text-align:right;">
                <a href="/admin/inventory" class="button secondary">Скасувати</a>
                <button type="submit" onclick="submitWriteOff(event)" class="button danger" style="background-color:#dc3545;">Підтвердити списання</button>
            </div>
        </form>
    </div>
    
    <script>
    async function submitWriteOff(e) {{
        e.preventDefault();
        const form = document.getElementById('writeOffForm');
        const rows = document.querySelectorAll('#write-off-rows .form-grid');
        const items = [];
        let hasError = false;
        
        rows.forEach(row => {{
            const select = row.querySelector('select');
            const qtyInput = row.querySelector('input[type="number"]');
            
            if(select && qtyInput) {{
                if(!select.value || !qtyInput.value) {{ hasError = true; return; }}
                items.push({{ ingredient_id: select.value, quantity: qtyInput.value }});
            }}
        }});
        
        if (hasError || items.length === 0) {{
            alert('Заповніть всі поля.');
            return;
        }}
        
        if(!confirm('Ви впевнені? Ця дія зменшить залишки на складі.')) return;
        
        const payload = {{
            reason: form.querySelector('select[name="reason"]').value,
            comment: form.querySelector('input[name="comment"]').value,
            items: items
        }};

        try {{
            const res = await fetch('/api/inventory/write_off', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(payload)
            }});
            if(res.ok) window.location.href = '/admin/inventory';
            else alert('Помилка збереження.');
        }} catch (error) {{
            console.error(error);
            alert('Помилка мережі.');
        }}
    }}
    </script>
    """
    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(title="Списання", body=body, site_title=settings.site_title, main_active="", **active_classes))

@router.post("/api/inventory/write_off")
async def api_write_off(data: dict = Body(...), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    items = data.get('items', [])
    reason = data.get('reason', 'Інше')
    comment = data.get('comment', '')
    
    if not items:
        raise HTTPException(status_code=400, detail="Немає товарів")
    
    await process_manual_write_off(session, reason, comment, items)
    await session.commit()
    return {"status": "ok"}

# --- ДОДАВАННЯ ТА РЕДАГУВАННЯ ІНГРЕДІЄНТІВ ---
@router.get("/admin/inventory/add_ingredient", response_class=HTMLResponse)
async def add_ingredient_form(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await session.get(Settings, 1) or Settings()
    body = """
    <div class="card">
        <h2>Новий інгредієнт</h2>
        <form action="/admin/inventory/add_ingredient" method="post">
            <label>Назва (напр. Картопля):</label>
            <input type="text" name="name" required placeholder="Введіть назву сировини">
            <label>Одиниця виміру (напр. кг, л, шт, г):</label>
            <input type="text" name="unit" required placeholder="кг">
            <label>Початковий залишок (якщо є):</label>
            <input type="number" step="0.001" name="stock_quantity" value="0">
            <label>Поточна собівартість за одиницю (грн):</label>
            <input type="number" step="0.01" name="price_per_unit" value="0">
            <button type="submit">Зберегти</button>
            <a href="/admin/inventory" class="button secondary">Скасувати</a>
        </form>
    </div>
    """
    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(title="Новий інгредієнт", body=body, site_title=settings.site_title, main_active="", **active_classes))

@router.post("/admin/inventory/add_ingredient")
async def add_ingredient_post(name: str = Form(...), unit: str = Form(...), stock_quantity: float = Form(0), price_per_unit: float = Form(0), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    session.add(Ingredient(name=name, unit=unit, stock_quantity=stock_quantity, price_per_unit=price_per_unit))
    await session.commit()
    return RedirectResponse("/admin/inventory", status_code=303)

@router.get("/admin/inventory/edit/{ing_id}", response_class=HTMLResponse)
async def edit_ingredient_form(ing_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await session.get(Settings, 1) or Settings()
    ing = await session.get(Ingredient, ing_id)
    if not ing: raise HTTPException(404, "Інгредієнт не знайдено")

    body = f"""
    <div class="card">
        <h2>Редагування: {html.escape(ing.name)}</h2>
        <form action="/admin/inventory/edit/{ing.id}" method="post">
            <label>Назва:</label>
            <input type="text" name="name" value="{html.escape(ing.name)}" required>
            <label>Одиниця виміру:</label>
            <input type="text" name="unit" value="{html.escape(ing.unit)}" required>
            <div style="background: #fff3cd; padding: 10px; margin-bottom: 15px; border-radius: 5px; border: 1px solid #ffeeba;">
                <strong>Увага!</strong> Ручна зміна залишку не створює запис про поставку. Використовуйте "Оформити поставку" або "Списання".
            </div>
            <label>Поточний залишок:</label>
            <input type="number" step="0.001" name="stock_quantity" value="{ing.stock_quantity}">
            <label>Собівартість за одиницю (грн):</label>
            <input type="number" step="0.01" name="price_per_unit" value="{ing.price_per_unit}">
            <button type="submit">Зберегти зміни</button>
            <a href="/admin/inventory" class="button secondary">Скасувати</a>
        </form>
    </div>
    """
    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(title="Редагування інгредієнта", body=body, site_title=settings.site_title, main_active="", **active_classes))

@router.post("/admin/inventory/edit/{ing_id}")
async def edit_ingredient_post(ing_id: int, name: str = Form(...), unit: str = Form(...), stock_quantity: float = Form(...), price_per_unit: float = Form(...), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    ing = await session.get(Ingredient, ing_id)
    if ing:
        ing.name = name
        ing.unit = unit
        ing.stock_quantity = stock_quantity
        ing.price_per_unit = price_per_unit
        await session.commit()
    return RedirectResponse("/admin/inventory", status_code=303)

# --- ТЕХКАРТИ ---
@router.get("/admin/inventory/select_product_for_tech")
async def select_product_redirect(product_id: int):
    return RedirectResponse(f"/admin/inventory/tech_card/{product_id}", status_code=303)

@router.get("/admin/inventory/tech_card/{product_id}", response_class=HTMLResponse)
async def manage_tech_card(product_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await session.get(Settings, 1) or Settings()
    product = await session.get(Product, product_id)
    if not product: raise HTTPException(404, "Продукт не знайдено")
    
    links_res = await session.execute(
        select(ProductIngredient).where(ProductIngredient.product_id == product_id).options(joinedload(ProductIngredient.ingredient))
    )
    links = links_res.scalars().all()
    
    rows = ""
    total_cost = 0
    for link in links:
        cost = link.quantity * link.ingredient.price_per_unit
        total_cost += cost
        rows += f"""
        <tr>
            <td>{html.escape(link.ingredient.name)}</td>
            <td>{link.quantity} {link.ingredient.unit}</td>
            <td>~{cost:.2f} грн</td>
            <td><a href="/admin/inventory/tech_card/delete/{link.id}" class="button-sm danger" onclick="return confirm('Видалити компонент?');">🗑️</a></td>
        </tr>
        """
    
    all_ingredients = (await session.execute(select(Ingredient).order_by(Ingredient.name))).scalars().all()
    ing_options = "".join([f'<option value="{i.id}">{html.escape(i.name)} ({i.unit})</option>' for i in all_ingredients])

    body = f"""
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:start;">
            <div>
                <h2>🥗 Техкарта: {html.escape(product.name)}</h2>
                <p style="color:gray;">Ціна продажу: <b>{product.price} грн</b> | Собівартість продуктів: <b>~{total_cost:.2f} грн</b></p>
            </div>
            <a href="/admin/inventory" class="button secondary">⬅️ До складу</a>
        </div>
        <div class="table-wrapper">
            <table>
                <thead><tr><th>Інгредієнт</th><th>Кількість (Брутто)</th><th>Вартість</th><th>Дії</th></tr></thead>
                <tbody>{rows or "<tr><td colspan='4' style='text-align:center;'>Інгредієнти не додано</td></tr>"}</tbody>
            </table>
        </div>
        <hr style="margin: 20px 0; border: 0; border-top: 1px solid #eee;">
        <h3>Додати компонент</h3>
        <form action="/admin/inventory/tech_card/add" method="post" class="inline-form" style="background: #f9fafb; padding: 15px; border-radius: 8px;">
            <input type="hidden" name="product_id" value="{product.id}">
            <div style="flex-grow:1;">
                <label style="font-size:0.9em;">Сировина:</label>
                <select name="ingredient_id" required style="width:100%;">{ing_options}</select>
            </div>
            <div style="width:120px;">
                <label style="font-size:0.9em;">Кількість:</label>
                <input type="number" step="0.001" name="quantity" placeholder="0.000" required style="width:100%;">
            </div>
            <div style="align-self:end;">
                <button type="submit">➕ Додати</button>
            </div>
        </form>
    </div>
    """
    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(title=f"Техкарта {product.name}", body=body, site_title=settings.site_title, main_active="", **active_classes))

@router.post("/admin/inventory/tech_card/add")
async def add_tech_card_item(product_id: int = Form(...), ingredient_id: int = Form(...), quantity: float = Form(...), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    session.add(ProductIngredient(product_id=product_id, ingredient_id=ingredient_id, quantity=quantity))
    await session.commit()
    return RedirectResponse(f"/admin/inventory/tech_card/{product_id}", status_code=303)

@router.get("/admin/inventory/tech_card/delete/{link_id}")
async def delete_tech_card_item(link_id: int, session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    link = await session.get(ProductIngredient, link_id)
    pid = link.product_id
    await session.delete(link)
    await session.commit()
    return RedirectResponse(f"/admin/inventory/tech_card/{pid}", status_code=303)

# --- ПОСТАВКИ ---
@router.get("/admin/inventory/supply", response_class=HTMLResponse)
async def supply_page(session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    settings = await session.get(Settings, 1) or Settings()
    ingredients = (await session.execute(select(Ingredient).order_by(Ingredient.name))).scalars().all()
    
    js_script = """
    <script>
        function addRow() {
            const container = document.getElementById('supply-rows');
            const index = container.children.length;
            const options = document.getElementById('hidden-options').innerHTML;
            
            const rowDiv = document.createElement('div');
            rowDiv.className = 'form-grid';
            rowDiv.style.cssText = 'margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; grid-template-columns: 2fr 1fr 1fr 50px; align-items: end;';
            
            rowDiv.innerHTML = `
                <div>
                    <label style="font-size:0.85rem;">Інгредієнт</label>
                    <select name="items[${index}][ingredient_id]" required style="margin-bottom:0;">${options}</select>
                </div>
                <div>
                    <label style="font-size:0.85rem;">Кількість</label>
                    <input type="number" step="0.001" name="items[${index}][quantity]" placeholder="К-сть" required style="margin-bottom:0;">
                </div>
                <div>
                    <label style="font-size:0.85rem;">Ціна за все (грн)</label>
                    <input type="number" step="0.01" name="items[${index}][price]" placeholder="Сума" required style="margin-bottom:0;">
                </div>
                <div>
                    <button type="button" class="button-sm danger" onclick="this.parentElement.parentElement.remove()" style="height:42px; width:100%;">🗑️</button>
                </div>
            `;
            container.appendChild(rowDiv);
        }
    </script>
    """
    ing_options = "".join([f'<option value="{i.id}">{html.escape(i.name)} ({i.unit})</option>' for i in ingredients])
    
    body = f"""
    {js_script}
    <div id="hidden-options" style="display:none;"><option value="" disabled selected>-- Оберіть --</option>{ing_options}</div>
    
    <div class="card">
        <h2>🚚 Оформлення поставки (Прихід)</h2>
        <p>Тут ви можете додати нові товари на склад. Собівартість буде перерахована автоматично.</p>
        
        <form id="supplyForm">
            <label>Коментар / Постачальник:</label>
            <input type="text" name="comment" placeholder="Наприклад: Metro, накладна №123 від 20.11">
            <div style="background: #f4f4f4; padding: 10px; border-radius: 5px; margin-bottom: 15px;">
                <div id="supply-rows">
                    <div class="form-grid" style="margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 10px; grid-template-columns: 2fr 1fr 1fr 50px; align-items: end;">
                        <div>
                            <label style="font-size:0.85rem;">Інгредієнт</label>
                            <select required style="margin-bottom:0;"><option value="" disabled selected>-- Оберіть --</option>{ing_options}</select>
                        </div>
                        <div>
                            <label style="font-size:0.85rem;">Кількість</label>
                            <input type="number" step="0.001" placeholder="К-сть" required style="margin-bottom:0;">
                        </div>
                        <div>
                            <label style="font-size:0.85rem;">Ціна за все (грн)</label>
                            <input type="number" step="0.01" placeholder="Сума" required style="margin-bottom:0;">
                        </div>
                        <div></div> 
                    </div>
                </div>
                <button type="button" class="button secondary" onclick="addRow()" style="margin-top:10px;">+ Додати рядок</button>
            </div>
            <div style="text-align:right;">
                <a href="/admin/inventory" class="button secondary">Скасувати</a>
                <button type="submit" onclick="submitSupply(event)">💾 Зберегти поставку</button>
            </div>
        </form>
    </div>
    
    <script>
    async function submitSupply(e) {{
        e.preventDefault();
        const form = document.getElementById('supplyForm');
        const rows = document.querySelectorAll('#supply-rows .form-grid');
        const items = [];
        let hasError = false;
        
        rows.forEach(row => {{
            const select = row.querySelector('select');
            const qtyInput = row.querySelector('input[placeholder="К-сть"]');
            const priceInput = row.querySelector('input[placeholder="Сума"]');
            
            if(select && qtyInput && priceInput) {{
                if(!select.value || !qtyInput.value || !priceInput.value) {{ hasError = true; return; }}
                items.push({{ ingredient_id: select.value, quantity: qtyInput.value, price: priceInput.value }});
            }}
        }});
        
        if (hasError || items.length === 0) {{ alert('Будь ласка, заповніть всі поля.'); return; }}
        
        const saveBtn = e.target;
        saveBtn.innerText = "Збереження...";
        saveBtn.disabled = true;
        
        const payload = {{ comment: form.querySelector('input[name="comment"]').value, items: items }};

        try {{
            const res = await fetch('/api/inventory/supply', {{
                method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload)
            }});
            if(res.ok) window.location.href = '/admin/inventory';
            else {{ alert('Помилка збереження.'); saveBtn.innerText = "💾 Зберегти поставку"; saveBtn.disabled = false; }}
        }} catch (error) {{ console.error(error); alert('Помилка мережі.'); saveBtn.innerText = "💾 Зберегти поставку"; saveBtn.disabled = false; }}
    }}
    </script>
    """
    active_classes = {key: "" for key in ["orders_active", "clients_active", "tables_active", "products_active", "categories_active", "menu_active", "employees_active", "statuses_active", "reports_active", "settings_active", "design_active"]}
    active_classes["products_active"] = "active"
    return HTMLResponse(ADMIN_HTML_TEMPLATE.format(title="Нова поставка", body=body, site_title=settings.site_title, main_active="", **active_classes))

@router.post("/api/inventory/supply")
async def api_add_supply(data: dict = Body(...), session: AsyncSession = Depends(get_db_session), username: str = Depends(check_credentials)):
    items = data.get('items', [])
    comment = data.get('comment', '')
    if not items: raise HTTPException(status_code=400, detail="Немає товарів")
    
    total_cost = sum(float(x['price']) for x in items)
    supply = Supply(comment=comment, total_cost=total_cost)
    session.add(supply)
    await session.flush()
    
    for item in items:
        session.add(SupplyItem(supply_id=supply.id, ingredient_id=int(item['ingredient_id']), quantity=float(item['quantity']), price=float(item['price'])))
    
    await add_supply_items(session, items)
    await session.commit()
    return {"status": "ok"}