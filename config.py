"""
Configuration for JTL-Wawi → Odoo Migration Connector
"""

import os
from dataclasses import dataclass
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class SQLServerConfig:
    host: str = os.getenv("SQLSERVER_HOST", "localhost")
    port: int = int(os.getenv("SQLSERVER_PORT", "1433"))
    database: str = os.getenv("SQLSERVER_DB", "eazybusiness")
    username: str = os.getenv("SQLSERVER_USER", "sa")
    password: str = os.getenv("SQLSERVER_PASSWORD", "")
    driver: str = "ODBC Driver 17 for SQL Server"
    trust_server_certificate: bool = True

    @property
    def connection_string(self) -> str:
        return (
            f"mssql+pyodbc://{self.username}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
            f"?driver={self.driver.replace(' ', '+')}"
            f"&TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'}"
        )


@dataclass
class OdooConfig:
    host: str = os.getenv("ODOO_HOST", "https://your-odoo.hetzner.com")
    port: int = int(os.getenv("ODOO_PORT", "8069"))
    database: str = os.getenv("ODOO_DB", "odoo")
    username: str = os.getenv("ODOO_USER", "admin")
    password: str = os.getenv("ODOO_PASSWORD", "")
    api_key: Optional[str] = os.getenv("ODOO_API_KEY", None)

    @property
    def url(self) -> str:
        host = self.host.rstrip("/")
        default_port = 443 if host.startswith("https://") else 80
        return host if self.port == default_port else f"{host}:{self.port}"


@dataclass
class MigrationConfig:
    # Modules to migrate (set False to skip)
    migrate_categories: bool = True
    migrate_products: bool = True
    migrate_variants: bool = True
    migrate_pricing: bool = True
    migrate_customers: bool = True
    migrate_orders: bool = True
    migrate_images: bool = True

    # Behaviour
    batch_size: int = 100          # Records per Odoo RPC batch
    dry_run: bool = False          # If True: read JTL but don't write to Odoo
    log_level: str = "INFO"
    log_file: str = "migration.log"
    output_dir: str = "./output"

    # Odoo defaults
    default_lang: str = "de_DE"
    default_currency: str = "EUR"
    default_pricelist: str = "Standard"
    product_type: str = "product"  # 'product' (storable) | 'consu' | 'service'

    # JTL specifics
    active_customers_only: bool = True
    order_date_from: Optional[str] = None   # "YYYY-MM-DD" filter
    order_date_to: Optional[str] = None

    # ID mapping persistence
    mapping_file: str = "./output/id_mappings.json"


# Singleton instances – override via env vars or direct assignment
sql_config = SQLServerConfig()
odoo_config = OdooConfig()
migration_config = MigrationConfig()
