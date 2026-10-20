// Клиентский терминал FlowLink для членов СРО «Стройкорпорация».
// Член СРО входит по ИНН и коду доступа, видит только свои договоры и отвечает «да / нет / исправить».
// Оператор (робот) забирает ответы по ключу оператора. Таблицы закрыты RLS, доступ только через эту функцию.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type, x-op-key",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const DATA_URL = "https://raw.githubusercontent.com/aperevedencev-glitch/veterans-stroy/main/flowlink-robot-eis-nostroy/data/members.json";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" } });

async function sha256(s: string) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

async function checkMember(inn: string, code: string) {
  inn = String(inn || "").replace(/\D/g, "");
  code = String(code || "").trim().toUpperCase();
  if (!/^\d{10}(\d{2})?$/.test(inn) || code.length < 6) return { err: "Проверьте ИНН и код доступа." };
  const since = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  const { count } = await db.from("sro_auth_log").select("id", { count: "exact", head: true })
    .eq("inn", inn).eq("ok", false).gte("created_at", since);
  if ((count ?? 0) >= 8) return { err: "Слишком много неверных попыток. Попробуйте через час или запросите новый код в СРО." };
  const { data: m } = await db.from("sro_members").select("inn,name,ogrn,lvl,liab,email,code_salt,code_hash").eq("inn", inn).maybeSingle();
  const ok = !!m && !!m.code_hash && (await sha256(m.code_salt + code)) === m.code_hash;
  await db.from("sro_auth_log").insert({ inn, ok });
  if (!ok) return { err: "ИНН или код доступа не подходят." };
  const { code_salt: _s, code_hash: _h, ...pub } = m!;
  return { m: pub };
}

async function checkOp(req: Request) {
  const key = req.headers.get("x-op-key") || "";
  if (key.length < 16) return false;
  const { data } = await db.from("sro_settings").select("value").eq("key", "op_key_hash").maybeSingle();
  return !!data && (await sha256(key)) === data.value;
}

async function memberView(inn: string) {
  const [{ data: contracts }, { data: responses }] = await Promise.all([
    db.from("sro_contracts").select("reg,law,st,cust,price,date,until,obj").eq("inn", inn).order("date", { ascending: false }),
    db.from("sro_responses").select("reg,answer,fix_price,fix_date,comment,new_cust,new_law,created_at").eq("inn", inn).order("created_at", { ascending: true }),
  ]);
  return { contracts: contracts ?? [], responses: responses ?? [] };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "Только POST" }, 405);
  let b: Record<string, unknown> = {};
  try { b = await req.json(); } catch { return json({ error: "Неверный запрос" }, 400); }
  const action = String(b.action || "");

  // ---- член СРО ----
  if (action === "login" || action === "respond") {
    const r = await checkMember(String(b.inn), String(b.code));
    if (r.err) return json({ error: r.err }, 401);
    const m = r.m!;
    if (action === "respond") {
      const items = Array.isArray(b.items) ? b.items.slice(0, 200) : [];
      const { data: own } = await db.from("sro_contracts").select("reg").eq("inn", m.inn);
      const regs = new Set((own ?? []).map((x) => x.reg));
      const rows = [];
      for (const it of items as Record<string, unknown>[]) {
        const answer = String(it.answer || "");
        if (!["yes", "no", "fix", "new"].includes(answer)) continue;
        const reg = String(it.reg || "").slice(0, 40);
        if (answer !== "new" && !regs.has(reg)) continue;
        const price = Number(String(it.fix_price ?? "").replace(/\s/g, "").replace(",", "."));
        const date = /^\d{4}-\d{2}-\d{2}$/.test(String(it.fix_date || "")) ? String(it.fix_date) : null;
        rows.push({
          inn: m.inn, reg: reg || null, answer,
          fix_price: Number.isFinite(price) && price > 0 ? price : null, fix_date: date,
          comment: String(it.comment || "").slice(0, 500) || null,
          new_cust: answer === "new" ? String(it.new_cust || "").slice(0, 200) : null,
          new_law: answer === "new" ? String(it.new_law || "").slice(0, 20) : null,
        });
      }
      if (!rows.length) return json({ error: "Нет ответов для сохранения." }, 400);
      if (typeof b.email === "string" && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(b.email)) {
        await db.from("sro_members").update({ email: b.email.slice(0, 120) }).eq("inn", m.inn);
      }
      const { error } = await db.from("sro_responses").insert(rows);
      if (error) return json({ error: "Не удалось сохранить ответы." }, 500);
    }
    return json({ member: m, ...(await memberView(m.inn)) });
  }

  // ---- оператор / робот ----
  if (action.startsWith("op_")) {
    if (!(await checkOp(req))) return json({ error: "Нужен ключ оператора." }, 401);
    if (action === "op_responses") {
      const since = typeof b.since === "string" ? b.since : "1970-01-01";
      const { data } = await db.from("sro_responses").select("*").gt("created_at", since).order("created_at").limit(5000);
      const { data: em } = await db.from("sro_members").select("inn,email").not("email", "is", null);
      return json({ responses: data ?? [], emails: em ?? [] });
    }
    if (action === "op_import") {
      const r = await fetch(DATA_URL);
      if (!r.ok) return json({ error: "Не удалось скачать members.json: " + r.status }, 502);
      const arr = (await r.json()) as Record<string, any>[];
      const mem = arr.map((m) => ({ inn: m.inn, name: m.name, ogrn: m.ogrn, lvl: m.lvl || null, liab: m.liab || 0, updated_at: new Date().toISOString() }));
      const { error: e1 } = await db.from("sro_members").upsert(mem, { onConflict: "inn", ignoreDuplicates: false });
      if (e1) return json({ error: "members: " + e1.message }, 500);
      const con = arr.flatMap((m) => (m.contracts || []).map((c: any) => ({
        reg: c.reg, inn: m.inn, law: c.law, st: c.st, cust: c.cust, price: c.price,
        date: c.date || null, until: c.until || null, obj: c.obj, updated_at: new Date().toISOString(),
      })));
      for (let i = 0; i < con.length; i += 500) {
        const { error } = await db.from("sro_contracts").upsert(con.slice(i, i + 500), { onConflict: "reg" });
        if (error) return json({ error: "contracts: " + error.message }, 500);
      }
      return json({ members: mem.length, contracts: con.length });
    }
    return json({ error: "Неизвестное действие" }, 400);
  }
  return json({ error: "Неизвестное действие" }, 400);
});
