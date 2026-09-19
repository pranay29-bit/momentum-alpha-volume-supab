# data/

Place your `NSE_Stocks.csv` file here.

## Expected format

| Column   | Example    | Notes                                      |
|----------|------------|--------------------------------------------|
| `Symbol` | `RELIANCE`  | Plain NSE ticker without `.NS` suffix      |

The scanner automatically appends `.NS` when downloading from Yahoo Finance.

## Updating the list

1. Download the latest symbol list from [NSE India](https://www.nseindia.com/market-data/securities-available-for-trading).
2. Save it as `NSE_Stocks.csv` in this directory.
3. Ensure the symbol column is named `Symbol` (or update `SYMBOL_COLUMN` in `scanner/config.py`).


