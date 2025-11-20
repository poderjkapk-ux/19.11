# inventory_service.py

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import Product, Ingredient, ProductIngredient, Order, WriteOff, WriteOffItem

logger = logging.getLogger(__name__)

async def deduct_ingredients_for_order(session: AsyncSession, order: Order) -> list[str]:
    """
    Списує інгредієнти зі складу на основі техкарт продуктів у замовленні.
    """
    if order.is_deducted:
        logger.info(f"Замовлення #{order.id} вже списане. Пропускаємо.")
        return []

    if not order.products:
        return []

    products_map = {}
    for part in order.products.split(", "):
        try:
            if " x " in part:
                name, qty = part.rsplit(" x ", 1)
                products_map[name.strip()] = int(qty)
        except ValueError:
            continue

    if not products_map:
        return []

    products_db = await session.execute(
        select(Product).where(Product.name.in_(products_map.keys()))
    )
    products = products_db.scalars().all()
    
    warnings = []

    for product in products:
        qty_ordered = products_map.get(product.name, 0)
        if qty_ordered <= 0:
            continue

        tech_card_res = await session.execute(
            select(ProductIngredient).where(ProductIngredient.product_id == product.id)
        )
        tech_card_items = tech_card_res.scalars().all()

        if not tech_card_items:
            continue

        for link in tech_card_items:
            ingredient = await session.get(Ingredient, link.ingredient_id)
            if not ingredient:
                continue
            
            total_needed = link.quantity * qty_ordered
            
            if ingredient.stock_quantity < total_needed:
                warnings.append(f"⚠️ Нестача: {ingredient.name}. На залишку {ingredient.stock_quantity:.3f} {ingredient.unit}, потрібно {total_needed:.3f} {ingredient.unit}. (Пішло в мінус)")

            ingredient.stock_quantity -= total_needed

    order.is_deducted = True
    return warnings

async def add_supply_items(session: AsyncSession, items_data: list):
    """
    Додає товари на склад (Прихід).
    """
    for item in items_data:
        ingredient_id = int(item['ingredient_id'])
        ingredient = await session.get(Ingredient, ingredient_id)
        
        if ingredient:
            qty = float(item['quantity'])
            price_total = float(item['price'])
            
            current_stock_for_calc = max(0, ingredient.stock_quantity) 
            current_value = current_stock_for_calc * ingredient.price_per_unit
            
            new_stock_for_calc = current_stock_for_calc + qty
            new_total_value = current_value + price_total
            
            if new_stock_for_calc > 0:
                ingredient.price_per_unit = new_total_value / new_stock_for_calc
            
            ingredient.stock_quantity += qty

async def process_manual_write_off(session: AsyncSession, reason: str, comment: str, items_data: list):
    """
    Обробляє ручне списання (Акт списання).
    """
    # Створюємо запис про акт
    write_off = WriteOff(reason=reason, comment=comment, total_loss=0.0)
    session.add(write_off)
    await session.flush()

    total_loss = 0.0

    for item in items_data:
        ingredient_id = int(item['ingredient_id'])
        quantity = float(item['quantity'])
        
        ingredient = await session.get(Ingredient, ingredient_id)
        if ingredient:
            # Рахуємо вартість списання по поточній собівартості
            cost = quantity * ingredient.price_per_unit
            total_loss += cost
            
            # Зменшуємо залишок
            ingredient.stock_quantity -= quantity
            
            # Додаємо запис в деталі акту
            session.add(WriteOffItem(
                write_off_id=write_off.id,
                ingredient_id=ingredient_id,
                quantity=quantity,
                cost_at_moment=ingredient.price_per_unit
            ))
    
    write_off.total_loss = total_loss