// docs/js/watchlist-star.js
//
// Shared ☆ / ★ watchlist button used on every generated scan dashboard
// (Momentum, Elite, Volume, Rocket, New RS High, Stage 4, SME Momentum,
// SME Elite). Clicking it opens a small popover asking which watchlist(s)
// to add the stock to — you can keep more than one watchlist and a stock
// can belong to several at once.
//
// Anonymous visitors: lists are kept in localStorage only (per-browser,
// no live price refresh).
// Logged-in visitors: lists are written to Firestore (see
// watchlist-lists.js for the schema) — the same data the Watchlist page
// (watchlist.html) reads, and the same collection scripts/update-prices.js
// refreshes on a schedule, so a starred stock's current price keeps
// updating live exactly like an Open Position.

import { auth, onAuthStateChanged } from "./firebase.js";
import {
  getLocalListsSorted,
  localSymbolLists,
  createLocalList,
  addToLocalList,
  removeFromLocalList,
  fetchRemoteLists,
  createRemoteList,
  addToRemoteList,
  removeFromRemoteList,
  subscribeRemoteListDefs,
  subscribeRemoteSymbols
} from "./watchlist-lists.js";

let currentUid = null;
let remoteListDefs = [];     // [{id, name, order}]
let remoteSymbolLists = {};  // { SYMBOL: [listId, …] }
let unsubDefs = null;
let unsubSymbols = null;

function symbolListIds(sym) {
  return currentUid ? (remoteSymbolLists[sym] || []) : localSymbolLists(sym);
}

function paintStars() {
  document.querySelectorAll(".wl-star").forEach((btn) => {
    const tr = btn.closest("tr");
    const sym = tr ? tr.dataset.sym : "";
    const on = symbolListIds(sym).length > 0;
    btn.classList.toggle("is-active", on);
    btn.textContent = on ? "★" : "☆";
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.title = on ? "In your watchlist(s) — click to manage" : "Add to a watchlist";
  });
}

function getListsForPickerSync() {
  return currentUid ? remoteListDefs : getLocalListsSorted();
}

// ── popover ───────────────────────────────────────────────────────────
let popoverEl = null;
let popoverSym = null;

function closePopover() {
  if (popoverEl) {
    popoverEl.remove();
    popoverEl = null;
    popoverSym = null;
  }
  document.removeEventListener("mousedown", onDocMouseDown, true);
  document.removeEventListener("keydown", onDocKeyDown, true);
  window.removeEventListener("scroll", closePopover, true);
}

function onDocMouseDown(e) {
  if (popoverEl && !popoverEl.contains(e.target)) closePopover();
}
function onDocKeyDown(e) {
  if (e.key === "Escape") closePopover();
}

async function getListsForPicker() {
  if (currentUid) {
    if (!remoteListDefs.length) remoteListDefs = await fetchRemoteLists(currentUid);
    return remoteListDefs;
  }
  return getLocalListsSorted();
}

async function toggleListMembership(listId, sym, close, indgrp, ind, turningOn) {
  const meta = {
    currentPrice: close && !isNaN(parseFloat(close)) ? parseFloat(close) : null,
    industryGroup: indgrp,
    industry: ind
  };
  if (currentUid) {
    if (turningOn) await addToRemoteList(currentUid, listId, sym, meta);
    else await removeFromRemoteList(currentUid, listId, sym);
  } else {
    if (turningOn) addToLocalList(listId, sym, meta);
    else removeFromLocalList(listId, sym);
    paintStars(); // no live Firestore snapshot when logged out
  }
}

async function renderPopover(btn, sym, close, indgrp, ind) {
  closePopover();

  const lists = await getListsForPicker();
  const active = new Set(symbolListIds(sym));

  const box = document.createElement("div");
  box.className = "wl-picker";
  box.innerHTML = `
    <div class="wl-picker-title">Add <span class="wl-picker-sym">${sym}</span> to…</div>
    <div class="wl-picker-list"></div>
    <form class="wl-picker-new" autocomplete="off">
      <input type="text" class="wl-picker-input" placeholder="+ New watchlist name" maxlength="40"/>
      <button type="submit" class="wl-picker-create" title="Create watchlist">Create</button>
    </form>
  `;

  const listWrap = box.querySelector(".wl-picker-list");

  function paintRows(allLists) {
    listWrap.innerHTML = allLists.length
      ? allLists.map((l) => `
        <label class="wl-picker-row" data-list="${l.id}">
          <input type="checkbox" ${active.has(l.id) ? "checked" : ""}/>
          <span class="wl-picker-name">${l.name}</span>
        </label>
      `).join("")
      : `<div class="wl-picker-empty">No watchlists yet — create one below.</div>`;

    listWrap.querySelectorAll(".wl-picker-row").forEach((row) => {
      row.addEventListener("click", async (e) => {
        e.preventDefault(); // stop the native checkbox toggle — we set .checked ourselves below,
        // because by the time this handler runs, the browser may have ALREADY flipped
        // cb.checked as part of its default click behavior (timing differs depending on
        // whether you clicked the checkbox itself or the label text next to it). Reading
        // cb.checked here would be unreliable, so we derive intent from `active` instead —
        // our own source of truth for which lists this symbol currently belongs to.
        const listId = row.dataset.list;
        const cb = row.querySelector("input");
        const turningOn = !active.has(listId);
        row.classList.add("wl-picker-busy");
        try {
          await toggleListMembership(listId, sym, close, indgrp, ind, turningOn);
          if (turningOn) active.add(listId); else active.delete(listId);
        } catch (err) {
          console.error("Watchlist update failed:", err);
          alert(`Could not update watchlist.\n\n${err.code || err.message || err}`);
        } finally {
          cb.checked = active.has(listId);
          row.classList.remove("wl-picker-busy");
        }
        paintStars();
      });
    });
  }
  paintRows(lists);

  const form = box.querySelector(".wl-picker-new");
  const input = box.querySelector(".wl-picker-input");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) return;
    const createBtn = box.querySelector(".wl-picker-create");
    createBtn.disabled = true;
    try {
      let listId;
      if (currentUid) {
        const nextOrder = (remoteListDefs.at(-1)?.order ?? -1) + 1;
        listId = await createRemoteList(currentUid, name, nextOrder);
        remoteListDefs = [...remoteListDefs, { id: listId, name, order: nextOrder }];
      } else {
        listId = createLocalList(name);
      }
      input.value = "";
      paintRows(getListsForPickerSync());

      // Auto-add the stock to the list you just created.
      const newRow = listWrap.querySelector(`.wl-picker-row[data-list="${listId}"] input`);
      if (newRow) {
        await toggleListMembership(listId, sym, close, indgrp, ind, true);
        newRow.checked = true;
        active.add(listId);
        paintStars();
      }
    } catch (err) {
      console.error("Create watchlist failed:", err);
      alert(`Could not create the watchlist.\n\n${err.code || err.message || err}`);
    } finally {
      createBtn.disabled = false;
    }
  });

  document.body.appendChild(box);
  positionPopover(box, btn);
  popoverEl = box;
  popoverSym = sym;
  input.focus({ preventScroll: true });

  setTimeout(() => {
    document.addEventListener("mousedown", onDocMouseDown, true);
    document.addEventListener("keydown", onDocKeyDown, true);
    window.addEventListener("scroll", closePopover, true);
  }, 0);
}

function positionPopover(box, btn) {
  const r = btn.getBoundingClientRect();
  let top = window.scrollY + r.bottom + 6;
  let left = window.scrollX + r.left;
  box.style.top = `${top}px`;
  box.style.left = `${left}px`;
  requestAnimationFrame(() => {
    const bw = box.offsetWidth;
    const bh = box.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    if (left + bw + 12 > window.scrollX + vw) left = window.scrollX + vw - bw - 12;
    if (r.bottom + bh + 12 > vh) top = window.scrollY + r.top - bh - 6; // flip above if no room below
    box.style.left = `${Math.max(8, left)}px`;
    box.style.top = `${Math.max(8, top)}px`;
  });
}

async function onStarClick(btn) {
  const tr = btn.closest("tr");
  if (!tr) return;
  const sym = tr.dataset.sym || "";
  if (!sym) return;

  if (popoverEl && popoverSym === sym) { closePopover(); return; }

  await renderPopover(btn, sym, tr.dataset.close, tr.dataset.indgrp || "", tr.dataset.ind || "");
}

// Exposed for the inline onclick="toggleStar(this)" attributes rendered by
// scanner/dashboard.py's _star_cell().
window.toggleStar = onStarClick;

document.addEventListener("DOMContentLoaded", paintStars);
paintStars();

onAuthStateChanged(auth, (user) => {
  if (unsubDefs) { unsubDefs(); unsubDefs = null; }
  if (unsubSymbols) { unsubSymbols(); unsubSymbols = null; }
  currentUid = user ? user.uid : null;
  closePopover();

  if (currentUid) {
    fetchRemoteLists(currentUid).then((defs) => { remoteListDefs = defs; });
    unsubDefs = subscribeRemoteListDefs(currentUid, (defs) => { remoteListDefs = defs; });
    unsubSymbols = subscribeRemoteSymbols(currentUid, (docs) => {
      const map = {};
      docs.forEach((d) => { map[d.id] = d.lists || []; });
      remoteSymbolLists = map;
      paintStars();
    });
  } else {
    paintStars();
  }
});
