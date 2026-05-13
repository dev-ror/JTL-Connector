"""
JTL → Odoo Migration Orchestrator
Run:  python migrate.py [--dry-run] [--module categories,products,customers,orders]
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from config import sql_config, odoo_config, migration_config
from jtl_reader import JTLReader
from odoo_writer import OdooWriter


def setup_logging(cfg):
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(cfg.output_dir, cfg.log_file)),
    ]
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def parse_args():
    p = argparse.ArgumentParser(description="JTL-Wawi → Odoo Migration")
    p.add_argument("--dry-run", action="store_true", help="Read JTL, do NOT write to Odoo")
    p.add_argument(
        "--module",
        default="all",
        help=(
            "Comma-separated list of modules to run: "
            "categories, manufacturers, products, variants, pricing, customers, orders, images  "
            "(default: all)"
        ),
    )
    p.add_argument("--image-dir", default="", help="Local path to JTL image folder")
    p.add_argument(
        "--order-from", default=None, help="Only import orders from this date (YYYY-MM-DD)"
    )
    p.add_argument(
        "--order-to", default=None, help="Only import orders up to this date (YYYY-MM-DD)"
    )
    return p.parse_args()


def run(args):
    cfg = migration_config
    cfg.dry_run = args.dry_run
    if args.order_from:
        cfg.order_date_from = args.order_from
    if args.order_to:
        cfg.order_date_to = args.order_to

    setup_logging(cfg)
    log = logging.getLogger("migrate")

    modules_all = {
        "categories", "manufacturers", "products",
        "variants", "pricing", "customers", "orders", "images",
    }
    if args.module == "all":
        modules = modules_all
    else:
        modules = {m.strip().lower() for m in args.module.split(",")}

    if cfg.dry_run:
        log.warning("⚠️  DRY-RUN mode — Odoo will NOT be modified.")

    start = time.time()
    log.info("=" * 60)
    log.info("JTL → Odoo Migration  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 60)

    # ── Connect ──────────────────────────────────────────────────── #
    reader = JTLReader(sql_config, cfg)
    writer = OdooWriter(odoo_config, cfg)

    reader.connect()
    writer.connect()

    stats = {}

    try:
        # 1. CATEGORIES
        if "categories" in modules:
            cats = reader.get_categories()
            stats["categories"] = writer.import_categories(cats)

        # 2. MANUFACTURERS
        if "manufacturers" in modules:
            mfrs = reader.get_manufacturers()
            stats["manufacturers"] = writer.import_manufacturers(mfrs)

        # 3. PRODUCTS (templates)
        if "products" in modules:
            prods = reader.get_products()
            stats["products"] = writer.import_products(prods)

        # 4. VARIANTS
        if "variants" in modules:
            attrs = reader.get_product_attributes()
            values = reader.get_attribute_values()
            combos = reader.get_variant_combinations()
            writer.import_attributes(attrs, values)
            stats["variants"] = writer.attach_variants(combos)

        # 5. PRICING
        if "pricing" in modules:
            price_groups = reader.get_price_groups()
            prices = reader.get_customer_prices()
            stats["pricing"] = writer.import_pricelists(price_groups, prices)

        # 6. CUSTOMERS
        if "customers" in modules:
            customers = reader.get_customers()
            stats["customers"] = writer.import_customers(customers)

        # 7. ORDERS
        if "orders" in modules:
            orders = reader.get_orders()
            order_ids = [o["jtl_id"] for o in orders]
            lines = reader.get_order_lines(order_ids) if order_ids else []
            stats["orders"] = writer.import_orders(orders, lines)

        # 8. IMAGES
        if "images" in modules:
            images = reader.get_product_images()
            stats["images"] = writer.import_images(images, args.image_dir)

    finally:
        reader.disconnect()

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info("Migration finished in %.1f s", elapsed)
    log.info("Summary: %s", json.dumps(stats, indent=2))
    log.info("ID mappings saved to: %s", cfg.mapping_file)
    log.info("=" * 60)
    return stats


if __name__ == "__main__":
    args = parse_args()
    run(args)
