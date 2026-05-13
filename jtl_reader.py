"""
JTL-Wawi SQL Server Reader
Queries the JTL eazybusiness database and returns normalised Python dicts.

Schema notes (this backup is a newer JTL version):
  - Orders live in schema Verkauf (Verkauf.tAuftrag, Verkauf.tAuftragPosition)
  - Product names/descs are in dbo.tArtikelBeschreibung (kSprache=1, kShop=0, kPlattform=1)
  - Category names are in dbo.tKategorieSprache (same language keys)
  - tArtikel uses cAktiv='Y' instead of nAktiv=1
  - Customer address details live in dbo.tAdresse, not directly on dbo.tkunde
  - Images are stored as binary blobs in dbo.tBild, linked via dbo.tArtikelbildPlattform
  - Variant combinations use dbo.tEigenschaftKombiWert (not tEigenschaftKombinationWert)
"""

import base64
import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pyodbc  # noqa: F401
import sqlalchemy
from sqlalchemy import create_engine, text

from config import SQLServerConfig, MigrationConfig

logger = logging.getLogger(__name__)

_LANG = 1    # kSprache = 1 (Deutsch)
_SHOP = 0    # kShop = 0 (base/default)
_PLAT = 1    # kPlattform = 1 (JTL-Wawi default platform)


class JTLReader:
    def __init__(self, sql_cfg: SQLServerConfig, mig_cfg: MigrationConfig):
        self.cfg = sql_cfg
        self.mig = mig_cfg
        self._engine: Optional[sqlalchemy.engine.Engine] = None

    # ------------------------------------------------------------------ #
    #  Connection                                                           #
    # ------------------------------------------------------------------ #

    def connect(self):
        self._engine = create_engine(
            self.cfg.connection_string,
            pool_pre_ping=True,
            pool_size=5,
            echo=False,
        )
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅  Connected to JTL SQL Server: %s / %s", self.cfg.host, self.cfg.database)

    def disconnect(self):
        if self._engine:
            self._engine.dispose()

    @contextmanager
    def _conn(self):
        with self._engine.connect() as conn:
            yield conn

    def _fetchall(self, sql: str, params: dict = None) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            result = conn.execute(text(sql), params or {})
            keys = result.keys()
            return [dict(zip(keys, row)) for row in result.fetchall()]

    # ------------------------------------------------------------------ #
    #  Connection-test helpers                                             #
    # ------------------------------------------------------------------ #

    def get_summary_stats(self) -> Dict[str, int]:
        """Return record counts for all entity types."""
        queries = [
            ("categories",     "SELECT COUNT(*) AS cnt FROM dbo.tkategorie"),
            ("manufacturers",  "SELECT COUNT(*) AS cnt FROM dbo.tHersteller"),
            ("products",       "SELECT COUNT(*) AS cnt FROM dbo.tArtikel WHERE cAktiv='Y'"),
            ("attributes",     "SELECT COUNT(*) AS cnt FROM dbo.teigenschaft"),
            ("customers",      "SELECT COUNT(*) AS cnt FROM dbo.tkunde WHERE cSperre='N' OR cSperre IS NULL"),
            ("orders",         "SELECT COUNT(*) AS cnt FROM Verkauf.tAuftrag WHERE nStorno=0"),
            ("order_lines",    "SELECT COUNT(*) AS cnt FROM Verkauf.tAuftragPosition"),
            ("product_images", "SELECT COUNT(*) AS cnt FROM dbo.tArtikelbildPlattform WHERE kShop=0 AND kPlattform=0"),
        ]
        stats: Dict[str, int] = {}
        for label, sql in queries:
            try:
                rows = self._fetchall(sql)
                stats[label] = rows[0]["cnt"] if rows else 0
            except Exception as exc:
                logger.warning("  Count query failed for %s: %s", label, exc)
                stats[label] = -1
        return stats

    def get_samples(self, limit: int = 5) -> Dict[str, List[Dict[str, Any]]]:
        """Return small sample rows from key tables for connection testing."""
        n = int(limit)
        samples: Dict[str, List[Dict[str, Any]]] = {}
        queries = {
            "categories": f"""
                SELECT TOP {n} k.kKategorie AS jtl_id, COALESCE(ks.cName,'') AS name
                FROM dbo.tkategorie k
                LEFT JOIN dbo.tKategorieSprache ks
                    ON ks.kKategorie = k.kKategorie AND ks.kSprache={_LANG}
                ORDER BY k.kKategorie
            """,
            "products": f"""
                SELECT TOP {n} a.kArtikel AS jtl_id, a.cArtNr AS default_code,
                       COALESCE(ab.cName, a.cArtNr, '') AS name
                FROM dbo.tArtikel a
                LEFT JOIN dbo.tArtikelBeschreibung ab
                    ON ab.kArtikel = a.kArtikel AND ab.kSprache={_LANG}
                       AND ab.kShop={_SHOP} AND ab.kPlattform={_PLAT}
                WHERE a.cAktiv = 'Y'
                ORDER BY a.kArtikel
            """,
            "customers": f"""
                SELECT TOP {n} k.kKunde AS jtl_id, k.cKundenNr AS ref,
                       COALESCE(a.cName,'') AS lastname
                FROM dbo.tkunde k
                LEFT JOIN dbo.tAdresse a ON a.kKunde = k.kKunde AND a.nTyp = 1
                ORDER BY k.kKunde
            """,
            "orders": f"""
                SELECT TOP {n} kAuftrag AS jtl_id, cAuftragsNr AS name
                FROM Verkauf.tAuftrag
                WHERE nStorno = 0
                ORDER BY dErstellt DESC
            """,
        }
        for label, sql in queries.items():
            try:
                samples[label] = self._fetchall(sql)
            except Exception as exc:
                logger.warning("  Sample query failed for %s: %s", label, exc)
                samples[label] = []
        return samples

    # ------------------------------------------------------------------ #
    #  Categories                                                           #
    # ------------------------------------------------------------------ #

    def get_categories(self) -> List[Dict[str, Any]]:
        """Returns flat list; jtl_parent_id=None means root."""
        sql = f"""
            SELECT
                k.kKategorie                        AS jtl_id,
                NULLIF(k.kOberKategorie, 0)         AS jtl_parent_id,
                k.nSort                             AS sort_order,
                COALESCE(ks.cName, '')              AS name,
                COALESCE(ks.cBeschreibung, '')      AS description,
                COALESCE(ks.cTitleTag, '')          AS meta_title,
                COALESCE(ks.cMetaDescription, '')   AS meta_description
            FROM dbo.tkategorie k
            LEFT JOIN dbo.tKategorieSprache ks
                ON ks.kKategorie = k.kKategorie
                AND ks.kSprache = {_LANG}
            WHERE k.cAktiv = 'Y'
            ORDER BY k.kOberKategorie, k.nSort
        """
        rows = self._fetchall(sql)
        logger.info("  Categories found: %d", len(rows))
        return rows

    # ------------------------------------------------------------------ #
    #  Products (Artikel)                                                   #
    # ------------------------------------------------------------------ #

    def get_products(self) -> List[Dict[str, Any]]:
        """Returns active products. Inactive products are excluded but may
        still be referenced by historical orders — those lines are skipped."""
        sql = f"""
            SELECT
                a.kArtikel                              AS jtl_id,
                NULLIF(a.kVaterArtikel, 0)              AS jtl_parent_id,
                a.cArtNr                                AS default_code,
                a.cBarcode                              AS barcode,
                COALESCE(ab.cName, a.cArtNr, '')        AS name,
                COALESCE(ab.cBeschreibung, '')           AS description_sale,
                COALESCE(ab.cKurzBeschreibung, '')       AS description,
                a.fVKNetto                              AS list_price,
                a.fEKNetto                              AS standard_price,
                a.fGewicht                              AS weight,
                a.fBreite                               AS product_width,
                a.fHoehe                                AS product_height,
                a.fLaenge                               AS product_length,
                CASE WHEN a.cAktiv='Y' THEN 1 ELSE 0 END AS active,
                a.cHAN                                  AS manufacturer_ref,
                a.kHersteller                           AS jtl_manufacturer_id,
                a.kSteuerklasse                         AS jtl_tax_id,
                a.cLagerAktiv                           AS track_inventory,
                a.nLagerbestand                         AS qty_on_hand,
                a.fUVP                                  AS msrp,
                a.dErstelldatum                         AS create_date,
                a.dMod                                  AS write_date
            FROM dbo.tArtikel a
            LEFT JOIN dbo.tArtikelBeschreibung ab
                ON ab.kArtikel = a.kArtikel
                AND ab.kSprache = {_LANG}
                AND ab.kShop = {_SHOP}
                AND ab.kPlattform = {_PLAT}
            WHERE a.cAktiv = 'Y'
            ORDER BY a.kArtikel
        """
        rows = self._fetchall(sql)
        logger.info("  Products found: %d", len(rows))
        return rows

    # ------------------------------------------------------------------ #
    #  Variants (Kindartikel / Eigenschaftswerte)                           #
    # ------------------------------------------------------------------ #

    def get_product_attributes(self) -> List[Dict[str, Any]]:
        sql = f"""
            SELECT
                e.kEigenschaft              AS jtl_id,
                e.kArtikel                  AS jtl_product_id,
                COALESCE(es.cName, '')      AS name,
                e.cTyp                      AS attr_type,
                e.nSort                     AS sort_order
            FROM dbo.teigenschaft e
            LEFT JOIN dbo.tEigenschaftSprache es
                ON es.kEigenschaft = e.kEigenschaft AND es.kSprache = {_LANG}
            ORDER BY e.kArtikel, e.nSort
        """
        return self._fetchall(sql)

    def get_attribute_values(self) -> List[Dict[str, Any]]:
        sql = f"""
            SELECT
                ew.kEigenschaftWert             AS jtl_id,
                ew.kEigenschaft                 AS jtl_attribute_id,
                COALESCE(ews.cName, ew.cArtNr, '') AS name,
                ew.nSort                        AS sort_order
            FROM dbo.teigenschaftwert ew
            LEFT JOIN dbo.tEigenschaftWertSprache ews
                ON ews.kEigenschaftWert = ew.kEigenschaftWert AND ews.kSprache = {_LANG}
            WHERE ew.cAktiv = 'Y'
            ORDER BY ew.kEigenschaft, ew.nSort
        """
        return self._fetchall(sql)

    def get_variant_combinations(self) -> List[Dict[str, Any]]:
        """Maps variant child Artikel → attribute value combinations."""
        sql = """
            SELECT
                a.kArtikel              AS jtl_variant_id,
                ekw.kEigenschaftWert    AS jtl_attr_value_id,
                ekw.kEigenschaft        AS jtl_attribute_id
            FROM dbo.tArtikel a
            JOIN dbo.tEigenschaftKombiWert ekw
                ON ekw.kEigenschaftKombi = a.kEigenschaftKombi
            WHERE a.kEigenschaftKombi > 0
              AND a.cAktiv = 'Y'
            ORDER BY a.kArtikel
        """
        return self._fetchall(sql)

    # ------------------------------------------------------------------ #
    #  Custom Pricing                                                        #
    # ------------------------------------------------------------------ #

    def get_price_groups(self) -> List[Dict[str, Any]]:
        """Return distinct customer groups as price groups (no tPreisgruppe in this schema)."""
        sql = """
            SELECT
                kKundenGruppe   AS jtl_id,
                cName           AS name
            FROM dbo.tKundenGruppe
            ORDER BY kKundenGruppe
        """
        try:
            return self._fetchall(sql)
        except Exception as exc:
            logger.warning("  Could not fetch price groups: %s", exc)
            return []

    def get_customer_prices(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                p.kPreis            AS jtl_id,
                p.kArtikel          AS jtl_product_id,
                p.kKunde            AS jtl_customer_id,
                p.kKundenGruppe     AS jtl_pricegroup_id,
                pp.nAnzahlAb        AS min_qty,
                pp.fNettoPreis      AS price_unit,
                pp.fRabatt          AS discount_pct
            FROM dbo.tPreis p
            JOIN dbo.tPreisDetail pp ON pp.kPreis = p.kPreis
            ORDER BY p.kArtikel, p.kKunde, pp.nAnzahlAb
        """
        try:
            rows = self._fetchall(sql)
            logger.info("  Custom price records found: %d", len(rows))
            return rows
        except Exception as exc:
            logger.warning("  Could not fetch customer prices: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    #  Customers                                                            #
    # ------------------------------------------------------------------ #

    def get_customers(self) -> List[Dict[str, Any]]:
        filter_sql = "AND (k.cSperre = 'N' OR k.cSperre IS NULL)" if self.mig.active_customers_only else ""
        sql = f"""
            SELECT
                k.kKunde                AS jtl_id,
                k.cKundenNr             AS ref,
                a.cFirma                AS company_name,
                a.cVorname              AS firstname,
                a.cName                 AS lastname,
                a.cMail                 AS email,
                a.cTel                  AS phone,
                a.cMobil                AS mobile,
                a.cFax                  AS fax,
                a.cUSTID                AS vat,
                k.kKundenGruppe         AS jtl_customergroup_id,
                CASE WHEN k.cSperre='N' OR k.cSperre IS NULL THEN 1 ELSE 0 END AS active,
                k.dErstellt             AS create_date,
                a.cStrasse              AS street,
                a.cZusatz               AS street2,
                a.cPLZ                  AS zip,
                a.cOrt                  AS city,
                a.cISO                  AS country_code,
                a.cBundesland           AS state
            FROM dbo.tkunde k
            LEFT JOIN dbo.tAdresse a
                ON a.kKunde = k.kKunde AND a.nTyp = 1
            WHERE 1=1 {filter_sql}
            ORDER BY k.kKunde
        """
        rows = self._fetchall(sql)
        logger.info("  Customers found: %d", len(rows))
        return rows

    # ------------------------------------------------------------------ #
    #  Orders                                                               #
    # ------------------------------------------------------------------ #

    def get_orders(self) -> List[Dict[str, Any]]:
        date_filter = ""
        params: dict = {}
        if self.mig.order_date_from:
            date_filter += " AND a.dErstellt >= :date_from"
            params["date_from"] = self.mig.order_date_from
        if self.mig.order_date_to:
            date_filter += " AND a.dErstellt <= :date_to"
            params["date_to"] = self.mig.order_date_to

        sql = f"""
            SELECT
                a.kAuftrag                  AS jtl_id,
                a.cAuftragsNr               AS name,
                a.kKunde                    AS jtl_customer_id,
                a.dErstellt                 AS date_order,
                COALESCE(e.fWertNetto, 0)   AS amount_untaxed,
                COALESCE(e.fWertBrutto, 0)  AS amount_total,
                vs.cName                    AS state,
                a.kVorgangsstatus           AS jtl_status_id,
                -- shipping address (nTyp=2)
                ls.cFirma                   AS ship_company,
                ls.cVorname                 AS ship_firstname,
                ls.cName                    AS ship_lastname,
                ls.cStrasse                 AS ship_street,
                ls.cZusatz                  AS ship_street2,
                ls.cPLZ                     AS ship_zip,
                ls.cOrt                     AS ship_city,
                ls.cISO                     AS ship_country_code,
                -- billing address (nTyp=1)
                lb.cFirma                   AS bill_company,
                lb.cVorname                 AS bill_firstname,
                lb.cName                    AS bill_lastname,
                lb.cStrasse                 AS bill_street,
                lb.cZusatz                  AS bill_street2,
                lb.cPLZ                     AS bill_zip,
                lb.cOrt                     AS bill_city,
                lb.cISO                     AS bill_country_code
            FROM Verkauf.tAuftrag a
            LEFT JOIN Verkauf.tAuftragEckdaten e ON e.kAuftrag = a.kAuftrag
            LEFT JOIN Verkauf.tVorgangsstatus vs  ON vs.kVorgangsstatus = a.kVorgangsstatus
            LEFT JOIN Verkauf.tAuftragAdresse ls  ON ls.kAuftrag = a.kAuftrag AND ls.nTyp = 2
            LEFT JOIN Verkauf.tAuftragAdresse lb  ON lb.kAuftrag = a.kAuftrag AND lb.nTyp = 1
            WHERE a.nStorno = 0 {date_filter}
            ORDER BY a.dErstellt DESC
        """
        rows = self._fetchall(sql, params)
        logger.info("  Orders found: %d", len(rows))
        return rows

    def get_order_lines(self, order_ids: List[int]) -> List[Dict[str, Any]]:
        if not order_ids:
            return []
        placeholders = ",".join([f":id{i}" for i in range(len(order_ids))])
        params = {f"id{i}": v for i, v in enumerate(order_ids)}
        sql = f"""
            SELECT
                ap.kAuftragPosition     AS jtl_id,
                ap.kAuftrag             AS jtl_order_id,
                ap.kArtikel             AS jtl_product_id,
                ap.cArtNr               AS default_code,
                ap.cName                AS product_name,
                ap.fAnzahl              AS product_qty,
                ap.fVkNetto             AS price_unit,
                ap.fRabatt              AS discount,
                ap.fMwSt                AS tax_rate,
                ap.nType                AS line_type    -- 1=Artikel, 0=Textposition
            FROM Verkauf.tAuftragPosition ap
            WHERE ap.kAuftrag IN ({placeholders})
            ORDER BY ap.kAuftrag, ap.nSort
        """
        return self._fetchall(sql, params)

    # ------------------------------------------------------------------ #
    #  Product Images (binary stored in DB)                                #
    # ------------------------------------------------------------------ #

    def get_product_images(self) -> List[Dict[str, Any]]:
        """Returns image metadata. Use get_image_data(kBild) for binary content."""
        sql = """
            SELECT
                abp.kArtikelbildPlattform   AS jtl_id,
                abp.kBild                   AS kBild,
                abp.kArtikel                AS jtl_product_id,
                abp.nNr                     AS sort_order
            FROM dbo.tArtikelbildPlattform abp
            WHERE abp.kShop = 0 AND abp.kPlattform = 0
            ORDER BY abp.kArtikel, abp.nNr
        """
        return self._fetchall(sql)

    def get_image_data(self, kBild: int) -> Optional[str]:
        """Fetch binary image for a single kBild; returns base64 string or None."""
        sql = "SELECT bBild FROM dbo.tBild WHERE kBild = :kBild"
        try:
            rows = self._fetchall(sql, {"kBild": kBild})
            if rows and rows[0]["bBild"]:
                return base64.b64encode(bytes(rows[0]["bBild"])).decode()
        except Exception as exc:
            logger.debug("  Image load failed kBild=%d: %s", kBild, exc)
        return None

    # ------------------------------------------------------------------ #
    #  Manufacturers                                                        #
    # ------------------------------------------------------------------ #

    def get_manufacturers(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT kHersteller AS jtl_id, cName AS name, cHomepage AS website
            FROM dbo.tHersteller
            WHERE cName IS NOT NULL AND cName != ''
            ORDER BY cName
        """
        return self._fetchall(sql)

    # ------------------------------------------------------------------ #
    #  Tax Classes                                                          #
    # ------------------------------------------------------------------ #

    def get_tax_classes(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                sk.kSteuerklasse    AS jtl_id,
                sk.cName            AS name,
                ss.fWert            AS tax_rate
            FROM dbo.tSteuerklasse sk
            LEFT JOIN dbo.tSteuersatz ss
                ON ss.kSteuerklasse = sk.kSteuerklasse AND ss.kSteuerzone = 1
        """
        try:
            return self._fetchall(sql)
        except Exception:
            return []
