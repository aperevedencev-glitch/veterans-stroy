-- Таблицы клиентского терминала. Все закрыты RLS без политик: доступ только через функцию sro-terminal (service role).
create table if not exists public.sro_members (
  inn text primary key, name text not null, ogrn text, lvl int, liab numeric default 0, email text,
  code_salt text, code_hash text, updated_at timestamptz default now()
);
create table if not exists public.sro_contracts (
  reg text primary key, inn text not null references public.sro_members(inn) on delete cascade,
  law text, st text, cust text, price numeric, date date, until date, obj text,
  source text default 'ЕИС', updated_at timestamptz default now()
);
create index if not exists sro_contracts_inn on public.sro_contracts(inn);
create table if not exists public.sro_responses (
  id bigint generated always as identity primary key, reg text,
  inn text not null references public.sro_members(inn) on delete cascade,
  answer text not null check (answer in ('yes','no','fix','new')),
  fix_price numeric, fix_date date, comment text, new_cust text, new_law text,
  created_at timestamptz default now()
);
create index if not exists sro_responses_inn on public.sro_responses(inn);
create table if not exists public.sro_auth_log (id bigint generated always as identity primary key, inn text, ok boolean, created_at timestamptz default now());
create table if not exists public.sro_settings (key text primary key, value text);
alter table public.sro_members enable row level security;
alter table public.sro_contracts enable row level security;
alter table public.sro_responses enable row level security;
alter table public.sro_auth_log enable row level security;
alter table public.sro_settings enable row level security;

-- Новый код доступа одному члену (код вернётся в ответе, в базе останется только хеш):
-- with g as (select (select string_agg(substr('23456789ABCDEFGHJKMNPQRSTUVWXYZ',(get_byte(extensions.gen_random_bytes(1),0)%31)+1,1),'') from generate_series(1,8)) code,
--                   encode(extensions.gen_random_bytes(8),'hex') salt)
-- update public.sro_members set code_salt=g.salt, code_hash=encode(extensions.digest(g.salt||g.code,'sha256'),'hex') from g where inn='ИНН' returning inn, g.code;
