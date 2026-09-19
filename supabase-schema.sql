-- supabase-schema.sql
--
-- Run this once in Supabase Dashboard -> SQL Editor -> New query -> Run.
-- Creates every table this app writes to (via docs/js/supabase-compat.js,
-- which mimics the old Firestore client API), with Row Level Security so
-- each signed-in user can only ever see/touch their own rows — the same
-- privacy model as the old firestore.rules (path-scoped by uid).
--
-- Column names are camelCase and double-quoted throughout because
-- supabase-compat.js / update-prices.js reference them as exact strings
-- (e.g. .eq("userId", ...)) — Postgres folds unquoted identifiers to
-- lowercase, which would break those lookups if the quotes were dropped.

-- ── users ──────────────────────────────────────────────────────────────
-- One row per signed-in person, keyed by their Supabase Auth id. Only
-- used today as a legacy fallback read in accounts.js (ensureDefaultAccount)
-- — the app works fine even if a user never gets a row here.
create table if not exists public.users (
  id uuid primary key references auth.users (id) on delete cascade,
  "portfolioSize" numeric,
  "riskType" text,
  "riskValue" numeric
);

alter table public.users enable row level security;

create policy "users_owner_all" on public.users
  for all
  using (auth.uid() = id)
  with check (auth.uid() = id);

-- ── accounts ───────────────────────────────────────────────────────────
-- A user's trading accounts (e.g. "Zerodha", "Upstox F&O"), each with its
-- own portfolio size / risk settings.
create table if not exists public.accounts (
  id uuid primary key default gen_random_uuid(),
  "userId" uuid not null references auth.users (id) on delete cascade,
  name text not null,
  "portfolioSize" numeric not null default 0,
  "riskType" text not null default 'percent',
  "riskValue" numeric not null default 0,
  "createdAt" timestamptz not null default now(),
  "updatedAt" timestamptz not null default now()
);

create index if not exists accounts_user_idx on public.accounts ("userId");

alter table public.accounts enable row level security;

create policy "accounts_owner_all" on public.accounts
  for all
  using (auth.uid() = "userId")
  with check (auth.uid() = "userId");

-- ── positions ──────────────────────────────────────────────────────────
-- Open positions, created from the Position Size Calculator.
create table if not exists public.positions (
  id uuid primary key default gen_random_uuid(),
  "userId" uuid not null references auth.users (id) on delete cascade,
  "accountId" uuid references public.accounts (id) on delete set null,
  symbol text not null,
  "dateBought" text,
  entry numeric not null,
  stop numeric not null,
  qty numeric not null,
  "riskPerShare" numeric,
  "riskAmount" numeric,
  "riskPct" numeric,
  "capitalRequired" numeric,
  "capitalPct" numeric,
  "currentPrice" numeric,
  "createdAt" timestamptz not null default now()
);

create index if not exists positions_user_idx on public.positions ("userId");
create index if not exists positions_symbol_idx on public.positions (symbol);

alter table public.positions enable row level security;

create policy "positions_owner_all" on public.positions
  for all
  using (auth.uid() = "userId")
  with check (auth.uid() = "userId");

-- ── bookedPositions ────────────────────────────────────────────────────
-- Closed/booked trades — the permanent trade journal. Table name is
-- camelCase and must stay double-quoted everywhere, including in code
-- that queries it directly.
create table if not exists public."bookedPositions" (
  id uuid primary key default gen_random_uuid(),
  "userId" uuid not null references auth.users (id) on delete cascade,
  "accountId" uuid references public.accounts (id) on delete set null,
  symbol text not null,
  entry numeric,
  stop numeric,
  "exitPrice" numeric not null,
  "riskPct" numeric,
  "riskPerShare" numeric,
  qty numeric,
  "pnlPct" numeric,
  "rMultiple" numeric,
  "impactAbs" numeric,
  "impactPct" numeric,
  "portfolioSizeAtBooking" numeric,
  "dateBought" text,
  "dateSold" text not null,
  "bookedAt" timestamptz not null default now()
);

create index if not exists booked_positions_user_idx on public."bookedPositions" ("userId");

alter table public."bookedPositions" enable row level security;

create policy "booked_positions_owner_all" on public."bookedPositions"
  for all
  using (auth.uid() = "userId")
  with check (auth.uid() = "userId");

-- ── watchlist ──────────────────────────────────────────────────────────
-- One row per (user, symbol). Composite primary key matches the
-- onConflict: "userId,symbol" upsert in supabase-compat.js. `lists` holds
-- which of the user's watchlist(s) this symbol belongs to (see
-- watchlistDefs below) — same role as the `lists` array field on the old
-- Firestore watchlist/{symbol} doc.
create table if not exists public.watchlist (
  "userId" uuid not null references auth.users (id) on delete cascade,
  symbol text not null,
  "currentPrice" numeric,
  "previousClose" numeric,
  change numeric,
  "changePercent" numeric,
  "industryGroup" text default '',
  industry text default '',
  "addedAt" bigint,
  lists text[] not null default '{}',
  primary key ("userId", symbol)
);

alter table public.watchlist enable row level security;

create policy "watchlist_owner_all" on public.watchlist
  for all
  using (auth.uid() = "userId")
  with check (auth.uid() = "userId");

-- ── watchlistDefs ──────────────────────────────────────────────────────
-- One row per watchlist a user has created (name + display order). `id`
-- is TEXT, not UUID, because the app seeds a fixed id of "default" for
-- the first watchlist (see DEFAULT_LIST_ID in watchlist-lists.js) and
-- otherwise generates a random UUID string for new ones — both are valid
-- text values, so keeping this column TEXT avoids a type mismatch.
create table if not exists public."watchlistDefs" (
  id text primary key,
  "userId" uuid not null references auth.users (id) on delete cascade,
  name text not null,
  "order" integer not null default 0,
  "createdAt" timestamptz not null default now()
);

create index if not exists watchlist_defs_user_idx on public."watchlistDefs" ("userId");

alter table public."watchlistDefs" enable row level security;

create policy "watchlist_defs_owner_all" on public."watchlistDefs"
  for all
  using (auth.uid() = "userId")
  with check (auth.uid() = "userId");

-- ── Realtime ───────────────────────────────────────────────────────────
-- supabase-compat.js's onSnapshot() subscribes via Postgres Realtime
-- (postgres_changes). Each table needs to be added to the built-in
-- `supabase_realtime` publication for those subscriptions to fire.
alter publication supabase_realtime add table public.accounts;
alter publication supabase_realtime add table public.positions;
alter publication supabase_realtime add table public."bookedPositions";
alter publication supabase_realtime add table public.watchlist;
alter publication supabase_realtime add table public."watchlistDefs";