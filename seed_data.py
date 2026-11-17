"""
Fictional sample inventory for demos and testing (`flask seed-products`).

All names, suppliers, and costs are made up. Seeding only ADDS products whose
SKU doesn't exist yet: existing products (and their quantities) are never
changed, so running it twice is harmless.
"""

from decimal import Decimal

from models import Product, StockMovement, db, log_activity

DEMO_PRODUCTS = [
    # Packaging Supplies
    dict(sku="SF-1001", name="Cardboard Shipping Box (Medium)", category="Packaging Supplies", unit="Boxes",
         quantity=240, reorder_level=50, unit_cost="1.25", supplier="Demo Packaging Supply",
         location="Warehouse A - Shelf A1",
         description="12 x 10 x 8 in single-wall corrugated box for small parcels."),
    dict(sku="SF-1002", name="Packing Tape Roll", category="Packaging Supplies", unit="Rolls",
         quantity=35, reorder_level=40, unit_cost="3.50", supplier="Demo Packaging Supply",
         location="Warehouse A - Shelf B2",
         description="2 in x 110 yd clear acrylic packing tape."),
    dict(sku="SF-1003", name="Bubble Wrap (50 ft)", category="Packaging Supplies", unit="Rolls",
         quantity=120, reorder_level=30, unit_cost="12.99", supplier="Demo Packaging Supply",
         location="Warehouse A - Shelf B3",
         description="12 in wide roll of 3/16 in small-bubble cushioning."),
    dict(sku="SF-1004", name="Stretch Wrap Film (18 in)", category="Packaging Supplies", unit="Rolls",
         quantity=18, reorder_level=25, unit_cost="24.50", supplier="Demo Industrial Supply",
         location="Warehouse A - Shelf B4",
         description="18 in x 1,500 ft, 80 gauge film for wrapping pallets."),
    # Office Supplies
    dict(sku="SF-1005", name="Shipping Labels (500 pcs)", category="Office Supplies", unit="Packs",
         quantity=150, reorder_level=40, unit_cost="18.75", supplier="Sample Office Co.",
         location="Office - Cabinet 1",
         description="4 x 6 in thermal shipping labels, 500 per pack."),
    dict(sku="SF-1011", name="Printer Paper (500 sheets)", category="Office Supplies", unit="Packs",
         quantity=100, reorder_level=20, unit_cost="6.49", supplier="Sample Office Co.",
         location="Office - Cabinet 2",
         description="One ream of 8.5 x 11 in, 20 lb white copy paper."),
    # Electronics
    dict(sku="SF-1007", name="USB-C Charging Cable", category="Electronics", unit="Pieces",
         quantity=75, reorder_level=15, unit_cost="7.99", supplier="Sample Electronics Supply",
         location="Warehouse B - Bin E2",
         description="6 ft braided USB-C to USB-C cable, 60 W."),
    dict(sku="SF-1008", name="Wireless Mouse", category="Electronics", unit="Pieces",
         quantity=12, reorder_level=20, unit_cost="14.50", supplier="Sample Electronics Supply",
         location="Warehouse B - Bin E3",
         description="2.4 GHz wireless optical mouse with USB receiver."),
    # Clothing
    dict(sku="SF-1009", name="Cotton T-Shirt (Black)", category="Clothing", unit="Pieces",
         quantity=85, reorder_level=25, unit_cost="6.00", supplier="Demo Apparel Wholesale",
         location="Warehouse C - Rack T1",
         description="100% cotton crew-neck t-shirt, size M."),
    dict(sku="SF-1010", name="Denim Jacket (Blue)", category="Clothing", unit="Pieces",
         quantity=8, reorder_level=10, unit_cost="32.00", supplier="Demo Apparel Wholesale",
         location="Warehouse C - Rack J1",
         description="Classic-fit denim jacket, size M."),
    # Other
    dict(sku="SF-1006", name="Safety Gloves (Pair)", category="Other", unit="Pairs",
         quantity=0, reorder_level=20, unit_cost="4.25", supplier="Demo Safety Gear",
         location="Warehouse B - Shelf C1",
         description="Nitrile-coated work gloves, size L."),
    dict(sku="SF-1012", name="Packing Scissors", category="Other", unit="Pieces",
         quantity=45, reorder_level=10, unit_cost="5.75", supplier="Demo Packaging Supply",
         location="Warehouse A - Packing Station",
         description="8 in stainless steel scissors for packing stations."),
]


def seed_products():
    """
    Add every DEMO_PRODUCTS item whose SKU isn't in the database yet, in ONE
    transaction (all or nothing). Returns (added_skus, skipped_skus).

    Each new product gets the same records as "Add product" in the app:
    a "Starting stock" stock movement (if quantity > 0) and a
    "Product created" activity entry. No user did it, so actor is empty.
    """
    existing = {sku for (sku,) in db.session.query(Product.sku).filter(
        Product.sku.in_([item["sku"] for item in DEMO_PRODUCTS]))}
    added, skipped = [], []
    try:
        for item in DEMO_PRODUCTS:
            if item["sku"] in existing:
                skipped.append(item["sku"])  # never overwrite or change existing products
                continue
            product = Product(**{**item, "unit_cost": Decimal(item["unit_cost"])})
            db.session.add(product)
            db.session.flush()  # assigns product.id
            if product.quantity > 0:
                db.session.add(StockMovement(
                    product_id=product.id, quantity_change=product.quantity,
                    actor_id=None, note="Starting stock (demo data)",
                ))
            log_activity(
                None, f"Added demo product {product.sku} ({product.name}) with {product.quantity} "
                      f"{product.unit.lower()}",
                product.id, category="product_created", quantity_change=product.quantity or None,
            )
            added.append(product.sku)
        db.session.commit()
    except Exception:
        db.session.rollback()  # nothing is saved if any product fails
        raise
    return added, skipped
