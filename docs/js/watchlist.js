// docs/js/watchlist.js
//
// Powers watchlist.html — now with support for MULTIPLE watchlists.
//   • Logged out  -> read/write localStorage only, no live price refresh.
//   • Logged in   -> subscribe to watchlistDefs + watchlist via Firestore,
//                    so the table re-renders the instant
//                    scripts/update-prices.js (run on the same schedule as
//                    Open Positions) writes a new currentPrice. No polling.
//
// See docs/js/watchlist-lists.js for the shared data-access layer.

import { db, auth, login, logout, onAuthStateChanged } from "./supabase.js";
import {
  DEFAULT_LIST_ID,
  getLocalListsSorted,
  createLocalList,
  renameLocalList,
  deleteLocalList,
  removeFromLocalList,
  addToLocalList,
  getLocalMeta,
  fetchRemoteLists,
  createRemoteList,
  renameRemoteList,
  deleteRemoteList,
  removeFromRemoteList,
  addToRemoteList,
  subscribeRemoteListDefs,
  subscribeRemoteSymbols
} from "./watchlist-lists.js";

const loginBtn      = document.getElementById("loginBtn");
const loginStatus    = document.getElementById("loginStatus");
const tableBody      = document.getElementById("wlTableBody");
const countBadgeNum  = document.querySelector("#wlCountBadge .n");
const addSymbolInput = document.getElementById("addSymbolInput");
const addSymbolBtn   = document.getElementById("addSymbolBtn");
const addSymbolMsg   = document.getElementById("addSymbolMsg");
const changeHeader   = document.getElementById("changeHeader");
const breadthAdvN    = document.getElementById("breadthAdvN");
const breadthDecN    = document.getElementById("breadthDecN");
const breadthFlatN   = document.getElementById("breadthFlatN");
const breadthNoDataN = document.getElementById("breadthNoDataN");
const breadthBarAdv  = document.getElementById("breadthBarAdv");
const breadthBarDec  = document.getElementById("breadthBarDec");
const breadthBarFlat = document.getElementById("breadthBarFlat");
const breadthVerdict = document.getElementById("breadthVerdict");
const wlTabs         = document.getElementById("wlTabs");
const wlNewListBtn   = document.getElementById("wlNewListBtn");
const wlRenameBtn    = document.getElementById("wlRenameBtn");
const wlDeleteBtn    = document.getElementById("wlDeleteBtn");

let currentUid = null;
let unsubDefs = null;
let unsubSymbols = null;

let listDefs = [];          // [{id, name, order}]
let activeListId = null;
let allSymbolDocs = [];     // Firestore: raw docs (with .lists array) OR local: flattened
let items = [];             // rows for the ACTIVE list only
let changeSortDir = null;   // null = default (by symbol), "asc" | "desc" = by changePercent

// ── tabs ──────────────────────────────────────────────────────────────
function renderTabs() {
  wlTabs.innerHTML = listDefs
    .map((l) => `<button class="wl-tab${l.id === activeListId ? " active" : ""}" data-id="${l.id}">${l.name}</button>`)
    .join("");
  wlTabs.querySelectorAll(".wl-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.id === activeListId) return;
      activeListId = btn.dataset.id;
      renderTabs();
      recomputeItems();
      render();
    });
  });
  wlDeleteBtn.style.display = listDefs.length > 1 ? "" : "none";
}

async function ensureListsLoaded() {
  if (currentUid) {
    listDefs = await fetchRemoteLists(currentUid);
  } else {
    listDefs = getLocalListsSorted();
  }
  if (!listDefs.some((l) => l.id === activeListId)) {
    activeListId = listDefs[0]?.id || DEFAULT_LIST_ID;
  }
  renderTabs();
}

wlNewListBtn.addEventListener("click", async () => {
  const name = prompt("Name this watchlist:", "");
  if (name === null) return;
  const clean = name.trim();
  if (!clean) return;
  wlNewListBtn.disabled = true;
  try {
    let id;
    if (currentUid) {
      id = await createRemoteList(currentUid, clean, (listDefs.at(-1)?.order ?? -1) + 1);
      listDefs = await fetchRemoteLists(currentUid);
    } else {
      id = createLocalList(clean);
      listDefs = getLocalListsSorted();
    }
    activeListId = id;
    renderTabs();
    recomputeItems();
    render();
  } catch (err) {
    console.error("Create watchlist failed:", err);
    alert(`Could not create the watchlist.\n\n${err.code || err.message || err}`);
  } finally {
    wlNewListBtn.disabled = false;
  }
});

wlRenameBtn.addEventListener("click", async () => {
  const cur = listDefs.find((l) => l.id === activeListId);
  if (!cur) return;
  const name = prompt("Rename this watchlist:", cur.name);
  if (name === null) return;
  const clean = name.trim();
  if (!clean || clean === cur.name) return;
  wlRenameBtn.disabled = true;
  try {
    if (currentUid) {
      await renameRemoteList(currentUid, activeListId, clean);
      listDefs = await fetchRemoteLists(currentUid);
    } else {
      renameLocalList(activeListId, clean);
      listDefs = getLocalListsSorted();
    }
    renderTabs();
  } catch (err) {
    console.error("Rename watchlist failed:", err);
    alert(`Could not rename the watchlist.\n\n${err.code || err.message || err}`);
  } finally {
    wlRenameBtn.disabled = false;
  }
});

wlDeleteBtn.addEventListener("click", async () => {
  const cur = listDefs.find((l) => l.id === activeListId);
  if (!cur || listDefs.length <= 1) return;
  if (!confirm(`Delete "${cur.name}"? Stocks that are only in this list will be removed from your watchlists.`)) return;
  wlDeleteBtn.disabled = true;
  try {
    if (currentUid) {
      await deleteRemoteList(currentUid, activeListId);
      listDefs = await fetchRemoteLists(currentUid);
    } else {
      deleteLocalList(activeListId);
      listDefs = getLocalListsSorted();
    }
    activeListId = listDefs[0]?.id || DEFAULT_LIST_ID;
    renderTabs();
    recomputeItems();
    render();
  } catch (err) {
    console.error("Delete watchlist failed:", err);
    alert(`Could not delete the watchlist.\n\n${err.code || err.message || err}`);
  } finally {
    wlDeleteBtn.disabled = false;
  }
});

// ── data → rows for the active list ─────────────────────────────────────
function recomputeItems() {
  if (currentUid) {
    items = allSymbolDocs
      .filter((d) => (d.lists || []).includes(activeListId))
      .map((d) => ({
        symbol: d.symbol || d.id,
        currentPrice: d.currentPrice ?? null,
        previousClose: d.previousClose ?? null,
        change: d.change ?? null,
        changePercent: d.changePercent ?? null,
        industryGroup: d.industryGroup || "—",
        industry: d.industry || "—"
      }));
  } else {
    const lists = getLocalListsSorted();
    const cur = lists.find((l) => l.id === activeListId);
    const meta = getLocalMeta();
    const symbols = cur ? cur.symbols : [];
    items = symbols.map((symbol) => ({
      symbol,
      currentPrice: meta[symbol]?.currentPrice ?? null,
      previousClose: meta[symbol]?.previousClose ?? null,
      change: meta[symbol]?.change ?? null,
      changePercent: meta[symbol]?.changePercent ?? null,
      industryGroup: meta[symbol]?.industryGroup ?? "—",
      industry: meta[symbol]?.industry ?? "—"
    }));
  }
}

function fmtPrice(p) {
  const n = Number(p);
  return Number.isFinite(n) && n > 0 ? `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—";
}

function pnlClass(value) {
  if (value > 0.001) return "pnl-pos";
  if (value < -0.001) return "pnl-neg";
  return "pnl-flat";
}

function fmtChange(change, changePercent) {
  if (!Number.isFinite(Number(change)) || !Number.isFinite(Number(changePercent))) {
    return `<span class="pnl-flat">—</span>`;
  }
  const c = Number(change);
  const pct = Number(changePercent);
  const sign = c > 0 ? "+" : "";
  return `<span class="${pnlClass(c)}">${sign}${c.toFixed(2)} (${sign}${pct.toFixed(2)}%)</span>`;
}

function computeBreadth(list) {
  const EPS = 0.05; // % — treat tiny wiggles as unchanged, not a real advance/decline
  let adv = 0, dec = 0, flat = 0, noData = 0;
  list.forEach((it) => {
    const cp = Number(it.changePercent);
    if (!Number.isFinite(cp)) { noData++; return; }
    if (cp > EPS) adv++;
    else if (cp < -EPS) dec++;
    else flat++;
  });
  return { adv, dec, flat, noData, tracked: adv + dec + flat };
}

function renderBreadth() {
  const { adv, dec, flat, noData, tracked } = computeBreadth(items);

  breadthAdvN.textContent = adv;
  breadthDecN.textContent = dec;
  breadthFlatN.textContent = flat;
  breadthNoDataN.textContent = noData;

  const total = adv + dec + flat + noData;
  if (total === 0) {
    breadthBarAdv.style.width = "0%";
    breadthBarDec.style.width = "0%";
    breadthBarFlat.style.width = "100%";
    breadthVerdict.innerHTML = `<span class="tag mixed">No data</span>This watchlist is empty — star some stocks to see breadth here.`;
    return;
  }

  breadthBarAdv.style.width = `${(adv / total) * 100}%`;
  breadthBarDec.style.width = `${(dec / total) * 100}%`;
  breadthBarFlat.style.width = `${((flat + noData) / total) * 100}%`;

  if (tracked === 0) {
    breadthVerdict.innerHTML = `<span class="tag mixed">No data</span>Waiting for the next scheduled price refresh — breadth will fill in once prices update.`;
    return;
  }

  const score = (adv - dec) / tracked;
  const advPct = Math.round((adv / tracked) * 100);
  const staleNote = noData > 0 ? ` (${noData} still waiting on a price refresh)` : "";

  let tagClass, tagText, verdict;
  if (score >= 0.4) {
    tagClass = "bullish";
    tagText = "Bullish tape";
    verdict = `${adv} of ${tracked} watchlist names (${advPct}%) are trading higher today, with only ${dec} down${staleNote}. Breadth is broadly positive — the tape is supportive of taking fresh long setups, though this only reflects this watchlist, not the full market.`;
  } else if (score <= -0.4) {
    tagClass = "bearish";
    tagText = "Bearish tape";
    verdict = `${dec} of ${tracked} watchlist names are trading lower today against just ${adv} advancing${staleNote}. Breadth is broadly negative — this is usually a day to be defensive: tighten stops on existing longs and hold off on fresh breakout entries until breadth improves.`;
  } else {
    tagClass = "mixed";
    tagText = "Mixed tape";
    verdict = `${adv} up vs ${dec} down out of ${tracked} tracked names${staleNote} — advances and declines are roughly balanced. No clear edge from breadth alone; be selective, favor your strongest setups, and size down on new entries.`;
  }

  breadthVerdict.innerHTML = `<span class="tag ${tagClass}">${tagText}</span>${verdict}`;
}

function render() {
  countBadgeNum.textContent = items.length;
  renderBreadth();

  if (!items.length) {
    tableBody.innerHTML = `<tr class="wl-empty-row"><td colspan="7">No stocks in this watchlist yet — click the ☆ next to any symbol on a dashboard and pick this list, or add a symbol above.</td></tr>`;
    return;
  }

  changeHeader.classList.toggle("sort-active", changeSortDir !== null);
  changeHeader.querySelector(".sort-i").textContent =
    changeSortDir === "asc" ? "↑" : changeSortDir === "desc" ? "↓" : "⇅";

  const sorted = items.slice().sort((a, b) => {
    if (changeSortDir === "asc" || changeSortDir === "desc") {
      const av = Number.isFinite(Number(a.changePercent)) ? Number(a.changePercent) : -Infinity;
      const bv = Number.isFinite(Number(b.changePercent)) ? Number(b.changePercent) : -Infinity;
      return changeSortDir === "asc" ? av - bv : bv - av;
    }
    return a.symbol.localeCompare(b.symbol);
  });

  tableBody.innerHTML = sorted
    .map(
      (it) => `
      <tr data-sym="${it.symbol}">
        <td>★</td>
        <td style="font-family:var(--mono);font-weight:600">${it.symbol}</td>
        <td>${fmtPrice(it.currentPrice)}</td>
        <td>${fmtChange(it.change, it.changePercent)}</td>
        <td>${it.industryGroup || "—"}</td>
        <td>${it.industry || "—"}</td>
        <td><button class="wl-remove-btn" data-sym="${it.symbol}">✕ Remove</button></td>
      </tr>`
    )
    .join("");

  tableBody.querySelectorAll(".wl-remove-btn").forEach((btn) => {
    btn.addEventListener("click", () => removeSymbol(btn.dataset.sym));
  });
}

async function removeSymbol(symbol) {
  if (currentUid) {
    try {
      await removeFromRemoteList(currentUid, activeListId, symbol);
    } catch (err) {
      console.error("Could not remove from Firestore:", err);
    }
  } else {
    removeFromLocalList(activeListId, symbol);
    recomputeItems();
    render();
  }
}

function showAddMsg(text, isError) {
  addSymbolMsg.textContent = text;
  addSymbolMsg.style.color = isError ? "var(--red)" : "var(--subtle)";
  if (text) setTimeout(() => { if (addSymbolMsg.textContent === text) addSymbolMsg.textContent = ""; }, 3000);
}

async function addSymbol() {
  const raw = (addSymbolInput.value || "").trim().toUpperCase();
  if (!raw) return;
  const symbol = raw.replace(/\.(NS|BO)$/, "");

  const alreadyIn = items.some((it) => it.symbol === symbol);
  if (alreadyIn) {
    showAddMsg(`${symbol} is already in this list.`, true);
    return;
  }

  addSymbolBtn.disabled = true;
  try {
    if (currentUid) {
      await addToRemoteList(currentUid, activeListId, symbol, {});
    } else {
      addToLocalList(activeListId, symbol, {});
      recomputeItems();
      render();
    }
    addSymbolInput.value = "";
    showAddMsg(`Added ${symbol}. Price and industry fill in on the next scheduled refresh.`, false);
  } catch (err) {
    console.error(err);
    showAddMsg(`Could not add ${symbol}.`, true);
  } finally {
    addSymbolBtn.disabled = false;
  }
}

addSymbolBtn.addEventListener("click", addSymbol);
addSymbolInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") addSymbol();
});

changeHeader.addEventListener("click", () => {
  changeSortDir = changeSortDir === null ? "desc" : changeSortDir === "desc" ? "asc" : null;
  render();
});

function subscribeToFirestore(uid) {
  tableBody.innerHTML = `<tr class="wl-empty-row"><td colspan="7">Loading…</td></tr>`;

  unsubDefs = subscribeRemoteListDefs(uid, (defs) => {
    listDefs = defs;
    if (!listDefs.some((l) => l.id === activeListId)) {
      activeListId = listDefs[0]?.id || DEFAULT_LIST_ID;
    }
    renderTabs();
  });

  unsubSymbols = subscribeRemoteSymbols(uid, (docs) => {
    allSymbolDocs = docs;
    recomputeItems();
    render();
  });
}

loginBtn.onclick = async () => {
  if (auth.currentUser) {
    await logout();
  } else {
    try {
      await login();
    } catch (err) {
      console.error(err);
      alert("Login failed. Please try again.");
    }
  }
};

onAuthStateChanged(auth, async (user) => {
  if (unsubDefs) { unsubDefs(); unsubDefs = null; }
  if (unsubSymbols) { unsubSymbols(); unsubSymbols = null; }

  if (user) {
    currentUid = user.uid;
    loginBtn.textContent = `Logout (${user.displayName || user.email})`;
    loginStatus.textContent =
      "Logged in — your watchlists sync across devices and Current Price refreshes automatically on the scheduled server job.";
    await ensureListsLoaded();
    subscribeToFirestore(user.uid);
  } else {
    currentUid = null;
    loginBtn.textContent = "Login with Google";
    loginStatus.textContent =
      "Login to sync your watchlists across devices and get live current-price refreshes (every scheduled run) like Open Positions. " +
      "Without login, watchlists are saved to this browser only and prices shown are the ones captured at the moment you starred them.";
    await ensureListsLoaded();
    recomputeItems();
    render();
  }
});

// ── TradingView export — plain "SYMBOL,SYMBOL,…" of the ACTIVE list ────
function _tvSymbolList() {
  return items.map((it) => (it.symbol || "").toUpperCase()).filter(Boolean);
}

window.downloadTVList = function () {
  const syms = _tvSymbolList();
  if (!syms.length) { alert("No symbols to export."); return; }
  const blob = new Blob([syms.join(",")], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "tradingview_watchlist.txt";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};

window.copyTVList = function () {
  const syms = _tvSymbolList();
  if (!syms.length) { alert("No symbols to copy."); return; }
  const text = syms.join(",");
  const btn = document.getElementById("tvCopyBtn");
  const done = () => {
    if (!btn) return;
    const orig = btn.dataset.origLabel || btn.textContent;
    btn.dataset.origLabel = orig;
    btn.textContent = "✓ Copied!";
    setTimeout(() => { btn.textContent = orig; }, 1600);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => _tvFallbackCopy(text, done));
  } else {
    _tvFallbackCopy(text, done);
  }
};

function _tvFallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { alert("Copy failed — please copy manually."); }
  ta.remove();
}

// Initial paint for the logged-out/default case, before onAuthStateChanged
// fires for the first time.
activeListId = DEFAULT_LIST_ID;
ensureListsLoaded().then(() => {
  recomputeItems();
  render();
});
