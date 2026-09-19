// scripts/update-prices.js
//
// Standalone script (no Edge Functions needed) that:
//   1. Connects to Supabase Postgres using the SERVICE ROLE key (bypasses
//      Row Level Security — this script legitimately needs to touch every
//      user's rows, unlike the browser client which only ever sees its
//      own).
//   2. Reads every row in the "positions" table and the "watchlist" table.
//   3. Fetches a live price per unique symbol from Yahoo Finance, handling
//      the crumb/cookie handshake Yahoo now requires.
//   4. Writes currentPrice back to Postgres.
//
// Run manually with:  node scripts/update-prices.js
// Run on a schedule via the GitHub Actions workflow in
// .github/workflows/update-prices.yml
//
// SETUP: two GitHub Actions secrets replace the old FIREBASE_SERVICE_ACCOUNT:
//   SUPABASE_URL          — Project Settings -> API -> Project URL
//   SUPABASE_SERVICE_ROLE_KEY — Project Settings -> API -> service_role key
//                                (NEVER expose this in client-side code —
//                                it bypasses Row Level Security entirely)

const { createClient } = require("@supabase/supabase-js");
const fs = require("fs");
const path = require("path");

const supabase = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

// ── Yahoo Finance crumb/cookie handshake ──────────────────────────────────
let cachedCrumb = null;
let cachedCookie = null;

// Positions are stored with whatever the user typed into the Position Size
// Calculator (e.g. "PGIL", "reliance") — clean and suffix-free, which is
// what the UI displays. Yahoo Finance, however, requires the exchange
// suffix for NSE-listed tickers (PGIL.NS, not PGIL) or it 404s. Rather than
// changing what's stored/displayed everywhere else in the app, normalize
// only at the point of the Yahoo fetch: uppercase, and append ".NS" unless
// the symbol already carries a recognized exchange suffix (so a position
// someone deliberately entered as "SOMETICKER.BO" for BSE is left alone).
function toYahooSymbol(symbol) {
  const s = symbol.trim().toUpperCase();
  return /\.(NS|BO)$/.test(s) ? s : `${s}.NS`;
}

// ── Industry / Industry Group lookup ──────────────────────────────────────
// (unchanged from the Firestore version — this logic never touched
// Firestore/Postgres at all, it just reads local CSVs)
function parseCsv(text) {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
  if (!lines.length) return [];
  const headers = lines[0].split(",").map((h) => h.trim());
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    const row = {};
    headers.forEach((h, i) => { row[h] = (cells[i] || "").trim(); });
    return row;
  });
}

function loadIndustryLookup() {
  const lookup = new Map(); // uppercased symbol OR name -> {industryGroup, industry}
  const files = [
    { file: "data/NSE_Stocks.csv", keyCols: ["Symbol", "Name"] },
    { file: "data/SME_Stocks.csv", keyCols: ["Symbols", "Name"] }
  ];

  for (const { file, keyCols } of files) {
    const fullPath = path.join(__dirname, "..", file);
    if (!fs.existsSync(fullPath)) continue;
    const rows = parseCsv(fs.readFileSync(fullPath, "utf-8"));
    for (const row of rows) {
      const info = {
        industryGroup: row["Industry Group"] || "",
        industry: row["Industry"] || ""
      };
      for (const col of keyCols) {
        const key = (row[col] || "").trim().toUpperCase();
        if (key) lookup.set(key, info);
      }
    }
  }
  return lookup;
}

function lookupIndustry(lookup, symbol) {
  return lookup.get(symbol.trim().toUpperCase()) || { industryGroup: "", industry: "" };
}

async function getCrumbAndCookie() {
  if (cachedCrumb && cachedCookie) return { crumb: cachedCrumb, cookie: cachedCookie };

  const cookieRes = await fetch("https://fc.yahoo.com", {
    headers: { "User-Agent": "Mozilla/5.0" }
  });
  const setCookie = cookieRes.headers.get("set-cookie") || "";
  const cookie = setCookie.split(";")[0];

  const crumbRes = await fetch("https://query2.finance.yahoo.com/v1/test/getcrumb", {
    headers: { "User-Agent": "Mozilla/5.0", "Cookie": cookie }
  });
  const crumb = await crumbRes.text();

  if (!crumb || crumb.includes("<html")) {
    throw new Error("Failed to obtain Yahoo crumb token");
  }

  cachedCrumb = crumb;
  cachedCookie = cookie;
  return { crumb, cookie };
}

async function fetchLivePrice(symbol) {
  const { crumb, cookie } = await getCrumbAndCookie();
  const yahooSymbol = toYahooSymbol(symbol);

  const url = `https://query2.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yahooSymbol)}` +
              `?interval=1m&crumb=${encodeURIComponent(crumb)}`;

  const res = await fetch(url, {
    headers: { "User-Agent": "Mozilla/5.0", "Cookie": cookie }
  });

  if (!res.ok) throw new Error(`Yahoo returned HTTP ${res.status} for ${yahooSymbol}`);

  const data = await res.json();
  const meta = data?.chart?.result?.[0]?.meta;
  const price = meta?.regularMarketPrice;
  const previousClose = meta?.chartPreviousClose ?? meta?.previousClose ?? null;
  const change = typeof meta?.regularMarketChange === "number" ? meta.regularMarketChange : null;
  const changePercent = typeof meta?.regularMarketChangePercent === "number" ? meta.regularMarketChangePercent : null;

  if (typeof price !== "number" || price <= 0) {
    throw new Error(`No valid price in response for ${yahooSymbol}`);
  }

  return {
    price,
    previousClose: typeof previousClose === "number" ? previousClose : null,
    change,
    changePercent
  };
}

async function updateAllWatchlistPrices() {
  // No collectionGroup query needed here — "watchlist" is already a single
  // flat table across every user, with Row Level Security bypassed by the
  // service-role key, so one plain select sees every user's rows at once.
  const { data: rows, error } = await supabase
    .from("watchlist")
    .select('"userId", symbol, "industryGroup", industry');

  if (error) throw error;
  if (!rows.length) {
    console.log("No watchlist stocks — nothing to update.");
    return;
  }

  const lookup = loadIndustryLookup();
  const bySymbol = new Map(); // symbol -> [{userId, hasIndustry}]

  rows.forEach((row) => {
    const symbol = row.symbol;
    const hasIndustry = Boolean(row.industryGroup && row.industry);
    if (!bySymbol.has(symbol)) bySymbol.set(symbol, []);
    bySymbol.get(symbol).push({ userId: row.userId, hasIndustry });
  });

  let updated = 0;
  let failed = 0;
  const writes = [];

  for (const [symbol, refs] of bySymbol.entries()) {
    const { industryGroup, industry } = lookupIndustry(lookup, symbol);

    try {
      const live = await fetchLivePrice(symbol);
      const { price, previousClose } = live;
      const change = live.change !== null
        ? live.change
        : (typeof previousClose === "number" && previousClose > 0 ? price - previousClose : null);
      const changePercent = live.changePercent !== null
        ? live.changePercent
        : (change !== null && previousClose > 0 ? (change / previousClose) * 100 : null);

      refs.forEach(({ userId, hasIndustry }) => {
        const update = { currentPrice: price, previousClose, change, changePercent };
        // Only fill in industry fields if they're missing/blank — never
        // clobber a value the front-end already wrote at star-click time.
        if (!hasIndustry) {
          update.industryGroup = industryGroup || "";
          update.industry = industry || "";
        }
        writes.push(
          supabase.from("watchlist").update(update).eq("userId", userId).eq("symbol", symbol)
        );
      });
      updated += refs.length;
      console.log(`✓ [watchlist] ${symbol}: ${price}`);
    } catch (err) {
      console.warn(`✗ [watchlist] ${symbol}: ${err.message}`);
      failed += refs.length;
    }
  }

  const results = await Promise.all(writes);
  const writeErrors = results.filter((r) => r.error);
  if (writeErrors.length) console.warn(`${writeErrors.length} watchlist row(s) failed to write.`);

  console.log(`Watchlist done. updated=${updated} failed=${failed}`);
}

async function updateAllPrices() {
  // Same simplification as above: "positions" is one flat table across
  // every user, so a single select (service-role key bypasses RLS) finds
  // every open position regardless of which account it belongs to.
  const { data: rows, error } = await supabase.from("positions").select("id, symbol");

  if (error) throw error;
  if (!rows.length) {
    console.log("No open positions — nothing to update.");
    return;
  }

  const bySymbol = new Map(); // symbol -> [id, ...]

  rows.forEach((row) => {
    if (!bySymbol.has(row.symbol)) bySymbol.set(row.symbol, []);
    bySymbol.get(row.symbol).push(row.id);
  });

  let updated = 0;
  let failed = 0;
  const writes = [];

  for (const [symbol, ids] of bySymbol.entries()) {
    try {
      const { price } = await fetchLivePrice(symbol);
      writes.push(
        supabase.from("positions").update({ currentPrice: price }).in("id", ids)
      );
      updated += ids.length;
      console.log(`✓ ${symbol}: ${price}`);
    } catch (err) {
      console.warn(`✗ ${symbol}: ${err.message}`);
      failed += ids.length;
    }
  }

  const results = await Promise.all(writes);
  const writeErrors = results.filter((r) => r.error);
  if (writeErrors.length) console.warn(`${writeErrors.length} position batch(es) failed to write.`);

  console.log(`Done. updated=${updated} failed=${failed}`);
}

updateAllPrices()
  .then(() => updateAllWatchlistPrices())
  .then(() => process.exit(0))
  .catch((err) => {
    console.error("Fatal error:", err);
    process.exit(1);
  });