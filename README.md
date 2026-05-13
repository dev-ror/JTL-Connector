# JTL-Wawi → Odoo Migration Connector

A Python connector that reads your JTL-Wawi SQL Server backup and imports
**categories, products, variants, pricing, customers and order history** into Odoo
via the standard XML-RPC API.

---

## Architecture

```
JTL SQL Server (Docker)
  └─ jtl_reader.py  ──read──►  migrate.py  ──write──►  odoo_writer.py
                                                              │
                                                     Odoo (Hetzner/cloudpepper)
                                                     XML-RPC :8069 or :443
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | `python --version` |
| ODBC Driver 17 for SQL Server | See below |
| Running SQL Server Docker container | With JTL backup restored |
| Odoo instance (v16 or v17) | API key recommended |

### Install ODBC Driver (Ubuntu / Debian)
```bash
curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -
curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list \
  | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql17
```

### Install ODBC Driver (macOS)
```bash
brew install microsoft/mssql-release/msodbcsql17
```

---

## Setup

```bash
git clone <this-repo>
cd jtl_odoo_connector
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your SQL Server + Odoo credentials
```

---

## Restore JTL Backup to Docker SQL Server

```bash
# 1. Start the container
docker-compose up -d sqlserver

# 2. Copy your .bak file
cp /path/to/JTL_backup.bak ./backups/

# 3. Restore inside the container
docker exec -it jtl_sqlserver /opt/mssql-tools/bin/sqlcmd \
  -S localhost -U sa -P 'YourStrongPassword!' \
  -Q "RESTORE DATABASE [eazybusiness]
      FROM DISK='/backups/JTL_backup.bak'
      WITH MOVE 'eazybusiness'     TO '/var/opt/mssql/data/eazybusiness.mdf',
           MOVE 'eazybusiness_log' TO '/var/opt/mssql/data/eazybusiness_log.ldf',
           REPLACE"

# 4. Verify
docker exec -it jtl_sqlserver /opt/mssql-tools/bin/sqlcmd \
  -S localhost -U sa -P 'YourStrongPassword!' \
  -Q "SELECT name FROM sys.databases"
```

---

## Odoo: Enable API Access

1. Log in as **admin** → Settings → Technical → API Keys
2. Create a new key and paste it into `.env` as `ODOO_API_KEY`
3. Ensure `sale_management`, `product`, `account` modules are installed
4. For pricelists: enable **Sales → Configuration → Pricelists**

---

## Running the Migration

### Full migration (all modules)
```bash
python migrate.py
```

### Dry run (reads JTL, no Odoo writes)
```bash
python migrate.py --dry-run
```

### Individual modules
```bash
python migrate.py --module categories
python migrate.py --module products,variants
python migrate.py --module customers,orders
python migrate.py --module pricing
```

### Order history with date filter
```bash
python migrate.py --module orders --order-from 2022-01-01 --order-to 2024-12-31
```

### With product images (specify local JTL image folder)
```bash
python migrate.py --module images --image-dir /path/to/jtl/bilder
```

---

## Configuration (config.py / .env)

| Variable | Default | Description |
|---|---|---|
| `SQLSERVER_HOST` | `localhost` | Docker host IP |
| `SQLSERVER_PORT` | `1433` | SQL Server port |
| `SQLSERVER_DB` | `eazybusiness` | JTL database name |
| `ODOO_HOST` | — | Full Odoo URL |
| `ODOO_API_KEY` | — | Preferred auth method |
| `migrate_images` | `True` | Import product images |
| `batch_size` | `100` | RPC batch size |
| `default_lang` | `de_DE` | Customer language |
| `order_date_from` | `None` | Order filter start |

---

## Output Files

| File | Description |
|---|---|
| `output/migration.log` | Full run log with warnings |
| `output/id_mappings.json` | JTL ID → Odoo ID map (idempotency) |

The `id_mappings.json` file makes re-runs safe: already-migrated records are
detected and skipped rather than duplicated.

---

## JTL Database Tables Used

| JTL Table | Maps To | Odoo Model |
|---|---|---|
| `tkategorie` | Categories | `product.category` |
| `tArtikel` | Products | `product.template` |
| `tEigenschaft` | Attributes | `product.attribute` |
| `tEigenschaftWert` | Attr. Values | `product.attribute.value` |
| `tPreis / tPreisDetail` | Pricing | `product.pricelist.item` |
| `tKunde` | Customers | `res.partner` |
| `tBestellung` | Orders | `sale.order` |
| `tBestellungPos` | Order Lines | `sale.order.line` |
| `tArtikelBild` | Images | `product.image` |
| `tHersteller` | Manufacturers | `res.partner` (supplier) |

---

## Recommended Migration Order

1. `categories`
2. `manufacturers`
3. `products`
4. `variants`
5. `pricing`
6. `customers`
7. `orders`
8. `images` (slowest — do last or separately)

---

## Troubleshooting

**Connection refused to SQL Server**
- Check `docker ps` — is the container running?
- Confirm port 1433 is mapped: `docker-compose ps`
- Test: `docker exec -it jtl_sqlserver /opt/mssql-tools/bin/sqlcmd -S localhost -U sa -P '...' -Q "SELECT 1"`

**Odoo auth error**
- Verify `ODOO_DB` matches the database shown in Odoo Settings
- Try `ODOO_PASSWORD` if API key doesn't work

**Missing JTL tables**
- JTL version differences: table names changed slightly between v1.5 and v1.6+
- Run `SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'` to inspect your schema

**Product variants not creating**
- Odoo requires the `Variants` feature enabled: Sales → Configuration → Settings → Product Variants
