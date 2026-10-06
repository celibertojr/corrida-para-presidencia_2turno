// Worker da Corrida para a Presidência (Cloudflare Workers com arquivos estáticos, ou Pages modo avançado)
// Serve o site (index.html) e expõe GET /api/resultado com os dados da Presidência
// vindos do TSE, já normalizados e com cache na borda da Cloudflare.
//
// Variáveis de ambiente opcionais (Pages > Settings > Environment variables):
//   TSE_URL = "..."   sobrescreve a URL do JSON (útil se o TSE mudar o caminho)
//   TSE_START = "..."  início da divulgação (ISO 8601). Antes disso o Worker NÃO consulta o TSE.
//                      Padrão: 2026-10-04T20:00:00Z (domingo, 4/10, 17h de Brasília).

const UPSTREAM = {
  // Padrão dos arquivos de 2026: /dados/<uf>/<uf>-c0001-e<eleição com 6 dígitos>-u.json
  oficial: "https://resultados.tse.jus.br/oficial/ele2026/6258/dados/br/br-c0001-e006258-u.json" // 2º turno (eleição 6258),
};

const START_DEFAULT = "2026-10-25T20:00:00Z"; // 2º turno: domingo, 25/10, 17h de Brasília (UTC-3)
const HIST_HORAS = 16; // por quantas horas após o início o histórico minuto a minuto é gravado
const FRESH = 20;   // segundos em que uma resposta boa é servida sem consultar o TSE
const STALE = 3600; // segundos em que a última resposta boa fica de reserva
const MISS = 60;    // segundos de cache para "ainda não publicado" (evita rajada de 404 no TSE)
const FAIL = 10;    // segundos de cache para erro do TSE

// O TSE usa vírgula decimal ("7,53"); números inteiros vêm como texto ("9075260").
function num(v) {
  if (v === null || v === undefined || v === "") return NaN;
  let s = String(v).trim();
  if (s.indexOf(",") >= 0) s = s.replace(/\./g, "").replace(",", ".");
  return Number(s);
}

// Junta todos os candidatos, qualquer que seja o aninhamento (carg > agr > par > cand).
function collect(node, out) {
  if (Array.isArray(node)) { node.forEach((n) => collect(n, out)); return; }
  if (node && typeof node === "object") {
    if (Array.isArray(node.cand)) node.cand.forEach((c) => out.push(c));
    for (const k in node) if (k !== "cand") collect(node[k], out);
  }
}

// Converte o JSON bruto do TSE (-u.json) no formato enxuto que o site usa.
function normalize(raw, src) {
  const carg = (raw.carg || []).find((c) => String(c.cd) === "1") || (raw.carg || [])[0];
  const list = [];
  collect(carg ? [carg] : [], list);
  const s = raw.s || {}, e = raw.e || {}, v = raw.v || {};
  const pct = (a, b) => { const x = num(a); if (Number.isFinite(x) && x >= 0 && x <= 100) return x; const y = num(b); return Number.isFinite(y) && y >= 0 && y <= 100 ? y : NaN; };
  let cands = list
    .map((c) => ({
      n: parseInt(c.n, 10),
      nm: c.nmu || c.nm || "",
      vap: num(c.vap),                        // votos apurados
      pvap: pct(c.pvap, c.pvapn),             // % que o site do TSE exibe ("41,23")
      dvt: c.dvt || "",                       // não existe mais no arquivo de 2026; mantido por compatibilidade
      stt: c.st || (c.e === "s" ? "Eleito" : ""), // "Eleito", "2º turno", "Não eleito"...
    }))
    .filter((c) => Number.isFinite(c.n) && Number.isFinite(c.vap));
  // Se algum percentual vier vazio, calcula a partir dos votos (base: votos válidos)
  const base = num(v.vvc) > 0 ? num(v.vvc) : cands.reduce((a, c) => a + c.vap, 0);
  cands = cands.map((c) => Number.isFinite(c.pvap) ? c : Object.assign(c, { pvap: base > 0 ? (100 * c.vap) / base : 0 }));
  let pst = pct(s.pst, s.pstn); // % de seções (urnas) totalizadas
  if (!Number.isFinite(pst) && num(s.ts) > 0 && Number.isFinite(num(s.st))) pst = (100 * num(s.st)) / num(s.ts);
  const out = {
    ok: cands.length > 0 && Number.isFinite(pst),
    src: src,
    generated: ([raw.dt, raw.ht].filter(Boolean).join(" ") || [raw.dg, raw.hg].filter(Boolean).join(" ")),
    pst: pst,
    ts: num(s.ts), st: num(s.st),          // seções totais / totalizadas
    te: num(e.te), est: num(e.est),        // eleitorado total / das seções totalizadas
    vvc: num(v.vvc), vv: num(v.vv),        // votos válidos (com e sem sub judice)
    comp: num(e.c), abst: num(e.a),        // comparecimento e abstenção
    tv: num(v.tv), vb: num(v.vb), vn: num(v.vn), // total de votos, brancos e nulos
    cands: cands,
  };
  if (!out.ok) out.error = "formato_inesperado";
  return out;
}

function json(body, ttl, status) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "public, max-age=" + ttl,
      "access-control-allow-origin": "*",
    },
  });
}

function startMs(env) {
  const t = Date.parse(env.TSE_START || START_DEFAULT);
  return Number.isFinite(t) ? t : Date.parse(START_DEFAULT);
}

function pickUrl(env) {
  if (env.TSE_URL) return { url: env.TSE_URL, src: "custom" };
  return { url: UPSTREAM.oficial, src: "oficial" }; // somente dados oficiais; não há modo de simulação
}

// Memória do próprio isolate: segunda barreira de cache. Funciona mesmo onde a Cache API
// não guarda nada (endereços *.workers.dev), e serve de reserva se o TSE falhar.
let MEM = { t: 0, data: null };
export function resetMemory() { MEM = { t: 0, data: null }; }

async function resultado(request, env, ctx) {
  const cache = caches.default;
  const base = new URL(request.url).origin;
  const freshKey = new Request(base + "/api/resultado");
  const staleKey = new Request(base + "/__reserva/resultado");

  const hit = await cache.match(freshKey);
  if (hit) return hit;
  if (MEM.data && Date.now() - MEM.t < FRESH * 1000) return json(MEM.data, FRESH);

  const up = pickUrl(env);
  const keep = (key, resp, ttl) => {
    const c = new Response(resp.clone().body, resp);
    c.headers.set("cache-control", "public, max-age=" + ttl);
    ctx.waitUntil(cache.put(key, c));
  };

  try {
    // cf.cacheTtlByStatus: o cache da Cloudflare também segura a resposta do TSE entre isolates
    const r = await fetch(up.url, {
      headers: { accept: "application/json" },
      cf: { cacheTtlByStatus: { "200-299": FRESH, "404": MISS, "403": 0, "429": 0, "500-599": 0 } },
    });
    if (r.status === 403 || r.status === 429) {
      // TSE recusou (bloqueio ou excesso de requisições): mostra o último dado bom, ou avisa que não respondeu
      if (MEM.data) return json(Object.assign({}, MEM.data, { stale: true }), FAIL);
      const reserva = await cache.match(staleKey);
      if (reserva) { const j = await reserva.json(); j.stale = true; return json(j, FAIL); }
      const resp = json({ ok: false, error: "blocked", status: r.status, src: up.src }, MISS);
      keep(freshKey, resp, MISS);
      return resp;
    }
    if (r.status === 404) {
      const reserva = await cache.match(staleKey);
      if (reserva) { const j = await reserva.json(); j.stale = true; return json(j, FAIL); }
      if (MEM.data) return json(Object.assign({}, MEM.data, { stale: true }), FAIL);
      const resp = json({ ok: false, error: "not_published", status: r.status, src: up.src }, MISS);
      keep(freshKey, resp, MISS);
      return resp;
    }
    if (!r.ok) throw new Error("upstream " + r.status);
    const data = normalize(await r.json(), up.src);
    if (!data.ok) throw new Error(data.error);
    MEM = { t: Date.now(), data: data };
    const resp = json(data, FRESH);
    keep(freshKey, resp, FRESH);
    keep(staleKey, json(data, STALE), STALE);
    return resp;
  } catch (err) {
    const reserva = await cache.match(staleKey);
    if (reserva) {
      const j = await reserva.json(); j.stale = true;
      return json(j, FAIL);
    }
    if (MEM.data) return json(Object.assign({}, MEM.data, { stale: true }), FAIL);
    const resp = json({ ok: false, error: "upstream_error", src: up.src }, FAIL, 502);
    keep(freshKey, resp, FAIL);
    return resp;
  }
}

// ---------------------------------------------------------------------------
// Histórico minuto a minuto (Durable Object com SQLite, uma única instância "hist")
// O TSE só publica o arquivo mais recente; aqui guardamos um ponto por minuto, sempre
// que o dado muda, para o gráfico e as estatísticas.
// ---------------------------------------------------------------------------
export class Historico {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.sql = ctx.storage.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS pts (minuto INTEGER PRIMARY KEY, gerado TEXT, pst REAL, vvc REAL, votos TEXT)");
  }
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/add") {
      const p = await request.json();
      const ult = this.sql.exec("SELECT gerado, pst FROM pts ORDER BY minuto DESC LIMIT 1").toArray()[0];
      if (ult && ult.gerado === p.gerado && ult.pst === p.pst) return new Response("igual");
      this.sql.exec("INSERT OR REPLACE INTO pts (minuto, gerado, pst, vvc, votos) VALUES (?, ?, ?, ?, ?)",
        p.minuto, p.gerado, p.pst, p.vvc, JSON.stringify(p.votos));
      return new Response("ok");
    }
    if (url.pathname === "/list") {
      const rows = this.sql.exec("SELECT minuto, gerado, pst, vvc, votos FROM pts ORDER BY minuto").toArray()
        .map((r) => ({ t: r.minuto * 60000, gerado: r.gerado, pst: r.pst, vvc: r.vvc, votos: JSON.parse(r.votos) }));
      return Response.json(rows);
    }
    return new Response("não encontrado", { status: 404 });
  }
}

function histStub(env) {
  return env.HIST ? env.HIST.get(env.HIST.idFromName("hist")) : null;
}

// Executado pelo agendador da Cloudflare a cada minuto (wrangler.jsonc → triggers.crons)
async function gravarMinuto(env) {
  const stub = histStub(env);
  if (!stub) return "sem HIST";
  const ini = startMs(env), agora = Date.now();
  if (agora < ini || agora > ini + HIST_HORAS * 3600e3) return "fora da janela";
  const up = pickUrl(env);
  const r = await fetch(up.url, { headers: { accept: "application/json" }, cf: { cacheTtlByStatus: { "200-299": FRESH, "404": MISS, "500-599": 0 } } });
  if (!r.ok) return "TSE " + r.status;
  const d = normalize(await r.json(), up.src);
  if (!d.ok || !(d.pst > 0)) return "sem apuração";
  const votos = {};
  d.cands.forEach((c) => { votos[c.n] = c.vap; });
  await stub.fetch("https://hist/add", { method: "POST", body: JSON.stringify({ minuto: Math.floor(agora / 60000), gerado: d.generated, pst: d.pst, vvc: Number.isFinite(d.vvc) ? d.vvc : null, votos }) });
  MEM_HIST = null; // próximo pedido de /api/historico já vê o ponto novo
  return "gravado";
}

let MEM_HIST = null; // cache do histórico na memória do isolate (30 s)
async function historico(env) {
  if (MEM_HIST && Date.now() - MEM_HIST.t < 30000) return json(MEM_HIST.data, 30);
  const stub = histStub(env);
  if (!stub) return json({ ok: false, error: "historico_indisponivel" }, 60);
  try {
    const pts = await (await stub.fetch("https://hist/list")).json();
    const data = { ok: true, start: new Date(startMs(env)).toISOString(), pontos: pts };
    MEM_HIST = { t: Date.now(), data };
    return json(data, 30);
  } catch (err) {
    return json({ ok: false, error: "historico_erro" }, 10, 502);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(gravarMinuto(env).catch(() => "erro"));
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/api/resultado" || url.pathname === "/api/verificar") {
      if (request.method !== "GET" && request.method !== "HEAD") return new Response("Método não permitido", { status: 405 });
      // Antes do início da divulgação não há o que buscar: responde sem consultar o TSE.
      // /api/verificar ignora o horário (uso manual, para conferir que a leitura do TSE funciona).
      const start = startMs(env), falta = start - Date.now();
      if (url.pathname === "/api/resultado" && falta > 0) {
        return json({ ok: false, error: "not_started", start: new Date(start).toISOString() }, Math.max(1, Math.min(300, Math.floor(falta / 1000))));
      }
      return resultado(request, env, ctx);
    }
    if (url.pathname === "/api/historico") {
      return historico(env);
    }
    if (url.pathname === "/api/status") {
      const up = pickUrl(env);
      return json({ upstream: up.url, src: up.src, fresh: FRESH, start: new Date(startMs(env)).toISOString(), started: Date.now() >= startMs(env) }, 0);
    }
    return env.ASSETS.fetch(request); // demais rotas: arquivos do site
  },
};
