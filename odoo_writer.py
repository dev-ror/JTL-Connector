"""
Odoo XML-RPC Writer
Pushes normalised records into Odoo via the standard external API.
"""

import base64
import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xmlrpc.client

from config import OdooConfig, MigrationConfig

logger = logging.getLogger(__name__)

# JTL cStatus → Odoo sale.order state
_JTL_STATE_MAP: Dict[str, str] = {
    "N": "sale",   # Neu / In Bearbeitung
    "B": "sale",   # Bezahlt
    "V": "done",   # Versandt
    "A": "done",   # Abgeschlossen
}


# ─────────────────────────────────────────────────────────────────── #
#  ID Mapping store (JTL-id → Odoo-id)                                #
# ─────────────────────────────────────────────────────────────────── #

class IDMapping:
    """Persist bidirectional mappings so restarts are safe."""

    _SAVE_EVERY = 50  # flush to disk every N writes

    def __init__(self, path: str, dry_run: bool = False):
        self._path = Path(path)
        self._data: Dict[str, Dict[str, int]] = {}
        self._dry_run = dry_run
        self._dirty = 0
        if not dry_run:
            self._load()
        logger.debug("ID mappings loaded from %s", self._path)

    def _load(self):
        if self._path.exists():
            with self._path.open() as f:
                self._data = json.load(f)

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w") as f:
            json.dump(self._data, f, indent=2)
        self._dirty = 0

    def flush(self):
        """Write any pending changes to disk."""
        if not self._dry_run and self._dirty > 0:
            self.save()

    def set(self, entity: str, jtl_id: int, odoo_id: int):
        self._data.setdefault(entity, {})[str(jtl_id)] = odoo_id
        if not self._dry_run:
            self._dirty += 1
            if self._dirty >= self._SAVE_EVERY:
                self.save()

    def get(self, entity: str, jtl_id: int) -> Optional[int]:
        return self._data.get(entity, {}).get(str(jtl_id))

    def all(self, entity: str) -> Dict[str, int]:
        return self._data.get(entity, {})


# ─────────────────────────────────────────────────────────────────── #
#  Odoo RPC client                                                     #
# ─────────────────────────────────────────────────────────────────── #

class OdooWriter:
    def __init__(self, odoo_cfg: OdooConfig, mig_cfg: MigrationConfig):
        self.cfg = odoo_cfg
        self.mig = mig_cfg
        self.mapping = IDMapping(mig_cfg.mapping_file, dry_run=mig_cfg.dry_run)
        self._uid: Optional[int] = None
        self._models = None
        self._common = None
        self._dry_run_counter = 0  # fake IDs for dry-run creates, count downward

    # ---------------------------------------------------------------- #
    #  Auth                                                              #
    # ---------------------------------------------------------------- #

    def connect(self):
        url = self.cfg.url
        self._common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

        credential = self.cfg.api_key or self.cfg.password
        self._uid = self._common.authenticate(
            self.cfg.database, self.cfg.username, credential, {}
        )
        if not self._uid:
            raise ConnectionError("Odoo authentication failed – check credentials.")
        logger.info("✅  Connected to Odoo as uid=%d on %s", self._uid, url)

    def _exec(self, model: str, method: str, *args, **kwargs) -> Any:
        if self.mig.dry_run and method in ("create", "write", "unlink"):
            logger.debug("[DRY-RUN] %s.%s(%s)", model, method, args)
            if method == "create":
                self._dry_run_counter -= 1
                return self._dry_run_counter
            return True
        return self._models.execute_kw(
            self.cfg.database, self._uid,
            self.cfg.api_key or self.cfg.password,
            model, method, list(args), kwargs
        )

    def _find_or_create(self, model: str, domain: list, vals: dict) -> Tuple[int, bool]:
        """Return (id, created). Search first, create only if missing."""
        ids = self._exec(model, "search", domain, limit=1)
        if ids:
            return ids[0], False
        new_id = self._exec(model, "create", vals)
        return new_id, True

    def _batch_create(self, model: str, vals_list: List[dict]) -> List[int]:
        results = []
        for i in range(0, len(vals_list), self.mig.batch_size):
            chunk = vals_list[i : i + self.mig.batch_size]
            ids = self._exec(model, "create", chunk)
            if isinstance(ids, int):
                ids = [ids]
            results.extend(ids)
        return results

    # ---------------------------------------------------------------- #
    #  Helper: country / state lookup                                   #
    # ---------------------------------------------------------------- #

    def _country_id(self, code: str) -> Optional[int]:
        if not code:
            return None
        ids = self._exec("res.country", "search", [("code", "=", code.upper()[:2])], limit=1)
        return ids[0] if ids else None

    def _state_id(self, country_id: int, state_name: str) -> Optional[int]:
        if not state_name or not country_id:
            return None
        ids = self._exec(
            "res.country.state", "search",
            [("country_id", "=", country_id), ("name", "ilike", state_name)],
            limit=1,
        )
        return ids[0] if ids else None

    # ---------------------------------------------------------------- #
    #  1. Categories                                                     #
    # ---------------------------------------------------------------- #

    def import_categories(self, categories: List[Dict]) -> int:
        logger.info("→ Importing %d categories…", len(categories))
        ordered = self._topological_sort(categories, "jtl_id", "jtl_parent_id")
        count = 0
        for cat in ordered:
            parent_odoo_id = (
                self.mapping.get("category", cat["jtl_parent_id"])
                if cat["jtl_parent_id"]
                else None
            )
            vals = {
                "name": cat["name"] or "—",
                "parent_id": parent_odoo_id,
            }
            odoo_id, created = self._find_or_create(
                "product.category",
                [("name", "=", vals["name"]), ("parent_id", "=", parent_odoo_id)],
                vals,
            )
            self.mapping.set("category", cat["jtl_id"], odoo_id)
            if created:
                count += 1
        logger.info("  ✅ Categories: %d created, %d already existed", count, len(categories) - count)
        return count

    # ---------------------------------------------------------------- #
    #  2. Manufacturers → res.partner (supplier) or product.brand       #
    # ---------------------------------------------------------------- #

    def import_manufacturers(self, manufacturers: List[Dict]) -> int:
        logger.info("→ Importing %d manufacturers…", len(manufacturers))
        count = 0
        for m in manufacturers:
            odoo_id, created = self._find_or_create(
                "res.partner",
                [("name", "=", m["name"]), ("supplier_rank", ">", 0)],
                {"name": m["name"], "website": m.get("website", ""), "supplier_rank": 1},
            )
            self.mapping.set("manufacturer", m["jtl_id"], odoo_id)
            if created:
                count += 1
        return count

    # ---------------------------------------------------------------- #
    #  3. Products                                                       #
    # ---------------------------------------------------------------- #

    def import_products(self, products: List[Dict]) -> int:
        logger.info("→ Importing %d products…", len(products))
        count = 0
        for p in products:
            vals = {
                "name": p["name"] or p["default_code"] or "Unknown",
                "default_code": p.get("default_code"),
                "barcode": p.get("barcode") or False,
                "description_sale": p.get("description_sale", ""),
                "description": p.get("description", ""),
                "list_price": float(p.get("list_price") or 0.0),
                "standard_price": float(p.get("standard_price") or 0.0),
                "weight": float(p.get("weight") or 0.0),
                "type": self.mig.product_type,
                "active": bool(p.get("active", 1)),
                "sale_ok": True,
                "purchase_ok": True,
            }
            if p.get("jtl_manufacturer_id"):
                mfr_id = self.mapping.get("manufacturer", p["jtl_manufacturer_id"])
                if mfr_id:
                    # If OCA product_brand module is installed:
                    # vals["product_brand_id"] = mfr_id
                    pass

            domain = (
                [("default_code", "=", vals["default_code"])]
                if vals["default_code"]
                else [("name", "=", vals["name"])]
            )
            odoo_id, created = self._find_or_create("product.template", domain, vals)
            self.mapping.set("product", p["jtl_id"], odoo_id)
            if created:
                count += 1

        logger.info("  ✅ Products: %d created", count)
        return count

    # ---------------------------------------------------------------- #
    #  4. Variants & Attributes                                         #
    # ---------------------------------------------------------------- #

    def import_attributes(self, attributes: List[Dict], values: List[Dict]) -> int:
        """Create product.attribute + product.attribute.value records."""
        logger.info("→ Importing %d attributes / %d values…", len(attributes), len(values))
        for a in attributes:
            odoo_id, _ = self._find_or_create(
                "product.attribute",
                [("name", "=", a["name"])],
                {"name": a["name"], "create_variant": "always"},
            )
            self.mapping.set("attribute", a["jtl_id"], odoo_id)

        for v in values:
            attr_odoo_id = self.mapping.get("attribute", v["jtl_attribute_id"])
            if not attr_odoo_id:
                continue
            odoo_id, _ = self._find_or_create(
                "product.attribute.value",
                [("attribute_id", "=", attr_odoo_id), ("name", "=", v["name"])],
                {"attribute_id": attr_odoo_id, "name": v["name"]},
            )
            self.mapping.set("attribute_value", v["jtl_id"], odoo_id)
        return len(values)

    def attach_variants(self, combinations: List[Dict]) -> int:
        """Attach product.template.attribute.line records to parent templates."""
        logger.info("→ Attaching variant combinations…")
        tmpl_attr: Dict[Tuple, set] = defaultdict(set)

        for combo in combinations:
            tmpl_odoo_id = self.mapping.get("product", combo["jtl_variant_id"])
            attr_odoo_id = self.mapping.get("attribute", combo["jtl_attribute_id"])
            av_odoo_id   = self.mapping.get("attribute_value", combo["jtl_attr_value_id"])
            if tmpl_odoo_id and attr_odoo_id and av_odoo_id:
                tmpl_attr[(tmpl_odoo_id, attr_odoo_id)].add(av_odoo_id)

        count = 0
        for (tmpl_id, attr_id), value_ids in tmpl_attr.items():
            lines = self._exec(
                "product.template.attribute.line", "search",
                [("product_tmpl_id", "=", tmpl_id), ("attribute_id", "=", attr_id)],
            )
            if not lines:
                self._exec(
                    "product.template.attribute.line", "create",
                    {
                        "product_tmpl_id": tmpl_id,
                        "attribute_id": attr_id,
                        "value_ids": [(6, 0, list(value_ids))],
                    },
                )
                count += 1
        logger.info("  ✅ Variant attribute lines attached: %d", count)
        return count

    # ---------------------------------------------------------------- #
    #  5. Custom Pricing → product.pricelist                            #
    # ---------------------------------------------------------------- #

    def import_pricelists(self, price_groups: List[Dict], customer_prices: List[Dict]) -> int:
        logger.info("→ Importing pricelists…")
        for pg in price_groups:
            pl_id, _ = self._find_or_create(
                "product.pricelist",
                [("name", "=", pg["name"])],
                {"name": pg["name"], "currency_id": self._get_currency_id()},
            )
            self.mapping.set("pricelist", pg["jtl_id"], pl_id)

        count = 0
        for cp in customer_prices:
            pl_id = self.mapping.get("pricelist", cp.get("jtl_pricegroup_id") or 0)
            prod_tmpl_id = self.mapping.get("product", cp["jtl_product_id"])
            if not pl_id or not prod_tmpl_id:
                continue

            vals = {
                "pricelist_id": pl_id,
                "applied_on": "1_product",
                "product_tmpl_id": prod_tmpl_id,
                "min_quantity": float(cp.get("min_qty") or 1),
                "compute_price": "fixed",
                "fixed_price": float(cp.get("price_unit") or 0),
            }
            if cp.get("discount_pct"):
                vals["compute_price"] = "percentage"
                vals["percent_price"] = float(cp["discount_pct"])

            self._exec("product.pricelist.item", "create", vals)
            count += 1

        logger.info("  ✅ Pricelist items created: %d", count)
        return count

    def _get_currency_id(self) -> int:
        ids = self._exec(
            "res.currency", "search",
            [("name", "=", self.mig.default_currency)], limit=1,
        )
        return ids[0] if ids else 1

    # ---------------------------------------------------------------- #
    #  6. Customers                                                     #
    # ---------------------------------------------------------------- #

    def import_customers(self, customers: List[Dict]) -> int:
        logger.info("→ Importing %d customers…", len(customers))
        count = 0
        for c in customers:
            country_id = self._country_id(c.get("country_code", ""))
            state_id   = self._state_id(country_id, c.get("state", "")) if country_id else None

            name = " ".join(
                filter(None, [c.get("company_name"), c.get("firstname"), c.get("lastname")])
            ) or c.get("ref", "Unknown")

            vals = {
                "name": name.strip(),
                "ref": c.get("ref"),
                "email": c.get("email") or False,
                "phone": c.get("phone") or False,
                "mobile": c.get("mobile") or False,
                "vat": c.get("vat") or False,
                "is_company": bool(c.get("company_name")),
                "customer_rank": 1,
                "street": c.get("street", ""),
                "street2": c.get("street2", ""),
                "zip": c.get("zip", ""),
                "city": c.get("city", ""),
                "country_id": country_id,
                "state_id": state_id,
                "active": bool(c.get("active", 1)),
                "lang": self.mig.default_lang,
            }
            domain = [("ref", "=", c["ref"])] if c.get("ref") else [("email", "=", c.get("email"))]
            odoo_id, created = self._find_or_create("res.partner", domain, vals)
            self.mapping.set("customer", c["jtl_id"], odoo_id)
            if created:
                count += 1

        logger.info("  ✅ Customers: %d created", count)
        return count

    # ---------------------------------------------------------------- #
    #  7. Orders                                                        #
    # ---------------------------------------------------------------- #

    def import_orders(self, orders: List[Dict], order_lines: List[Dict]) -> int:
        logger.info("→ Importing %d orders…", len(orders))
        lines_by_order: Dict[int, List[Dict]] = defaultdict(list)
        for line in order_lines:
            lines_by_order[line["jtl_order_id"]].append(line)

        count = 0
        for order in orders:
            if self.mapping.get("order", order["jtl_id"]):
                continue

            partner_id = self.mapping.get("customer", order.get("jtl_customer_id") or 0)
            if not partner_id:
                logger.warning("  ⚠ Order %s: customer not found, skipping.", order["name"])
                continue

            odoo_state = _JTL_STATE_MAP.get(str(order.get("state", "")).upper(), "sale")

            order_vals = {
                "name": order["name"],
                "partner_id": partner_id,
                "date_order": str(order.get("date_order", "")),
                "note": order.get("note", ""),
                "state": odoo_state,
                "order_line": [],
            }

            for line in lines_by_order.get(order["jtl_id"], []):
                if line.get("line_type") not in (1, None):
                    continue  # skip text lines (nType=0); keep product lines (nType=1)
                prod_id = self._get_product_product_id(line.get("jtl_product_id"))
                if not prod_id:
                    logger.debug("    product not found for line %s", line.get("default_code"))
                    continue
                order_vals["order_line"].append(
                    (0, 0, {
                        "product_id": prod_id,
                        "name": line.get("product_name", ""),
                        "product_uom_qty": float(line.get("product_qty") or 1),
                        "price_unit": float(line.get("price_unit") or 0),
                        "discount": float(line.get("discount") or 0),
                    })
                )

            if not order_vals["order_line"]:
                logger.debug("  Order %s has no valid lines – skipping", order["name"])
                continue

            try:
                odoo_id = self._exec("sale.order", "create", order_vals)
                self.mapping.set("order", order["jtl_id"], odoo_id)
                count += 1
            except Exception as exc:
                logger.error("  ✗ Order %s failed: %s", order["name"], exc)

        logger.info("  ✅ Orders imported: %d", count)
        return count

    def _get_product_product_id(self, jtl_product_id: Optional[int]) -> Optional[int]:
        if not jtl_product_id:
            return None
        tmpl_id = self.mapping.get("product", jtl_product_id)
        if not tmpl_id:
            return None
        ids = self._exec("product.product", "search", [("product_tmpl_id", "=", tmpl_id)], limit=1)
        return ids[0] if ids else None

    # ---------------------------------------------------------------- #
    #  8. Product Images                                                #
    # ---------------------------------------------------------------- #

    def import_images(self, images: List[Dict], image_base_dir: str = "", reader=None) -> int:
        """Import product images. Supports two modes:
        - reader != None: binary data fetched from JTL DB via reader.get_image_data()
        - reader is None: binary data read from local file paths (image_base_dir + image_path)
        """
        logger.info("→ Importing product images…")
        count = 0
        for img in images:
            tmpl_id = self.mapping.get("product", img["jtl_product_id"])
            if not tmpl_id:
                continue

            # Load binary image data
            b64 = None
            if reader is not None and img.get("kBild"):
                b64 = reader.get_image_data(img["kBild"])
                if not b64:
                    logger.debug("  Image not found in DB: kBild=%s", img.get("kBild"))
                    continue
            else:
                path = img.get("image_path_large") or img.get("image_path", "")
                full_path = os.path.join(image_base_dir, path) if image_base_dir else path
                if not os.path.isfile(full_path):
                    logger.debug("  Image file not found: %s", full_path)
                    continue
                with open(full_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()

            try:
                if img["sort_order"] == 0:
                    self._exec("product.template", "write", [tmpl_id], {"image_1920": b64})
                else:
                    self._exec(
                        "product.image", "create",
                        {"product_tmpl_id": tmpl_id, "image_1920": b64, "sequence": img["sort_order"]},
                    )
                count += 1
            except Exception as exc:
                logger.error("  ✗ Image kBild=%s: %s", img.get("kBild", "?"), exc)
        logger.info("  ✅ Images uploaded: %d", count)
        return count

    # ---------------------------------------------------------------- #
    #  Utility: topological sort for category tree                     #
    # ---------------------------------------------------------------- #

    @staticmethod
    def _topological_sort(items, id_key, parent_key):
        by_id = {item[id_key]: item for item in items}
        visited = set()
        result = []

        def visit(item):
            if item[id_key] in visited:
                return
            visited.add(item[id_key])
            parent = item.get(parent_key)
            if parent and parent in by_id:
                visit(by_id[parent])
            result.append(item)

        for item in items:
            visit(item)
        return result
