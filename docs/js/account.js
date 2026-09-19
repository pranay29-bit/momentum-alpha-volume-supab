
// docs/js/accounts.js
//
// Shared multi-account support for the Position Size Calculator and
// Position Tracker pages. A user can manage several trading accounts
// (e.g. "Zerodha", "Upstox F&O", "Kid's Account") — each with its own
// portfolio size / risk settings. Positions and booked trades are tagged
// with an `accountId` so each account's data stays separate.
//
// Firestore layout added by this module:
//   users/{uid}/accounts/{accountId}  -> { name, portfolioSize, riskType, riskValue, createdAt, updatedAt }
//
// Existing collections are untouched in shape — `positions` and
// `bookedPositions` docs simply gain an extra `accountId` field.

import { db } from "./firebase.js";
import {
  collection,
  doc,
  addDoc,
  getDoc,
  getDocs,
  setDoc,
  query,
  orderBy,
  onSnapshot,
  serverTimestamp
} from "https://www.gstatic.com/firebasejs/11.9.0/firebase-firestore.js";

const DEFAULT_ACCOUNT_NAME = "Account 1";

function accountsRef(uid) {
  return collection(db, "users", uid, "accounts");
}

function activeAccountKey(uid) {
  return `alphaMomentum:activeAccount:${uid}`;
}

export function getActiveAccountId(uid) {
  return localStorage.getItem(activeAccountKey(uid));
}

export function setActiveAccountId(uid, accountId) {
  localStorage.setItem(activeAccountKey(uid), accountId);
}

// Called once per login. If the user has no accounts yet, create one —
// carrying over any legacy single-account settings that were previously
// stored directly on users/{uid} (portfolioSize/riskType/riskValue), so
// nobody loses their existing setup when this feature rolls out.
export async function ensureDefaultAccount(uid) {
  const existing = await getDocs(accountsRef(uid));
  if (!existing.empty) return;

  let legacy = {};
  try {
    const userSnap = await getDoc(doc(db, "users", uid));
    if (userSnap.exists()) {
      const data = userSnap.data();
      legacy = {
        portfolioSize: Number(data.portfolioSize) || 0,
        riskType: data.riskType || "percent",
        riskValue: Number(data.riskValue) || 0
      };
    }
  } catch (err) {
    console.error(err);
  }

  await addDoc(accountsRef(uid), {
    name: DEFAULT_ACCOUNT_NAME,
    portfolioSize: legacy.portfolioSize || 0,
    riskType: legacy.riskType || "percent",
    riskValue: legacy.riskValue || 0,
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp()
  });
}

export async function createAccount(uid, name) {
  const ref = await addDoc(accountsRef(uid), {
    name,
    portfolioSize: 0,
    riskType: "percent",
    riskValue: 0,
    createdAt: serverTimestamp(),
    updatedAt: serverTimestamp()
  });
  return ref.id;
}

export async function saveAccountSettings(uid, accountId, { portfolioSize, riskType, riskValue }) {
  await setDoc(doc(db, "users", uid, "accounts", accountId), {
    portfolioSize,
    riskType,
    riskValue,
    updatedAt: serverTimestamp()
  }, { merge: true });
}

export async function getAccountSettings(uid, accountId) {
  const snap = await getDoc(doc(db, "users", uid, "accounts", accountId));
  return snap.exists() ? snap.data() : null;
}

// Live-subscribes to the account list, oldest first. Fires immediately
// with the current snapshot, then on every subsequent change.
export function subscribeAccounts(uid, callback) {
  const q = query(accountsRef(uid), orderBy("createdAt", "asc"));
  return onSnapshot(q, (snap) => {
    const accounts = snap.docs.map((d) => ({ id: d.id, ...d.data() }));
    callback(accounts);
  });
}

// Builds a "<select> + Add Account" control inside `container` and wires
// it up. Returns nothing — callbacks tell the caller what happened.
//   onChange(accountId)  — user picked a different account
//   onAdd(accountId)     — a new account was just created (and selected)
export function renderAccountSwitcher(container, uid, accounts, activeId, onChange, onAdd) {
  container.innerHTML = "";

  const wrap = document.createElement("div");
  wrap.className = "field account-switcher-field";

  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = "Account";
  wrap.appendChild(label);

  const row = document.createElement("div");
  row.style.display = "flex";
  row.style.gap = ".5rem";
  row.style.alignItems = "center";

  const select = document.createElement("select");
  select.id = "accountSelect";
  accounts.forEach((acc) => {
    const opt = document.createElement("option");
    opt.value = acc.id;
    opt.textContent = acc.name || "Untitled Account";
    if (acc.id === activeId) opt.selected = true;
    select.appendChild(opt);
  });
  select.addEventListener("change", () => {
    setActiveAccountId(uid, select.value);
    onChange(select.value);
  });
  row.appendChild(select);

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn-link violet";
  addBtn.textContent = "+ Add Account";
  addBtn.addEventListener("click", async () => {
    const name = window.prompt("Name this account (e.g. Zerodha, Upstox F&O):");
    if (!name || !name.trim()) return;
    addBtn.disabled = true;
    try {
      const newId = await createAccount(uid, name.trim());
      setActiveAccountId(uid, newId);
      onAdd(newId);
    } catch (err) {
      console.error(err);
      alert("Could not create account. Please try again.");
    } finally {
      addBtn.disabled = false;
    }
  });
  row.appendChild(addBtn);

  wrap.appendChild(row);
  container.appendChild(wrap);
}

// Picks which account should be active: whatever's saved in localStorage
// if it still exists in the list, otherwise the first (oldest) account.
export function resolveActiveAccountId(uid, accounts) {
  const saved = getActiveAccountId(uid);
  if (saved && accounts.some((a) => a.id === saved)) return saved;
  const fallback = accounts[0]?.id || null;
  if (fallback) setActiveAccountId(uid, fallback);
  return fallback;
}
