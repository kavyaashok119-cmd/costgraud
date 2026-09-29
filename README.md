# CostGuard

**CostGuard** is a fast, lightweight FinOps CLI guardrail that parses Terraform JSON execution plans, queries the real Azure Retail Prices API for billable resources, calculates net monthly cost deltas, and enforces automated budget circuit breakers to block unexpected cloud spend.

---

## Features

- **Terraform Plan Analysis**: Reads `resource_changes` from Terraform JSON (`terraform show -json tfplan`).
- **Real-Time Azure Pricing API**: Queries public endpoint `https://prices.azure.com/api/retail/prices` on the fly for virtual machine consumption rates (excludes Spot and Low Priority meters).
- **SQLite Local Caching**: Persists pricing per `(sku, region, currency)` in `pricing_cache.db` to prevent repetitive API calls and rate-limiting.
- **Budget Circuit Breakers**:
  - Net increase $\le$ `--max-increase`: Prints `PASSED` and exits with code `0`.
  - Net increase $>$ `--max-increase`: Prints `FAILED`, triggers `[CIRCUIT BREAKER] Budget threshold breached. Deployment blocked.`, and exits with code `1`.
  - Invalid inputs / JSON: Exits with code `2`.
- **Fault-Tolerant Execution**: Gracefully skips non-billable resources (Resource Groups, VNets, Subnets, NSGs) and logs clear warnings on unknown SKUs or network issues without crashing.

---

## Project Structure

```text
├── costguard.py       # Core CLI script
├── requirements.txt   # Minimal dependencies (requests)
├── test-plan.json     # Sample Terraform plan JSON
├── pricing_cache.db   # SQLite pricing cache (auto-generated)
└── README.md          # Documentation
```

---

## Installation

Ensure Python 3.11+ is installed.

```bash
pip install -r requirements.txt
```

---

## Usage

### 1. Evaluate Terraform Plan File

```bash
python costguard.py --plan test-plan.json --max-increase 50
```

### 2. Read from Piped Stdin

```bash
# Linux / macOS
cat test-plan.json | python costguard.py --max-increase 50

# Windows PowerShell
Get-Content test-plan.json -Raw | python costguard.py --max-increase 50
```

### 3. CLI Arguments

| Argument | Description | Default |
|---|---|---|
| `--plan <path>` | Path to Terraform plan JSON file (or `-` for stdin) | Reads stdin if piped |
| `--max-increase <float>` | Maximum allowable net monthly cost increase | Optional (enforced if provided) |
| `--currency <code>` | ISO Currency Code (e.g. `USD`, `EUR`, `GBP`) | `USD` |
| `--clear-cache` | Clears local SQLite pricing cache | Disabled |

---

## Example Output

### Passing Run (Under Budget)

```text
============================================================
COSTGUARD: Azure Infrastructure Cost Impact Report
============================================================

Resource                         Action    SKU        Old       New       Delta
-------------------------------------------------------------------------------
azurerm_linux_virtual_machine.app
                                 CREATE    Standard_B2s
                                                     $0.00     $30.37    +$30.37

-------------------------------------------------------------------------------

Prior Monthly Total:     $0.00
Projected Monthly Total: $30.37
Net Monthly Impact:      +$30.37

Cache: 0 hits, 1 API lookup

POLICY VERDICT:
Budget Threshold: $50.00
Status: PASSED

============================================================
```

### Circuit Breaker Triggered (Over Budget)

```text
POLICY VERDICT:
Budget Threshold: $20.00
Status: FAILED
[CIRCUIT BREAKER] Budget threshold breached. Deployment blocked.

============================================================
```
*(Exit code: `1`)*

---

## How SQLite Caching Works

CostGuard uses Python's built-in `sqlite3` database stored at `pricing_cache.db`.

1. **Table Schema**:
   ```sql
   CREATE TABLE IF NOT EXISTS pricing_cache (
       sku TEXT NOT NULL,
       region TEXT NOT NULL,
       currency TEXT NOT NULL,
       hourly_rate REAL NOT NULL,
       cached_at INTEGER NOT NULL,
       PRIMARY KEY (sku, region, currency)
   );
   ```

2. **Lookup Workflow**:
   - For every billable VM, CostGuard queries `pricing_cache` with composite key `(sku, region, currency)`.
   - **Cache Hit**: If present, the stored `hourly_rate` is used immediately without issuing an HTTP request, and the cache hit counter increments.
   - **Cache Miss**: If absent, CostGuard queries the Azure Retail Prices API with OData filters, extracts the consumption rate, and saves it into `pricing_cache` along with a Unix timestamp `cached_at`.
   - **Cache Invalidation**: Passing `--clear-cache` executes `DELETE FROM pricing_cache;` to force fresh pricing lookups on demand.
