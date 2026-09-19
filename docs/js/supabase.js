// docs/js/supabase.js
//
// Drop-in replacement for firebase.js. Exports the same names
// (db, auth, login, logout, onAuthStateChanged) with the same shapes, so
// every other file only needs to change its import line — no other code
// changes required.
//
// SETUP: replace the two placeholders below with your project's own
// values (Supabase Dashboard → Project Settings → API).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = "https://YOUR-PROJECT-REF.supabase.co";
const SUPABASE_ANON_KEY = "YOUR-ANON-PUBLIC-KEY";

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// `db` is just an opaque handle passed around by the compat shim — mirrors
// how firebase.js exported `db` from getFirestore(app).
export const db = supabase;

// Mimics Firebase's synchronous `auth.currentUser`. Supabase's own auth
// API is async-only (getSession()), so we cache the last known user here
// and keep it updated via onAuthStateChange below.
export const auth = { currentUser: null };

function toFirebaseLikeUser(u) {
  if (!u) return null;
  return {
    uid: u.id,
    email: u.email,
    displayName: u.user_metadata?.full_name || u.user_metadata?.name || u.email
  };
}

// Keep `auth.currentUser` in sync from the moment this module loads,
// before any page explicitly calls onAuthStateChanged.
supabase.auth.getSession().then(({ data }) => {
  auth.currentUser = toFirebaseLikeUser(data.session?.user);
});

// Mirrors Firebase's signInWithPopup(auth, new GoogleAuthProvider()).
// IMPORTANT DIFFERENCE: Supabase's OAuth flow redirects the whole page to
// Google and back, rather than opening a popup. Functionally equivalent,
// but the page will reload after login instead of staying on the same view.
export async function login() {
  const { error } = await supabase.auth.signInWithOAuth({
    provider: "google",
    options: { redirectTo: window.location.href }
  });
  if (error) throw error;
}

export async function logout() {
  const { error } = await supabase.auth.signOut();
  if (error) throw error;
}

// Mirrors Firebase's onAuthStateChanged(auth, callback) signature exactly,
// including firing once immediately with the current state.
export function onAuthStateChanged(_auth, callback) {
  supabase.auth.getSession().then(({ data }) => {
    auth.currentUser = toFirebaseLikeUser(data.session?.user);
    callback(auth.currentUser);
  });

  const { data: sub } = supabase.auth.onAuthStateChange((_event, session) => {
    auth.currentUser = toFirebaseLikeUser(session?.user);
    callback(auth.currentUser);
  });

  return () => sub.subscription.unsubscribe();
}