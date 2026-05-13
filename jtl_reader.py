"""
JTL-Wawi SQL Server Reader
Queries the JTL eazybusiness database and returns normalised Python dicts.
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pyodbc  # noqa: F401 – required by pyodbc ODBC driver registration
import sqlalchemy
from sqlalchemy import create_engine, text

from config import SQLServerConfig, MigrationConfig

logger = logging.getLogger(__name__)


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
            ("products",       "SELECT COUNT(*) AS cnt FROM dbo.tArtikel WHERE nAktiv = 1"),
            ("attributes",     "SELECT COUNT(*) AS cnt FROM dbo.tEigenschaft"),
            ("price_groups",   "SELECT COUNT(*) AS cnt FROM dbo.tPreisgruppe"),
            ("customers",      "SELECT COUNT(*) AS cnt FROM dbo.tKunde"),
            ("orders",         "SELECT COUNT(*) AS cnt FROM dbo.tBestellung WHERE nStorniert = 0"),
            ("order_lines",    "SELECT COUNT(*) AS cnt FROM dbo.tBestellungPos"),
            ("product_images", "SELECT COUNT(*) AS cnt FROM dbo.tArtikelBild"),
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
        return {
            "categories": self._fetchall(
                f"SELECT TOP {n} kKategorie AS jtl_id, COALESCE(cName,'') AS name"
                " FROM dbo.tkategorie ORDER BY kKategorie"
            ),
            "products": self._fetchall(
                f"SELECT TOP {n} kArtikel AS jtl_id, cArtNr AS default_code, cName AS name"
                " FROM dbo.tArtikel WHERE nAktiv = 1 ORDER BY kArtikel"
            ),
            "customers": self._fetchall(
                f"SELECT TOP {n} kKunde AS jtl_id, cKundenNr AS ref, cNachname AS lastname"
                " FROM dbo.tKunde ORDER BY kKunde"
            ),
            "orders": self._fetchall(
                f"SELECT TOP {n} kBestellung AS jtl_id, cBestellNr AS name"
                " FROM dbo.tBestellung WHERE nStorniert = 0 ORDER BY dErstellt DESC"
            ),
        }

    # ------------------------------------------------------------------ #
    #  Categories                                                           #
    # ------------------------------------------------------------------ #

    def get_categories(self) -> List[Dict[str, Any]]:
        """Returns flat list; parent_id=None means root."""
        sql = """
            SELECT
                k.kKategorie          AS jtl_id,
                k.kOberKategorie      AS jtl_parent_id,
                k.nSort               AS sort_order,
                COALESCE(ks.cName, k.cName)        AS name,
                COALESCE(ks.cBeschreibung, '')      AS description,
                COALESCE(ks.cMetaTitle, '')         AS meta_title,
                COALESCE(ks.cMetaDescription, '')   AS meta_description
            FROM dbo.tkategorie k
            LEFT JOIN dbo.tkategoriesprache ks
                ON ks.kKategorie = k.kKategorie AND ks.cISOSprache = 'ger'
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
        still be referenced by historical orders — those order lines will be
        skipped during import."""
        sql = """
            SELECT
                a.kArtikel              AS jtl_id,
                a.kVaterArtikel         AS jtl_parent_id,
                a.cArtNr                AS default_code,
                a.cEAN                  AS barcode,
                COALESCE(as2.cName, a.cName)        AS name,
                COALESCE(as2.cBeschreibung, '')      AS description_sale,
                COALESCE(as2.cKurzBeschreibung, '') AS description,
                a.fVKNetto              AS list_price,
                a.fEKNetto              AS standard_price,
                a.fGewicht              AS weight,
                a.fBreite               AS product_width,
                a.fHoehe                AS product_height,
                a.fLaenge               AS product_length,
                a.nAktiv                AS active,
                a.cHAN                  AS manufacturer_ref,
                a.kHersteller           AS jtl_manufacturer_id,
                a.kSteuerklasse         AS jtl_tax_id,
                a.cLagerBeachten        AS track_inventory,
                a.fLagerbestand         AS qty_on_hand,
                a.cLieferstatus         AS delivery_status,
                a.fUVP                  AS msrp,
                a.dErstellt             AS create_date,
                a.dLetzteAktualisierung AS write_date
            FROM dbo.tArtikel a
            LEFT JOIN dbo.tArtikelsprache as2
                ON as2.kArtikel = a.kArtikel AND as2.cISOSprache = 'ger'
            WHERE a.nAktiv = 1
            ORDER BY a.kArtikel
        """
        rows = self._fetchall(sql)
        logger.info("  Products found: %d", len(rows))
        return rows

    # ------------------------------------------------------------------ #
    #  Variants (Kindartikel / Eigenschaftswerte)                           #
    # ------------------------------------------------------------------ #

    def get_product_attributes(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                e.kEigenschaft      AS jtl_id,
                e.kArtikel          AS jtl_product_id,
                COALESCE(es.cName, e.cName) AS name,
                e.cTyp              AS attr_type,
                e.nSort             AS sort_order
            FROM dbo.tEigenschaft e
            LEFT JOIN dbo.tEigenschaftsprache es
                ON es.kEigenschaft = e.kEigenschaft AND es.cISOSprache = 'ger'
            ORDER BY e.kArtikel, e.nSort
        """
        return self._fetchall(sql)

    def get_attribute_values(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                ew.kEigenschaftWert     AS jtl_id,
                ew.kEigenschaft         AS jtl_attribute_id,
                COALESCE(ews.cName, ew.cName) AS name,
                ew.nSort                AS sort_order
            FROM dbo.tEigenschaftWert ew
            LEFT JOIN dbo.tEigenschaftWertsprache ews
                ON ews.kEigenschaftWert = ew.kEigenschaftWert AND ews.cISOSprache = 'ger'
            ORDER BY ew.kEigenschaft, ew.nSort
        """
        return self._fetchall(sql)

    def get_variant_combinations(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                ekw.kArtikel            AS jtl_variant_id,
                ekw.kEigenschaftWert    AS jtl_attr_value_id,
                ew.kEigenschaft         AS jtl_attribute_id
            FROM dbo.tEigenschaftKombinationWert ekw
            JOIN dbo.tEigenschaftWert ew ON ew.kEigenschaftWert = ekw.kEigenschaftWert
            ORDER BY ekw.kArtikel
        """
        return self._fetchall(sql)

    # ------------------------------------------------------------------ #
    #  Custom Pricing                                                        #
    # ------------------------------------------------------------------ #

    def get_price_groups(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT kPreisgruppe AS jtl_id, cName AS name
            FROM dbo.tPreisgruppe
            ORDER BY kPreisgruppe
        """
        return self._fetchall(sql)

    def get_customer_prices(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                p.kPreis            AS jtl_id,
                p.kArtikel          AS jtl_product_id,
                p.kKunde            AS jtl_customer_id,
                p.kPreisgruppe      AS jtl_pricegroup_id,
                p.kKundengruppe     AS jtl_customergroup_id,
                pp.nAnzahlAb        AS min_qty,
                pp.fNettoPreis      AS price_unit,
                pp.fRabatt          AS discount_pct
            FROM dbo.tPreis p
            JOIN dbo.tPreisDetail pp ON pp.kPreis = p.kPreis
            ORDER BY p.kArtikel, p.kKunde, pp.nAnzahlAb
        """
        rows = self._fetchall(sql)
        logger.info("  Custom price records found: %d", len(rows))
        return rows

    # ------------------------------------------------------------------ #
    #  Customers                                                            #
    # ------------------------------------------------------------------ #

    def get_customers(self) -> List[Dict[str, Any]]:
        filter_sql = "AND k.nAktiv = 1" if self.mig.active_customers_only else ""
        sql = f"""
            SELECT
                k.kKunde                AS jtl_id,
                k.cKundenNr             AS ref,
                k.cFirma                AS company_name,
                k.cVorname              AS firstname,
                k.cNachname             AS lastname,
                k.cMail                 AS email,
                k.cTel                  AS phone,
                k.cMobil                AS mobile,
                k.cFax                  AS fax,
                k.cUSTID                AS vat,
                k.kKundengruppe         AS jtl_customergroup_id,
                k.nAktiv                AS active,
                k.dErstellt             AS create_date,
                ka.cStrasse             AS street,
                ka.cHausnummer          AS street2,
                ka.cPLZ                 AS zip,
                ka.cOrt                 AS city,
                ka.cLand                AS country_code,
                ka.cBundesland          AS state
            FROM dbo.tKunde k
            LEFT JOIN dbo.tKundenadresse ka
                ON ka.kKunde = k.kKunde AND ka.nTyp = 1
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
            date_filter += " AND b.dErstellt >= :date_from"
            params["date_from"] = self.mig.order_date_from
        if self.mig.order_date_to:
            date_filter += " AND b.dErstellt <= :date_to"
            params["date_to"] = self.mig.order_date_to

        sql = f"""
            SELECT
                b.kBestellung           AS jtl_id,
                b.cBestellNr            AS name,
                b.kKunde                AS jtl_customer_id,
                b.dErstellt             AS date_order,
                b.fGesamtbetragNetto    AS amount_untaxed,
                b.fGesamtbetragBrutto   AS amount_total,
                b.fVersandkostenNetto   AS shipping_cost,
                b.cStatus               AS state,
                b.cKommentar            AS note,
                b.cVersandartName       AS carrier_name,
                b.cZahlungsartName      AS payment_method,
                lk.cFirma               AS ship_company,
                lk.cVorname             AS ship_firstname,
                lk.cNachname            AS ship_lastname,
                lk.cStrasse             AS ship_street,
                lk.cHausnummer          AS ship_street2,
                lk.cPLZ                 AS ship_zip,
                lk.cOrt                 AS ship_city,
                lk.cLand                AS ship_country_code,
                rk.cFirma               AS bill_company,
                rk.cVorname             AS bill_firstname,
                rk.cNachname            AS bill_lastname,
                rk.cStrasse             AS bill_street,
                rk.cHausnummer          AS bill_street2,
                rk.cPLZ                 AS bill_zip,
                rk.cOrt                 AS bill_city,
                rk.cLand                AS bill_country_code
            FROM dbo.tBestellung b
            LEFT JOIN dbo.tLieferadresse lk ON lk.kBestellung = b.kBestellung
            LEFT JOIN dbo.tRechnungsadresse rk ON rk.kBestellung = b.kBestellung
            WHERE b.nStorniert = 0 {date_filter}
            ORDER BY b.dErstellt DESC
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
                bp.kBestellPos          AS jtl_id,
                bp.kBestellung          AS jtl_order_id,
                bp.kArtikel             AS jtl_product_id,
                bp.cArtNr               AS default_code,
                bp.cName                AS product_name,
                bp.nAnzahl              AS product_qty,
                bp.fVKPreisNetto        AS price_unit,
                bp.fRabatt              AS discount,
                bp.fMwSt                AS tax_rate,
                bp.cEAN                 AS barcode,
                bp.nPosTyp              AS line_type    -- 1=Artikel, 2=Versand, 3=Gutschein
            FROM dbo.tBestellungPos bp
            WHERE bp.kBestellung IN ({placeholders})
            ORDER BY bp.kBestellung, bp.kBestellPos
        """
        return self._fetchall(sql, params)

    # ------------------------------------------------------------------ #
    #  Product Images                                                       #
    # ------------------------------------------------------------------ #

    def get_product_images(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                ab.kArtikelBild         AS jtl_id,
                ab.kArtikel             AS jtl_product_id,
                ab.nSort                AS sort_order,
                ab.cPfad                AS image_path,
                ab.cPfadGross           AS image_path_large
            FROM dbo.tArtikelBild ab
            ORDER BY ab.kArtikel, ab.nSort
        """
        return self._fetchall(sql)

    # ------------------------------------------------------------------ #
    #  Manufacturers                                                        #
    # ------------------------------------------------------------------ #

    def get_manufacturers(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT kHersteller AS jtl_id, cName AS name, cHomepage AS website
            FROM dbo.tHersteller
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
        return self._fetchall(sql)
