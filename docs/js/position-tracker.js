import { db, auth, login, logout, onAuthStateChanged } from "./firebase.js";
import {
  ensureDefaultAccount,
  subscribeAccounts,
  resolveActiveAccountId,
  renderAccountSwitcher,
  getAccountSettings
} from "./accounts.js";

import {
  collection,
  doc,
  addDoc,
  getDoc,
  deleteDoc,
  updateDoc,
  query,
  orderBy,
  onSnapshot,
  serverTimestamp
} from "https://www.gstatic.com/firebasejs/11.9.0/firebase-firestore.js";

// `allPositions` / `allBookedPositions` hold every position across every
// account for this user (that's how Firestore gives them to us). `positions`
// / `bookedPositions` are the filtered-to-the-active-account view that the
// rest of this file (rendering, summaries, Kelly, heatmap, etc.) reads —
// keeping that split means none of the existing render logic below needs
// to know accounts exist at all.
let allPositions = [];
let allBookedPositions = [];
let positions = [];
let bookedPositions = [];
let portfolioSize = 0;
let unsubOpen = null;
let unsubBooked = null;
let unsubAccounts = null;
let activeAccountId = null;
let accountsCache = [];
let activeTab = "open"; // "open" | "booked"

const accountPanel = document.getElementById("accountPanel");
const accountSwitcher = document.getElementById("accountSwitcher");

// A position/booked-trade created before multi-account support has no
// accountId stored — treat those as belonging to whichever account is
// oldest (the one ensureDefaultAccount creates / migrates settings into),
// so nobody's existing data silently disappears from every view.
function matchesActiveAccount(p) {
  // Fail open: if accounts haven't loaded (or failed to load — e.g. a
  // Firestore rules issue), show every position rather than hiding
  // everything. Only filter once we actually know the active account.
  if (!activeAccountId) return true;
  const fallbackId = accountsCache[0]?.id;
  const owner = p.accountId || fallbackId;
  return owner === activeAccountId;
}

function applyAccountFilter() {
  positions = allPositions.filter(matchesActiveAccount);
  bookedPositions = allBookedPositions.filter(matchesActiveAccount);
}

// Column sorting state for the Open Positions / Booked Positions tables.
// dir: 1 = ascending, -1 = descending. key: null = original (insertion) order.
let openSort = { key: null, dir: -1 };
let bookedSort = { key: null, dir: -1 };

const loginBtn = document.getElementById("loginBtn");
const settingsDisplay = document.getElementById("settingsDisplay");
const tabOpenBtn = document.getElementById("tabOpenBtn");
const tabBookedBtn = document.getElementById("tabBookedBtn");
const openPanel = document.getElementById("openPanel");
const bookedPanel = document.getElementById("bookedPanel");

const bookModalOverlay = document.getElementById("bookModalOverlay");
const bookModalSymbol = document.getElementById("bookModalSymbol");
const bookExitPrice = document.getElementById("bookExitPrice");
const bookDateSold = document.getElementById("bookDateSold");
const bookModalCancel = document.getElementById("bookModalCancel");
const bookModalConfirm = document.getElementById("bookModalConfirm");
let pendingBookPosition = null;

tabOpenBtn.onclick = () => switchTab("open");
tabBookedBtn.onclick = () => switchTab("booked");

function switchTab(tab) {
  activeTab = tab;
  tabOpenBtn.classList.toggle("active", tab === "open");
  tabBookedBtn.classList.toggle("active", tab === "booked");
  openPanel.style.display = tab === "open" ? "" : "none";
  bookedPanel.style.display = tab === "booked" ? "" : "none";
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
  if (unsubOpen) { unsubOpen(); unsubOpen = null; }
  if (unsubBooked) { unsubBooked(); unsubBooked = null; }
  if (unsubAccounts) { unsubAccounts(); unsubAccounts = null; }

  if (user) {
    loginBtn.textContent = `Logout (${user.displayName || user.email})`;
    accountPanel.style.display = "";

    // Positions/booked trades load immediately and independently of the
    // accounts feature below — so if the accounts subcollection can't be
    // read (e.g. Firestore rules haven't been updated to allow it yet),
    // your existing data still shows up instead of the page going blank.
    subscribeToPositions(user.uid);
    subscribeToBooked(user.uid);

    try {
      await ensureDefaultAccount(user.uid);
      unsubAccounts = subscribeAccounts(user.uid, (accounts) => {
        accountsCache = accounts;
        activeAccountId = resolveActiveAccountId(user.uid, accounts);
        renderAccountSwitcher(
          accountSwitcher,
          user.uid,
          accounts,
          activeAccountId,
          (newId) => { activeAccountId = newId; onAccountSwitched(user.uid); },
          (newId) => { activeAccountId = newId; onAccountSwitched(user.uid); }
        );
        loadSettings(user.uid, activeAccountId);
        applyAccountFilter();
        renderAll();
        renderBooked();
      });
    } catch (err) {
      console.error(err);
      accountSwitcher.innerHTML =
        `<p class="login-status">Could not load accounts (check Firestore rules for users/{uid}/accounts). Showing all positions for now.</p>`;
    }
  } else {
    loginBtn.textContent = "Login with Google";
    accountPanel.style.display = "none";
    accountSwitcher.innerHTML = "";
    allPositions = [];
    allBookedPositions = [];
    positions = [];
    bookedPositions = [];
    accountsCache = [];
    activeAccountId = null;
    portfolioSize = 0;
    settingsDisplay.textContent = "Login to see your saved portfolio size & risk settings.";
    renderAll();
    renderBooked();
  }
});

function onAccountSwitched(uid) {
  loadSettings(uid, activeAccountId);
  applyAccountFilter();
  renderAll();
  renderBooked();
}

async function loadSettings(uid, accountId) {
  if (!accountId) return;
  try {
    const data = await getAccountSettings(uid, accountId);
    if (data) {
      portfolioSize = Number(data.portfolioSize) || 0;
      const riskLabel = data.riskType === "percent"
        ? `${data.riskValue}% of portfolio`
        : `₹${Number(data.riskValue).toLocaleString("en-IN")} fixed`;
      settingsDisplay.textContent =
        `Portfolio Size: ₹${portfolioSize.toLocaleString("en-IN")} · Risk per trade: ${riskLabel} ` +
        `— set on the Position Size Calculator page.`;
    } else {
      portfolioSize = 0;
      settingsDisplay.textContent = "No saved settings yet — set your portfolio size & risk on the Position Size Calculator page.";
    }
    renderAll();
  } catch (err) {
    console.error(err);
  }
}

function subscribeToPositions(uid) {
  const tbody = document.getElementById("positionsTable");
  tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:var(--subtle)">Loading…</td></tr>`;

  const positionsRef = collection(db, "users", uid, "positions");
  const positionsQuery = query(positionsRef, orderBy("createdAt", "desc"));

  unsubOpen = onSnapshot(
    positionsQuery,
    (snap) => {
      allPositions = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      applyAccountFilter();
      renderAll();
    },
    (err) => {
      console.error(err);
      tbody.innerHTML = `<tr><td colspan="11" style="text-align:center;color:var(--subtle)">Could not load positions.</td></tr>`;
    }
  );
}

function subscribeToBooked(uid) {
  const tbody = document.getElementById("bookedTable");
  tbody.innerHTML = `<tr><td colspan="13" style="text-align:center;color:var(--subtle)">Loading…</td></tr>`;

  const bookedRef = collection(db, "users", uid, "bookedPositions");
  const bookedQuery = query(bookedRef, orderBy("dateSold", "desc"));

  unsubBooked = onSnapshot(
    bookedQuery,
    (snap) => {
      allBookedPositions = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
      applyAccountFilter();
      renderBooked();
    },
    (err) => {
      console.error(err);
      tbody.innerHTML = `<tr><td colspan="13" style="text-align:center;color:var(--subtle)">Could not load booked trades.</td></tr>`;
    }
  );
}

function renderAll() {
  const tbody = document.getElementById("positionsTable");
  tbody.innerHTML = "";

  const rows = sortRows(positions, openSort.key, openSort.dir, openSortValue);

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="12"><div class="empty-state"><span class="icon">📭</span>No open positions yet — add one from the Position Size Calculator.</div></td></tr>`;
  } else {
    rows.forEach(renderRow);
  }
  updateSummary();
  updateSortIndicators("open");
}

function metrics(entry, riskPerShare, qty, currentPrice) {
  const pnlPct = ((currentPrice - entry) / entry) * 100;
  const rMultiple = riskPerShare > 0 ? (currentPrice - entry) / riskPerShare : 0;
  const impactAbs = (currentPrice - entry) * qty;
  const impactPct = portfolioSize > 0 ? (impactAbs / portfolioSize) * 100 : 0;
  return { pnlPct, rMultiple, impactAbs, impactPct };
}

function positionMetrics(p) {
  const currentPrice = Number(p.currentPrice ?? p.entry);
  const entry = Number(p.entry);
  const riskPerShare = Number(p.riskPerShare);
  const qty = Number(p.qty);
  return { currentPrice, ...metrics(entry, riskPerShare, qty, currentPrice) };
}

function pnlClass(value) {
  if (value > 0.001) return "pnl-pos";
  if (value < -0.001) return "pnl-neg";
  return "pnl-flat";
}

// ── Column sorting (P&L %, R Multiple, Portfolio Impact) ─────────────────────

function openSortValue(p, key) {
  const { pnlPct, rMultiple, impactPct } = positionMetrics(p);
  if (key === "pnlPct") return pnlPct;
  if (key === "rMultiple") return rMultiple;
  if (key === "impactPct") return impactPct;
  return null;
}

function bookedSortValue(p, key) {
  if (key === "pnlPct") return Number(p.pnlPct);
  if (key === "rMultiple") return Number(p.rMultiple);
  if (key === "impactPct") return Number(p.impactPct);
  return null;
}

function sortRows(arr, key, dir, valueFn) {
  if (!key) return arr;
  return arr
    .map((item, idx) => ({ item, idx, val: valueFn(item, key) }))
    .sort((a, b) => {
      const aBad = a.val === null || a.val === undefined || Number.isNaN(a.val);
      const bBad = b.val === null || b.val === undefined || Number.isNaN(b.val);
      if (aBad && bBad) return a.idx - b.idx;      // both unknown — keep original order
      if (aBad) return 1;                           // unknown values always sort last
      if (bBad) return -1;
      if (a.val === b.val) return a.idx - b.idx;    // stable tie-break
      return dir * (a.val - b.val);
    })
    .map((x) => x.item);
}

function updateSortIndicators(table) {
  const state = table === "open" ? openSort : bookedSort;
  document.querySelectorAll(`th.sortable[data-table="${table}"]`).forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    if (th.dataset.key === state.key) {
      arrow.textContent = state.dir === 1 ? "▲" : "▼";
      th.classList.add("sorted");
    } else {
      arrow.textContent = "";
      th.classList.remove("sorted");
    }
  });
}

document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const table = th.dataset.table;
    const key = th.dataset.key;
    const state = table === "open" ? openSort : bookedSort;

    if (state.key === key) {
      state.dir *= -1;          // clicking the same column flips direction
    } else {
      state.key = key;
      state.dir = -1;           // new column — start with highest value first
    }

    if (table === "open") renderAll();
    else renderBooked();
  });
});

function formatINR(n) {
  const sign = n < 0 ? "-" : "";
  return sign + "₹" + Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function formatDate(ts) {
  if (!ts) return "—";
  const d = typeof ts.toDate === "function" ? ts.toDate() : new Date(ts);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

function renderRow(p) {
  const tbody = document.getElementById("positionsTable");
  const tr = document.createElement("tr");
  tr.dataset.id = p.id;

  const { currentPrice, pnlPct, rMultiple, impactAbs, impactPct } = positionMetrics(p);
  const riskPctDisplay = typeof p.riskPct === "number" ? p.riskPct.toFixed(2) + "%" : "—";

  const stopValue = Number(p.stop);
  const slHit = Number.isFinite(stopValue) && stopValue > 0 && currentPrice <= stopValue;
  const slBadge = slHit ? `<span class="sl-hit-badge" title="Current price has fallen to or below the stop loss">SL HIT</span>` : "";

  tr.innerHTML = `
    <td>${slBadge}${escapeHtml(p.symbol)}</td>
    <td>${formatDate(p.dateBought)}</td>
    <td>
      ${p.entry}
      <input type="number" class="price-input entry-input" placeholder="override" data-id="${p.id}"/>
    </td>
    <td class="sl-value">
      ${p.stop}
      <input type="number" class="price-input stop-input" placeholder="override" data-id="${p.id}"/>
    </td>
    <td>${riskPctDisplay}</td>
    <td>
      ${p.qty}
      <input type="number" class="price-input qty-input" placeholder="override" data-id="${p.id}"/>
    </td>
    <td>
      ${currentPrice.toFixed(2)}
      <input type="number" class="price-input current-price-input" placeholder="override" data-id="${p.id}"/>
    </td>
    <td class="${pnlClass(pnlPct)}">${pnlPct.toFixed(2)}%</td>
    <td class="${pnlClass(rMultiple)}">${rMultiple.toFixed(2)}R</td>
    <td class="${pnlClass(impactAbs)}">${formatINR(impactAbs)} (${impactPct.toFixed(2)}%)</td>
    <td><button data-id="${p.id}" class="bookBtn" title="Book this position (move to Booked Positions)">✅</button></td>
    <td><button data-id="${p.id}" class="deleteBtn" title="Delete without booking">❌</button></td>
  `;
  tbody.appendChild(tr);

  const entryInput = tr.querySelector(".entry-input");
  entryInput.addEventListener("change", (e) =>
  updateEntry(p.id, e.target.value)
  );

  const stopInput = tr.querySelector(".stop-input");
  stopInput.addEventListener("change", (e) =>
  updateStop(p.id, e.target.value)
  );

  const qtyInput = tr.querySelector(".qty-input");
  qtyInput.addEventListener("change", (e) =>
  updateQty(p.id, e.target.value)
  );

  const currentPriceInput = tr.querySelector(".current-price-input");
  currentPriceInput.addEventListener("change", (e) =>
  updateCurrentPrice(p.id, e.target.value)
  );
  const delBtn = tr.querySelector(".deleteBtn");
  delBtn.onclick = () => deletePosition(p.id, tr);
  const bookBtn = tr.querySelector(".bookBtn");
  bookBtn.onclick = () => promptBookPosition(p);
}

async function updateEntry(id, value) {
  const entry = Number(value);

  if (!(entry > 0) || !Number.isFinite(entry) || !auth.currentUser) {
    alert("Please enter a valid entry price.");
    return;
  }

  try {
    await updateDoc(doc(db, "users", auth.currentUser.uid, "positions", id), { entry });
  } catch (err) {
    console.error(err);
    alert("Could not update entry price.");
  }
}

async function updateCurrentPrice(id, value) {
  const price = Number(value);
  if (!(price > 0) || !auth.currentUser) return;

  try {
    await updateDoc(doc(db, "users", auth.currentUser.uid, "positions", id), { currentPrice: price });
  } catch (err) {
    console.error(err);
    alert("Could not update price. Please try again.");
  }
}

async function updateStop(id, value) {
  const stop = Number(value);
  if (!(stop > 0) || !Number.isFinite(stop) || !auth.currentUser) {
    alert("Please enter a valid stop-loss price.");
    return;
  }

  try {
    await updateDoc(doc(db, "users", auth.currentUser.uid, "positions", id), { stop });
  } catch (err) {
    console.error(err);
    alert("Could not update stop-loss. Please try again.");
  }
}

async function updateQty(id, value) {
  const qty = Number(value);
  if (!(qty > 0) || !Number.isFinite(qty) || !auth.currentUser) {
    alert("Please enter a valid quantity greater than 0.");
    return;
  }

  try {
    await updateDoc(doc(db, "users", auth.currentUser.uid, "positions", id), { qty });
  } catch (err) {
    console.error(err);
    alert("Could not update quantity. Please try again.");
  }
}

async function deletePosition(id, rowEl) {
  if (!auth.currentUser) return;
  rowEl.style.opacity = "0.4";
  try {
    await deleteDoc(doc(db, "users", auth.currentUser.uid, "positions", id));
  } catch (err) {
    console.error(err);
    rowEl.style.opacity = "1";
    alert("Could not delete position. Please try again.");
  }
}

function promptBookPosition(p) {
  pendingBookPosition = p;
  bookModalSymbol.textContent = p.symbol;
  bookExitPrice.value = p.currentPrice ?? p.entry;
  bookDateSold.value = new Date().toISOString().slice(0, 10);
  bookModalOverlay.style.display = "flex";
  bookExitPrice.focus();
}

function closeBookModal() {
  bookModalOverlay.style.display = "none";
  pendingBookPosition = null;
}

bookModalCancel.onclick = closeBookModal;
bookModalOverlay.addEventListener("click", (e) => {
  if (e.target === bookModalOverlay) closeBookModal();
});

bookModalConfirm.onclick = () => {
  if (!pendingBookPosition) return;

  const exitPrice = Number(bookExitPrice.value);
  if (!(exitPrice > 0)) {
    alert("Please enter a valid exit price greater than 0.");
    return;
  }

  const dateSold = bookDateSold.value;
  if (!dateSold) {
    alert("Please select the date you sold.");
    return;
  }

  const p = pendingBookPosition;
  closeBookModal();
  bookPosition(p, exitPrice, dateSold);
};

async function bookPosition(p, exitPrice, dateSold) {
  if (!auth.currentUser) return;
  const uid = auth.currentUser.uid;

  const entry = Number(p.entry);
  const riskPerShare = Number(p.riskPerShare);
  const qty = Number(p.qty);
  const { pnlPct, rMultiple, impactAbs, impactPct } = metrics(entry, riskPerShare, qty, exitPrice);

  const bookedDoc = {
    accountId: p.accountId || accountsCache[0]?.id || activeAccountId,
    symbol: p.symbol,
    entry: p.entry,
    stop: p.stop,
    exitPrice,
    riskPct: p.riskPct ?? null,
    riskPerShare: p.riskPerShare ?? null,
    qty: p.qty,
    pnlPct,
    rMultiple,
    impactAbs,
    impactPct,
    portfolioSizeAtBooking: portfolioSize,
    dateBought: p.dateBought ?? null,   // entered manually on the Calculator page
    dateSold,                            // entered manually when booking, e.g. "2026-06-30"
    bookedAt: serverTimestamp(),         // internal metadata only, not shown
  };

  try {
    await addDoc(collection(db, "users", uid, "bookedPositions"), bookedDoc);
    await deleteDoc(doc(db, "users", uid, "positions", p.id));
    switchTab("booked");
  } catch (err) {
    console.error(err);
    alert("Could not book this position. Please try again.");
  }
}

function renderBooked() {
  const tbody = document.getElementById("bookedTable");
  tbody.innerHTML = "";

  const rows = sortRows(bookedPositions, bookedSort.key, bookedSort.dir, bookedSortValue);

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="13"><div class="empty-state"><span class="icon">📒</span>No booked trades yet — click ✅ on an open position to log it here once closed.</div></td></tr>`;
  } else {
    rows.forEach(renderBookedRow);
  }
  updateBookedSummary();
  updateSortIndicators("booked");
  renderHeatmap();
  updateAnalytics();
  updateKelly();
}

// ── Kelly Criterion, computed from the user's real booked trades ─────────
let kellyChartInstance = null;
const MIN_TRADES_FOR_KELLY = 5; // fewer than this and win-rate/R estimates are too noisy to trust

function kellyGrowthRate(f, W, a, b) {
  // g(f) = W*ln(1+b*f) + (1-W)*ln(1-a*f)
  const term1 = (1 + b * f) > 0 ? W * Math.log(1 + b * f) : -Infinity;
  const term2 = (1 - a * f) > 0 ? (1 - W) * Math.log(1 - a * f) : -Infinity;
  const g = term1 + term2;
  return Number.isFinite(g) ? g : null;
}

function updateKelly() {
  const nEl = document.getElementById("kellyN");
  const wrEl = document.getElementById("kellyWinRate");
  const winEl = document.getElementById("kellyAvgWin");
  const lossEl = document.getElementById("kellyAvgLoss");
  const fullEl = document.getElementById("kellyFull");
  const halfEl = document.getElementById("kellyHalf");
  const actualEl = document.getElementById("kellyActual");
  const verdictEl = document.getElementById("kellyVerdict");
  const noteEl = document.getElementById("kellyNote");
  if (!nEl) return; // panel not on this page

  const n = bookedPositions.length;
  nEl.textContent = n;

  // Effective R per trade: prefer the stored rMultiple, but fall back to
  // pnlPct / riskPct if rMultiple is missing/0/invalid (this happens when
  // riskPerShare wasn't set on a booking, which makes stored rMultiple
  // silently come out as 0 for that trade).
  function effectiveR(p) {
    const stored = Number(p.rMultiple);
    if (Number.isFinite(stored) && Math.abs(stored) > 0.0001) return stored;
    const pnl = Number(p.pnlPct);
    const risk = Number(p.riskPct);
    if (Number.isFinite(pnl) && Number.isFinite(risk) && risk > 0) return pnl / risk;
    return 0;
  }

  const rValues = bookedPositions.map(effectiveR);
  const winners = rValues.filter((r) => r > 0.001);
  const losers = rValues.filter((r) => r < -0.001);

  if (n < MIN_TRADES_FOR_KELLY) {
    wrEl.textContent = "—";
    winEl.textContent = "—";
    lossEl.textContent = "—";
    fullEl.textContent = "—";
    halfEl.textContent = "—";
    actualEl.textContent = "—";
    verdictEl.textContent = "Not enough data";
    verdictEl.className = "";
    noteEl.textContent = n === 0
      ? "Book some closed trades above — once you have at least " + MIN_TRADES_FOR_KELLY +
        " booked trades, your real Kelly sizing will appear here."
      : `You have ${n} booked trade${n === 1 ? "" : "s"} — Kelly needs at least ${MIN_TRADES_FOR_KELLY} to give a` +
        ` win-rate/R estimate that isn't mostly noise. Keep logging trades.`;
    if (kellyChartInstance) { kellyChartInstance.destroy(); kellyChartInstance = null; }
    const canvas = document.getElementById("kellyGrowthChart");
    if (canvas) { const ctx = canvas.getContext("2d"); ctx.clearRect(0, 0, canvas.width, canvas.height); }
    return;
  }

  const W = winners.length / n;
  const b = winners.length
    ? winners.reduce((s, r) => s + r, 0) / winners.length
    : 0;
  const a = losers.length
    ? Math.abs(losers.reduce((s, r) => s + r, 0) / losers.length)
    : 1; // fallback: assume a full 1R stop-out if somehow no losers logged

  wrEl.textContent = (W * 100).toFixed(0) + "%";
  winEl.textContent = b.toFixed(2) + "R";
  lossEl.textContent = "-" + a.toFixed(2) + "R";

  let fStar = (W / a) - ((1 - W) / b || 0);
  if (!Number.isFinite(fStar) || fStar < 0) fStar = 0;
  const halfKelly = fStar / 2;

  fullEl.textContent = (fStar * 100).toFixed(2) + "%";
  halfEl.textContent = (halfKelly * 100).toFixed(2) + "%";

  const riskVals = bookedPositions.map((p) => Number(p.riskPct)).filter((v) => Number.isFinite(v));
  const actualAvgRisk = riskVals.length ? riskVals.reduce((s, v) => s + v, 0) / riskVals.length / 100 : null;
  actualEl.textContent = actualAvgRisk !== null ? (actualAvgRisk * 100).toFixed(2) + "%" : "—";

  if (fStar <= 0) {
    verdictEl.textContent = "No edge — review strategy";
    verdictEl.className = "loss";
  } else if (actualAvgRisk === null) {
    verdictEl.textContent = "Kelly computed";
    verdictEl.className = "";
  } else if (actualAvgRisk > fStar) {
    verdictEl.textContent = "Over-sized — undertrade";
    verdictEl.className = "loss";
  } else if (actualAvgRisk < halfKelly) {
    verdictEl.textContent = "Under-sized vs. half-Kelly";
    verdictEl.className = "";
  } else {
    verdictEl.textContent = "Within a sensible Kelly range";
    verdictEl.className = "profit";
  }

  const dangerF = 2 * fStar; // beyond 2x Kelly, expected growth turns negative
  noteEl.innerHTML =
    `Based on ${n} booked trades: <b>${(W * 100).toFixed(0)}% win rate</b>, average winner ` +
    `<b>${b.toFixed(2)}R</b>, average loser <b>-${a.toFixed(2)}R</b>. Full Kelly says risk ` +
    `<b>${(fStar * 100).toFixed(2)}%</b> of your portfolio per trade for max long-run growth ` +
    `(green peak below) — but past <b>${(dangerF * 100).toFixed(2)}%</b> (roughly 2× Kelly), ` +
    `expected growth turns negative, so that's the line where you should actively cut size and ` +
    `start under-trading. Half-Kelly (<b>${(halfKelly * 100).toFixed(2)}%</b>) is the safer, ` +
    `commonly-used practical target given how few trades most traders have logged.`;

  // ── growth curve chart ──
  const maxF = Math.min(0.95, Math.max(dangerF * 1.3, fStar * 2.5, 0.1));
  const step = maxF / 80;
  const labels = [];
  const data = [];
  for (let f = 0; f <= maxF; f += step) {
    labels.push((f * 100).toFixed(1));
    data.push(kellyGrowthRate(f, W, a, b));
  }

  const canvas = document.getElementById("kellyGrowthChart");
  if (canvas && window.Chart) {
    if (kellyChartInstance) kellyChartInstance.destroy();
    kellyChartInstance = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Expected growth rate g(f)",
          data,
          borderColor: "#5b5fef",
          backgroundColor: "rgba(91,95,239,0.08)",
          fill: true,
          tension: 0.25,
          pointRadius: 0,
          borderWidth: 2.5,
        }],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { title: { display: true, text: "Capital risked per trade (%)" } },
          y: { title: { display: true, text: "Expected log-growth rate" } },
        },
      },
    });
  }
}

function holdingDays(p) {
  if (!p.dateBought || !p.dateSold) return "—";
  const bought = typeof p.dateBought.toDate === "function" ? p.dateBought.toDate() : new Date(p.dateBought);
  const sold = typeof p.dateSold.toDate === "function" ? p.dateSold.toDate() : new Date(p.dateSold);
  if (isNaN(bought.getTime()) || isNaN(sold.getTime())) return "—";
  const days = Math.max(0, Math.round((sold - bought) / 86400000));
  return `${days}d`;
}

function renderBookedRow(p) {
  const tbody = document.getElementById("bookedTable");
  const tr = document.createElement("tr");
  tr.dataset.id = p.id;

  const riskPctDisplay = typeof p.riskPct === "number" ? p.riskPct.toFixed(2) + "%" : "—";

  tr.innerHTML = `
    <td>${escapeHtml(p.symbol)}</td>
    <td>${formatDate(p.dateBought)}</td>
    <td>${formatDate(p.dateSold)}</td>
    <td>${holdingDays(p)}</td>
    <td>${p.entry}</td>
    <td class="sl-value">${p.stop}</td>
    <td>${p.exitPrice}</td>
    <td>${riskPctDisplay}</td>
    <td>${p.qty}</td>
    <td class="${pnlClass(p.pnlPct)}">${Number(p.pnlPct).toFixed(2)}%</td>
    <td class="${pnlClass(p.rMultiple)}">${Number(p.rMultiple).toFixed(2)}R</td>
    <td class="${pnlClass(p.impactAbs)}">${formatINR(p.impactAbs)} (${Number(p.impactPct).toFixed(2)}%)</td>
    <td><button data-id="${p.id}" class="deleteBtn" title="Delete this trade record">❌</button></td>
  `;
  tbody.appendChild(tr);

  const delBtn = tr.querySelector(".deleteBtn");
  delBtn.onclick = () => deleteBooked(p.id, tr);
}

async function deleteBooked(id, rowEl) {
  if (!auth.currentUser) return;
  if (!window.confirm("Delete this booked trade record permanently? This cannot be undone.")) return;
  rowEl.style.opacity = "0.4";
  try {
    await deleteDoc(doc(db, "users", auth.currentUser.uid, "bookedPositions", id));
  } catch (err) {
    console.error(err);
    rowEl.style.opacity = "1";
    alert("Could not delete trade record. Please try again.");
  }
}

function updateSummary() {
  document.getElementById("openCount").textContent = positions.length;

  // Total Capital always reflects the saved Portfolio Size, even with 0 open positions.
  const totalCapitalEl = document.getElementById("totalCapital");
  if (totalCapitalEl) totalCapitalEl.textContent = formatINR(portfolioSize);

  if (positions.length === 0) {
    document.getElementById("avgR").textContent = "0R";
    document.getElementById("winLoss").textContent = "0 / 0";
    document.getElementById("totalImpact").textContent = "₹0";
    document.getElementById("totalImpactPct").textContent = "0%";

    const costEl = document.getElementById("totalInvestedCost");
    if (costEl) costEl.textContent = "₹0 (0.00%)";
    const curEl = document.getElementById("totalInvestedCurrent");
    if (curEl) curEl.textContent = "₹0 (0.00%)";
    const cashEl = document.getElementById("totalCash");
    if (cashEl) {
      const pct = portfolioSize > 0 ? "100.00" : "0.00";
      cashEl.textContent = `${formatINR(portfolioSize)} (${pct}%)`;
      cashEl.className = pnlClass(portfolioSize);
    }
    return;
  }

  let totalR = 0;
  let winners = 0;
  let losers = 0;
  let totalImpactAbs = 0;
  let totalCostBasis = 0;
  let totalCurrentValue = 0;

  positions.forEach((p) => {
    const { rMultiple, impactAbs, currentPrice } = positionMetrics(p);
    const entry = Number(p.entry) || 0;
    const qty = Number(p.qty) || 0;
    totalR += rMultiple;
    totalImpactAbs += impactAbs;
    if (rMultiple > 0.001) winners++;
    else if (rMultiple < -0.001) losers++;
    totalCostBasis += entry * qty;
    totalCurrentValue += currentPrice * qty;
  });

  const totalImpactPct = portfolioSize > 0 ? (totalImpactAbs / portfolioSize) * 100 : 0;

  document.getElementById("avgR").textContent = (totalR / positions.length).toFixed(2) + "R";
  document.getElementById("winLoss").textContent = `${winners} / ${losers}`;

  const totalImpactEl = document.getElementById("totalImpact");
  totalImpactEl.textContent = formatINR(totalImpactAbs);
  totalImpactEl.className = pnlClass(totalImpactAbs);

  const totalImpactPctEl = document.getElementById("totalImpactPct");
  totalImpactPctEl.textContent = totalImpactPct.toFixed(2) + "%";
  totalImpactPctEl.className = pnlClass(totalImpactPct);

  // Total Invested — shown both ways: cost basis (capital actually deployed
  // at entry) and current market value (what those shares are worth today).
  // Each figure is also expressed as a % of total portfolio size.
  const costPct = portfolioSize > 0 ? (totalCostBasis / portfolioSize) * 100 : 0;
  const curPct  = portfolioSize > 0 ? (totalCurrentValue / portfolioSize) * 100 : 0;

  const costEl = document.getElementById("totalInvestedCost");
  if (costEl) costEl.textContent = `${formatINR(totalCostBasis)} (${costPct.toFixed(2)}%)`;

  const curEl = document.getElementById("totalInvestedCurrent");
  if (curEl) curEl.textContent = `${formatINR(totalCurrentValue)} (${curPct.toFixed(2)}%)`;

  // Total Cash = Portfolio Size minus capital actually deployed (cost basis),
  // i.e. what's still sitting uninvested / available for new trades.
  const totalCash = portfolioSize - totalCostBasis;
  const totalCashPct = portfolioSize > 0 ? (totalCash / portfolioSize) * 100 : 0;
  const cashEl = document.getElementById("totalCash");
  if (cashEl) {
    cashEl.textContent = `${formatINR(totalCash)} (${totalCashPct.toFixed(2)}%)`;
    cashEl.className = pnlClass(totalCash);
  }
}

function updateBookedSummary() {
  document.getElementById("bookedCount").textContent = bookedPositions.length;

  if (bookedPositions.length === 0) {
    document.getElementById("bookedAvgR").textContent = "0R";
    document.getElementById("bookedWinRate").textContent = "0%";
    document.getElementById("bookedTotalPnl").textContent = "₹0";
    document.getElementById("bookedTotalPnlPct").textContent = "0%";
    return;
  }

  let totalR = 0;
  let winners = 0;
  let totalImpactAbs = 0;
  let totalImpactPct = 0;

  bookedPositions.forEach((p) => {
    totalR += Number(p.rMultiple) || 0;
    totalImpactAbs += Number(p.impactAbs) || 0;
    totalImpactPct += Number(p.impactPct) || 0;
    if (Number(p.rMultiple) > 0.001) winners++;
  });

  const winRate = (winners / bookedPositions.length) * 100;

  document.getElementById("bookedAvgR").textContent = (totalR / bookedPositions.length).toFixed(2) + "R";
  document.getElementById("bookedWinRate").textContent = winRate.toFixed(0) + "%";

  const totalPnlEl = document.getElementById("bookedTotalPnl");
  totalPnlEl.textContent = formatINR(totalImpactAbs);
  totalPnlEl.className = pnlClass(totalImpactAbs);

  const totalPnlPctEl = document.getElementById("bookedTotalPnlPct");
  totalPnlPctEl.textContent = totalImpactPct.toFixed(2) + "%";
  totalPnlPctEl.className = pnlClass(totalImpactPct);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function dayKey(dateVal) {
  const d = typeof dateVal?.toDate === "function" ? dateVal.toDate() : new Date(dateVal);
  if (!dateVal || isNaN(d.getTime())) return null;
  return d.toISOString().slice(0, 10); // YYYY-MM-DD
}

function blendHex(hex1, hex2, t) {
  const c1 = parseInt(hex1.slice(1), 16);
  const c2 = parseInt(hex2.slice(1), 16);
  const r1 = (c1 >> 16) & 255, g1 = (c1 >> 8) & 255, b1 = c1 & 255;
  const r2 = (c2 >> 16) & 255, g2 = (c2 >> 8) & 255, b2 = c2 & 255;
  const r = Math.round(r1 + (r2 - r1) * t);
  const g = Math.round(g1 + (g2 - g1) * t);
  const b = Math.round(b1 + (b2 - b1) * t);
  return `rgb(${r},${g},${b})`;
}

const HEATMAP_COLORS = {
  profitLt: "#ecfdf5",
  profit: "#059669",
  lossLt: "#fef2f2",
  loss: "#dc2626",
};

function renderHeatmap() {
  const grid = document.getElementById("heatmapGrid");
  const rangeEl = document.getElementById("heatmapRange");
  const tooltip = document.getElementById("heatmapTooltip");
  if (!grid) return;
  grid.innerHTML = "";

  if (bookedPositions.length === 0) {
    grid.innerHTML = `<div class="empty-state" style="padding:1.5rem 0;"><span class="icon">🗓️</span>No booked trades yet to plot.</div>`;
    if (rangeEl) rangeEl.textContent = "";
    return;
  }

  const byDay = {}; // "YYYY-MM-DD" -> { total, trades: [] }
  let minDate = null;
  let maxDate = null;

  bookedPositions.forEach((p) => {
    const key = dayKey(p.dateSold);
    if (!key) return;
    const d = new Date(key + "T00:00:00");
    if (!minDate || d < minDate) minDate = d;
    if (!maxDate || d > maxDate) maxDate = d;
    if (!byDay[key]) byDay[key] = { total: 0, trades: [] };
    byDay[key].total += Number(p.impactAbs) || 0;
    byDay[key].trades.push(p);
  });

  if (!minDate || !maxDate) {
    grid.innerHTML = `<div class="empty-state" style="padding:1.5rem 0;"><span class="icon">🗓️</span>No dated trades yet to plot.</div>`;
    if (rangeEl) rangeEl.textContent = "";
    return;
  }

  if (rangeEl) {
    rangeEl.innerHTML = `🕐 ${minDate.toISOString().slice(0, 10)} to ${maxDate.toISOString().slice(0, 10)}`;
  }

  const start = new Date(minDate);
  start.setDate(start.getDate() - start.getDay());
  const end = new Date(maxDate);
  end.setDate(end.getDate() + (6 - end.getDay()));

  const maxAbs = Math.max(1, ...Object.values(byDay).map((v) => Math.abs(v.total)));
  const monthNames = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

  const weeks = [];
  let cur = new Date(start);
  while (cur <= end) {
    const week = [];
    for (let i = 0; i < 7; i++) {
      week.push(new Date(cur));
      cur.setDate(cur.getDate() + 1);
    }
    weeks.push(week);
  }

  const outer = document.createElement("div");

  const monthRow = document.createElement("div");
  monthRow.style.display = "flex";
  monthRow.style.gap = "3px";
  monthRow.style.marginBottom = "4px";

  const weeksRow = document.createElement("div");
  weeksRow.style.display = "flex";
  weeksRow.style.gap = "3px";

  let lastMonth = -1;
  weeks.forEach((week) => {
    const firstDay = week[0];
    const monthLbl = document.createElement("div");
    monthLbl.style.width = "13px";
    monthLbl.style.flex = "none";
    monthLbl.style.fontFamily = "var(--mono)";
    monthLbl.style.fontSize = ".65rem";
    monthLbl.style.color = "var(--subtle)";
    monthLbl.style.overflow = "visible";
    monthLbl.style.whiteSpace = "nowrap";
    if (firstDay.getMonth() !== lastMonth) {
      monthLbl.textContent = monthNames[firstDay.getMonth()];
      lastMonth = firstDay.getMonth();
    }
    monthRow.appendChild(monthLbl);

    const col = document.createElement("div");
    col.className = "heatmap-week";
    week.forEach((day) => {
      const key = day.toISOString().slice(0, 10);
      const cell = document.createElement("div");
      cell.className = "heatmap-day";

      if (day < minDate || day > maxDate) {
        cell.style.visibility = "hidden";
      } else {
        const info = byDay[key];
        if (info && Math.abs(info.total) > 0.0001) {
          cell.classList.add("has-trade");
          const intensity = Math.min(1, Math.abs(info.total) / maxAbs);
          cell.style.background =
            info.total > 0
              ? blendHex(HEATMAP_COLORS.profitLt, HEATMAP_COLORS.profit, intensity)
              : blendHex(HEATMAP_COLORS.lossLt, HEATMAP_COLORS.loss, intensity);

          cell.addEventListener("mouseenter", () => {
            tooltip.style.display = "block";
            const sign = info.total >= 0 ? "+" : "";
            tooltip.innerHTML = `<strong>${key}</strong><br/>${info.trades.length} trade${info.trades.length > 1 ? "s" : ""} · ${sign}${formatINR(info.total)}`;
          });
          cell.addEventListener("mousemove", (e) => {
            tooltip.style.left = e.clientX + 14 + "px";
            tooltip.style.top = e.clientY + 14 + "px";
          });
          cell.addEventListener("mouseleave", () => {
            tooltip.style.display = "none";
          });
        }
      }
      col.appendChild(cell);
    });
    weeksRow.appendChild(col);
  });

  outer.appendChild(monthRow);
  outer.appendChild(weeksRow);
  grid.appendChild(outer);
}

function updateAnalytics() {
  const setPnl = (id, val, suffix = "") => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = formatINR(val) + suffix;
    el.className = pnlClass(val);
  };

  if (bookedPositions.length === 0) {
    setPnl("maxProfitDay", 0);
    setPnl("maxLossDay", 0);
    setPnl("avgProfitTrade", 0);
    setPnl("avgLossTrade", 0);
    document.getElementById("bestTrade").textContent = "—";
    document.getElementById("bestTrade").className = "";
    document.getElementById("worstTrade").textContent = "—";
    document.getElementById("worstTrade").className = "";
    document.getElementById("profitFactor").textContent = "—";
    setPnl("expectancyTrade", 0);
    document.getElementById("avgHoldWin").textContent = "—";
    document.getElementById("avgHoldLoss").textContent = "—";
    document.getElementById("maxWinStreak").textContent = "0";
    document.getElementById("maxLossStreak").textContent = "0";
    return;
  }

  const byDay = {};
  bookedPositions.forEach((p) => {
    const key = dayKey(p.dateSold);
    if (!key) return;
    byDay[key] = (byDay[key] || 0) + (Number(p.impactAbs) || 0);
  });

  let maxProfitDayVal = 0;
  let maxLossDayVal = 0;
  Object.values(byDay).forEach((v) => {
    if (v > maxProfitDayVal) maxProfitDayVal = v;
    if (v < maxLossDayVal) maxLossDayVal = v;
  });

  const winners = bookedPositions.filter((p) => (Number(p.impactAbs) || 0) > 0.0001);
  const losers = bookedPositions.filter((p) => (Number(p.impactAbs) || 0) < -0.0001);
  const sum = (arr) => arr.reduce((s, p) => s + (Number(p.impactAbs) || 0), 0);

  const grossProfit = sum(winners);
  const grossLoss = Math.abs(sum(losers));
  const avgProfit = winners.length ? grossProfit / winners.length : 0;
  const avgLoss = losers.length ? -grossLoss / losers.length : 0;

  let best = null;
  let worst = null;
  bookedPositions.forEach((p) => {
    const v = Number(p.impactAbs) || 0;
    if (!best || v > (Number(best.impactAbs) || 0)) best = p;
    if (!worst || v < (Number(worst.impactAbs) || 0)) worst = p;
  });

  const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0;
  const expectancy = sum(bookedPositions) / bookedPositions.length;

  const avgHoldDays = (arr) => {
    const days = arr
      .map((p) => {
        const b = typeof p.dateBought?.toDate === "function" ? p.dateBought.toDate() : new Date(p.dateBought);
        const s = typeof p.dateSold?.toDate === "function" ? p.dateSold.toDate() : new Date(p.dateSold);
        if (!p.dateBought || !p.dateSold || isNaN(b.getTime()) || isNaN(s.getTime())) return null;
        return Math.max(0, Math.round((s - b) / 86400000));
      })
      .filter((d) => d !== null);
    return days.length ? days.reduce((a, b) => a + b, 0) / days.length : null;
  };

  const avgHoldWinDays = avgHoldDays(winners);
  const avgHoldLossDays = avgHoldDays(losers);

  const sorted = [...bookedPositions]
    .filter((p) => dayKey(p.dateSold))
    .sort((a, b) => dayKey(a.dateSold).localeCompare(dayKey(b.dateSold)));

  let curWinStreak = 0, maxWinStreak = 0, curLossStreak = 0, maxLossStreak = 0;
  sorted.forEach((p) => {
    const v = Number(p.impactAbs) || 0;
    if (v > 0.0001) {
      curWinStreak++;
      curLossStreak = 0;
      maxWinStreak = Math.max(maxWinStreak, curWinStreak);
    } else if (v < -0.0001) {
      curLossStreak++;
      curWinStreak = 0;
      maxLossStreak = Math.max(maxLossStreak, curLossStreak);
    } else {
      curWinStreak = 0;
      curLossStreak = 0;
    }
  });

  setPnl("maxProfitDay", maxProfitDayVal);
  setPnl("maxLossDay", maxLossDayVal);
  setPnl("avgProfitTrade", avgProfit);
  setPnl("avgLossTrade", avgLoss);

  const bestEl = document.getElementById("bestTrade");
  bestEl.textContent = `${best.symbol} · ${formatINR(Number(best.impactAbs) || 0)}`;
  bestEl.className = pnlClass(Number(best.impactAbs) || 0);

  const worstEl = document.getElementById("worstTrade");
  worstEl.textContent = `${worst.symbol} · ${formatINR(Number(worst.impactAbs) || 0)}`;
  worstEl.className = pnlClass(Number(worst.impactAbs) || 0);

  document.getElementById("profitFactor").textContent =
    grossLoss > 0 ? profitFactor.toFixed(2) : grossProfit > 0 ? "∞" : "—";

  setPnl("expectancyTrade", expectancy);

  document.getElementById("avgHoldWin").textContent = avgHoldWinDays !== null ? avgHoldWinDays.toFixed(1) + "d" : "—";
  document.getElementById("avgHoldLoss").textContent = avgHoldLossDays !== null ? avgHoldLossDays.toFixed(1) + "d" : "—";

  document.getElementById("maxWinStreak").textContent = maxWinStreak;
  document.getElementById("maxLossStreak").textContent = maxLossStreak;
}

switchTab("open");
