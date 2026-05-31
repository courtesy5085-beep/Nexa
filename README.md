# Nexa — Your AI Career & News Companion

**Tagline:** *Real-time, personalized — jobs, scholarships, internships & news that match you.*

Nexa is a production-ready Streamlit SaaS that combines:
- 🔐 Gmail OAuth + 6-digit email OTP verification
- 🧠 Streaming AI chat (Claude / OpenAI / Lovable AI Gateway compatible)
- 🌐 Real-time web search (Tavily) for jobs, scholarships, internships, news
- 📚 RAG over Supabase `pgvector`
- 👤 Forced onboarding (profile, interests, location, companies)
- 🛡️ Supabase Postgres with RLS — users only see their own data
- 🛠️ Hidden `/admin` dashboard for the owner (broadcast, analytics, CRUD)
- 💎 Premium UI (Perplexity + Superhuman vibe, glassmorphism, Inter font, Lucide icons)

---

## 📁 Project Structure

```
nexa/
├── app.py                  # Main Streamlit entry (router, layout, chat UI)
├── auth.py                 # Gmail OAuth + SMTP OTP + session
├── database.py             # Supabase client, profiles, chats, RLS helpers
├── chatbot.py              # Streaming LLM + RAG + Tavily web search + commands
├── admin.py                # Admin dashboard (owner-only)
├── style.css               # Premium glassmorphism + gradient theme
├── requirements.txt
├── .env.example
├── .streamlit/
│   ├── config.toml         # Theme + server config
│   └── secrets.toml.example
└── assets/
    └── logo.svg
```

---

## 🚀 Deploy to Streamlit Cloud (1-click)

[![Deploy](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://share.streamlit.io/deploy)

1. Push this folder to a **public GitHub repo**.
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Point it at `app.py`.
4. In **Advanced settings → Secrets**, paste the contents of `.env.example` filled in (TOML format).
5. Add the OAuth redirect URI in Google Cloud Console:
   `https://<your-app>.streamlit.app/`

---

## 🧰 Setup (Local)

```bash
git clone <your-repo>
cd nexa
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in real keys
streamlit run app.py
```

---

## 🔑 Required Services & Keys

| Service | Purpose | Where to get it |
|---|---|---|
| **Supabase** | Postgres + pgvector + RLS | https://supabase.com |
| **Google Cloud** | Gmail OAuth client | https://console.cloud.google.com/apis/credentials |
| **Gmail App Password** | SMTP for OTP emails | https://myaccount.google.com/apppasswords |
| **OpenAI** *or* **Anthropic** | LLM | platform.openai.com / console.anthropic.com |
| **Tavily** | Real-time web search | https://tavily.com |

---

## 🗄️ Supabase Schema

Run this SQL **once** in the Supabase SQL editor:

```sql
create extension if not exists vector;
create extension if not exists pgcrypto;

-- 1. Profiles
create table public.profiles (
  id uuid primary key default gen_random_uuid(),
  email text unique not null,
  full_name text,
  phone text,
  education_level text,
  field_of_study text,
  interests text[] default '{}',
  preferred_companies text[] default '{}',
  location text,
  email_verified boolean default false,
  onboarded boolean default false,
  created_at timestamptz default now()
);

-- 2. OTP codes
create table public.otp_codes (
  email text primary key,
  code_hash text not null,
  expires_at timestamptz not null,
  attempts int default 0
);

-- 3. Chat sessions + messages
create table public.chat_sessions (
  id uuid primary key default gen_random_uuid(),
  user_email text references public.profiles(email) on delete cascade,
  title text default 'New chat',
  created_at timestamptz default now()
);

create table public.chat_messages (
  id uuid primary key default gen_random_uuid(),
  session_id uuid references public.chat_sessions(id) on delete cascade,
  role text check (role in ('user','assistant','system')),
  content text,
  rating int default 0, -- -1, 0, 1
  created_at timestamptz default now()
);

-- 4. Vector store (RAG corpus)
create table public.knowledge_chunks (
  id uuid primary key default gen_random_uuid(),
  kind text,                  -- 'job' | 'scholarship' | 'news' | 'internship'
  title text,
  url text,
  content text,
  embedding vector(1536),
  metadata jsonb default '{}',
  created_at timestamptz default now()
);
create index on public.knowledge_chunks
  using hnsw (embedding vector_cosine_ops);

-- 5. Admin-managed opportunities
create table public.opportunities (
  id uuid primary key default gen_random_uuid(),
  kind text,
  title text not null,
  org text,
  url text,
  location text,
  deadline date,
  description text,
  created_at timestamptz default now()
);

-- 6. Broadcasts (admin push)
create table public.broadcasts (
  id uuid primary key default gen_random_uuid(),
  title text,
  body text,
  filter jsonb default '{}',
  sent_at timestamptz default now()
);

-- Grants
grant select, insert, update, delete on public.profiles to authenticated;
grant select, insert, update, delete on public.chat_sessions to authenticated;
grant select, insert, update, delete on public.chat_messages to authenticated;
grant select on public.opportunities to authenticated, anon;
grant all on all tables in schema public to service_role;

-- RLS
alter table public.profiles enable row level security;
alter table public.chat_sessions enable row level security;
alter table public.chat_messages enable row level security;

create policy "own profile" on public.profiles
  for all using (email = current_setting('request.jwt.claim.email', true));

create policy "own chats" on public.chat_sessions
  for all using (user_email = current_setting('request.jwt.claim.email', true));

create policy "own messages" on public.chat_messages
  for all using (
    session_id in (select id from public.chat_sessions
      where user_email = current_setting('request.jwt.claim.email', true))
  );

-- Vector search RPC
create or replace function match_chunks(
  query_embedding vector(1536),
  match_count int default 5,
  filter_kind text default null
) returns table (id uuid, title text, url text, content text, similarity float)
language sql stable as $$
  select id, title, url, content,
         1 - (embedding <=> query_embedding) as similarity
  from public.knowledge_chunks
  where filter_kind is null or kind = filter_kind
  order by embedding <=> query_embedding
  limit match_count;
$$;
```

---

## 🧑‍💻 Slash Commands

| Command | Action |
|---|---|
| `/jobs` | Fetch fresh jobs matching your interests |
| `/internships` | Fresh internships in your field/city |
| `/scholarships` | Latest scholarships (HEC, global) |
| `/news` | Today's news for your topics |
| `/update-profile` | Reopen onboarding form |

---

## 🛡️ Security & Compliance

- All secrets in `.env` / Streamlit Secrets — none hardcoded.
- Supabase RLS isolates user data at the DB level.
- OTP codes are **bcrypt-hashed**, expire in 10 min, max 5 attempts.
- Per-user rate limit (default 30 messages / 10 min).
- Input sanitization on every prompt (length cap + control-char strip).
- **GDPR**: "Delete my account" wipes profile + chats + sessions.
- Terms & Privacy pages built-in.

---

## 📜 License

MIT — build, ship, profit.
