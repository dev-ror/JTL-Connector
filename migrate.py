"""
JTL → Odoo Migration Orchestrator
Run:  python migrate.py [--dry-run] [--module categories,products,customers,orders]
      python migrate.py --test           # test JTL + Odoo connectivity only
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
        "--test", action="store_true",
        help="Test JTL SQL Server (and Odoo) connectivity and print record counts, then exit",
    )
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


# ── Connection-test screen ─────────────────────────────────────────── #

def test_connection(_args):
    """Test JTL SQL Server and Odoo connectivity; print record counts."""
    setup_logging(migration_config)

    SEP = "=" * 62
    print(SEP)
    print(f"JTL Connection Test  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(SEP)

    start = time.time()

    # ── SQL Server ──────────────────────────────────────────────────── #
    print(
        f"\nConnecting to SQL Server:"
        f"  {sql_config.host}:{sql_config.port} / {sql_config.database} …"
    )
    reader = JTLReader(sql_config, migration_config)
    try:
        reader.connect()
    except Exception as exc:
        print(f"\n✗  Connection FAILED: {exc}")
        sys.exit(1)

    print("Fetching record counts …\n")
    stats = reader.get_summary_stats()
    samples = reader.get_samples(limit=3)

    print(f"  {'Entity':<20s}  {'Count':>7s}  Sample")
    print("  " + "─" * 56)
    for entity, count in stats.items():
        count_str = f"{count:>7,d}" if count >= 0 else "  ERROR"
        sample_rows = samples.get(entity, [])
        if sample_rows:
            ids = [str(r.get("jtl_id", r.get("name", "?"))) for r in sample_rows]
            sample_str = "IDs: " + ", ".join(ids)
        else:
            sample_str = ""
        print(f"  {entity:<20s}  {count_str}  {sample_str}")

    reader.disconnect()

    # ── Odoo ────────────────────────────────────────────────────────── #
    print(f"\nConnecting to Odoo:  {odoo_config.url} / {odoo_config.database} …")
    writer = OdooWriter(odoo_config, migration_config)
    try:
        writer.connect()
        print("✅  Odoo authentication successful")
    except Exception as exc:
        print(f"⚠   Odoo connection failed: {exc}")

    elapsed = time.time() - start
    print(f"\n{SEP}")
    print(f"Test completed in {elapsed:.1f} s")
    print(SEP)


# ── Full migration ─────────────────────────────────────────────────── #

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

    reader = JTLReader(sql_config, cfg)
    writer = OdooWriter(odoo_config, cfg)

    reader.connect()
    writer.connect()

    stats = {}

    try:
        if "categories" in modules:
            cats = reader.get_categories()
            stats["categories"] = writer.import_categories(cats)

        if "manufacturers" in modules:
            mfrs = reader.get_manufacturers()
            stats["manufacturers"] = writer.import_manufacturers(mfrs)

        if "products" in modules:
            prods = reader.get_products()
            stats["products"] = writer.import_products(prods)

        if "variants" in modules:
            attrs = reader.get_product_attributes()
            values = reader.get_attribute_values()
            combos = reader.get_variant_combinations()
            writer.import_attributes(attrs, values)
            stats["variants"] = writer.attach_variants(combos)

        if "pricing" in modules:
            price_groups = reader.get_price_groups()
            prices = reader.get_customer_prices()
            stats["pricing"] = writer.import_pricelists(price_groups, prices)

        if "customers" in modules:
            customers = reader.get_customers()
            stats["customers"] = writer.import_customers(customers)

        if "orders" in modules:
            orders = reader.get_orders()
            order_ids = [o["jtl_id"] for o in orders]
            lines = reader.get_order_lines(order_ids)
            stats["orders"] = writer.import_orders(orders, lines)

        if "images" in modules:
            images = reader.get_product_images()
            stats["images"] = writer.import_images(images, args.image_dir)

    finally:
        reader.disconnect()
        writer.mapping.flush()

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info("Migration finished in %.1f s", elapsed)
    log.info("Summary: %s", json.dumps(stats, indent=2))
    log.info("ID mappings saved to: %s", cfg.mapping_file)
    log.info("=" * 60)
    return stats


if __name__ == "__main__":
    args = parse_args()
    if args.test:
        test_connection(args)
    else:
        run(args)
