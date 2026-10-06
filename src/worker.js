// Worker da Corrida para a Presidência (Cloudflare Workers com arquivos estáticos)
// Serve o site (index.html) e expõe GET /api/resultado com os dados da Presidência
// vindos do TSE, já normalizados, e GET /api/historico com um ponto por minuto da apuração.
//
// Variáveis de ambiente opcionais (Configurações > Variáveis e segredos):
//   TSE_URL = "..."   sobrescreve a URL do JSON (útil se o TSE mudar o caminho)
//   TSE_START = "..."  início da divulgação (ISO 8601, com Z ou -03:00). Antes disso o Worker NÃO consulta o TSE.
//                      Padrão: 2026-10-25T20:00:00Z (domingo, 25/10, 17h de Brasília).

const UPSTREAM = {
  // Padrão dos arquivos de 2026: /dados/<uf>/<uf>-c0001-e<eleição com 6 dígitos>-u.json
  oficial: "https://resultados.tse.jus.br/oficial/ele2026/6258/dados/br/br-c0001-e006258-u.json", // 2º turno (eleição 6258)
};

const START_DEFAULT = "2026-10-25T20:00:00Z"; // 2º turno: domingo, 25/10, 17h de Brasília (UTC-3)
const HIST_HORAS = 16; // por quantas horas após o início o histórico minuto a minuto é gravado
const FRESH = 20;   // segundos em que uma resposta boa é servida sem consultar o TSE
const STALE = 3600; // segundos em que a última resposta boa fica de reserva
const MISS = 60;    // segundos de cache para "ainda não publicado"
const FAIL = 10;    // segundos de cache para erro do TSE
const TIMEOUT = 8000;                     // ms: tempo máximo de uma consulta ao TSE
const ESPERA_404 = 15;                    // s sem consultar de novo enquanto o arquivo não é publicado
const ESPERA_BLOQUEIO = [30, 60, 120, 300]; // s após 403/429 seguidos (ou o Retry-After do TSE, se maior)
const ESPERA_ERRO = [5, 10, 20, 40, 60];  // s após erro, timeout ou resposta inválida seguidos

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
      stt: c.st || (c.e === "s" ? "Eleito" : ""), // "Eleito", "Não eleito"...
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

// Sem "access-control-allow-origin": a API só é usada pelo próprio site (outros sites não gastam a cota).
function json(body, ttl, status) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status: status || 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "public, max-age=" + ttl,
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

// ---------------------------------------------------------------------------
// Consulta ao TSE. Em *.workers.dev a Cache API não guarda nada, então quem segura as
// consultas é a memória de cada cópia (isolate) do Worker:
//   - dado bom fica FRESH segundos na memória;
//   - uma única consulta em andamento por vez (os outros pedidos esperam a mesma resposta);
//   - depois de uma falha, espera crescente antes de consultar de novo (o TSE bloqueia
//     quem insiste: insistir durante um bloqueio só prolonga o bloqueio);
//   - cada consulta tem tempo-limite.
// ---------------------------------------------------------------------------
let MEM = { url: "", t: 0, data: null };     // último dado bom (do endereço em uso)
let NEG = { ate: 0, falhas: 0, resp: null }; // após falha: até quando não consultar, falhas seguidas, resposta a servir
let VOO = null;                              // consulta em andamento
export function resetMemory() { MEM = { url: "", t: 0, data: null }; NEG = { ate: 0, falhas: 0, resp: null }; VOO = null; MEM_HIST = null; }

function consultarTSE(env) {
  const up = pickUrl(env);
  if (MEM.url !== up.url) { MEM = { url: up.url, t: 0, data: null }; NEG = { ate: 0, falhas: 0, resp: null }; } // endereço mudou: não mistura eleições
  const agora = Date.now();
  if (MEM.data && agora - MEM.t < FRESH * 1000) return Promise.resolve({ data: MEM.data });
  if (agora < NEG.ate) return Promise.resolve({ erro: NEG.resp });
  if (!VOO) VOO = buscarTSE(up).finally(() => { VOO = null; });
  return VOO;
}

async function buscarTSE(up) {
  const esperaErro = () => ESPERA_ERRO[Math.min(NEG.falhas, ESPERA_ERRO.length - 1)];
  const falha = (resp, espera) => { NEG = { ate: Date.now() + espera * 1000, falhas: NEG.falhas + 1, resp }; return { erro: resp }; };
  let r;
  try {
    r = await fetch(up.url, {
      headers: { accept: "application/json" },
      // cacheTtlByStatus: o cache da Cloudflare também pode segurar a resposta do TSE entre cópias do Worker
      cf: { cacheTtlByStatus: { "200-299": FRESH, "404": MISS, "403": 0, "429": 0, "500-599": 0 } },
      signal: AbortSignal.timeout(TIMEOUT),
    });
  } catch (err) {
    return falha({ ok: false, error: "upstream_error", detail: (err && err.name) || "fetch", src: up.src }, esperaErro());
  }
  if (r.status === 403 || r.status === 429) {
    const ra = parseInt(r.headers.get("retry-after") || "", 10);
    const espera = Math.max(ESPERA_BLOQUEIO[Math.min(NEG.falhas, ESPERA_BLOQUEIO.length - 1)], Number.isFinite(ra) ? Math.min(ra, 900) : 0);
    return falha({ ok: false, error: "blocked", status: r.status, src: up.src }, espera);
  }
  if (r.status === 404) {
    NEG = { ate: Date.now() + ESPERA_404 * 1000, falhas: 0, resp: { ok: false, error: "not_published", status: 404, src: up.src } };
    return { erro: NEG.resp };
  }
  if (!r.ok) return falha({ ok: false, error: "upstream_error", status: r.status, src: up.src }, esperaErro());
  let data;
  try { data = normalize(await r.json(), up.src); } catch (err) { data = { ok: false, error: "resposta_invalida" }; }
  if (!data.ok) return falha({ ok: false, error: "upstream_error", detail: data.error, src: up.src }, esperaErro());
  MEM = { url: up.url, t: Date.now(), data };
  NEG = { ate: 0, falhas: 0, resp: null };
  return { data, novo: true };
}

async function resultado(request, env, ctx) {
  const cache = caches.default; // funciona em domínio próprio; em *.workers.dev não guarda nada (sem problema)
  const up = pickUrl(env);
  const base = new URL(request.url).origin, u = encodeURIComponent(up.url);
  const freshKey = new Request(base + "/api/resultado?u=" + u);
  const staleKey = new Request(base + "/__reserva/resultado?u=" + u);
  const keep = (key, resp, ttl) => {
    const c = new Response(resp.clone().body, resp);
    c.headers.set("cache-control", "public, max-age=" + ttl);
    ctx.waitUntil(cache.put(key, c).catch(() => {}));
  };

  const hit = await cache.match(freshKey);
  if (hit) return hit;

  const res = await consultarTSE(env);
  if (res.data) {
    const resp = json(res.data, FRESH);
    if (res.novo) { keep(freshKey, resp, FRESH); keep(staleKey, json(res.data, STALE), STALE); }
    return resp;
  }
  // TSE recusou, falhou ou ainda não publicou: serve o dado bom MAIS NOVO que houver (memória ou
  // reserva), marcado como "último dado"; a página nunca recebe números inventados.
  let melhor = MEM.url === up.url ? MEM.data : null;
  const reserva = await cache.match(staleKey);
  if (reserva) { try { const j = await reserva.json(); if (!melhor || (j.pst || 0) > (melhor.pst || 0)) melhor = j; } catch (e) { /* reserva ilegível */ } }
  if (melhor) return json(Object.assign({}, melhor, { stale: true }), FAIL);
  const e = res.erro || { ok: false, error: "upstream_error", src: up.src };
  const ttl = e.error === "upstream_error" ? FAIL : MISS;
  const resp = json(e, ttl, e.error === "upstream_error" ? 502 : 200);
  keep(freshKey, resp, ttl);
  return resp;
}

// ---------------------------------------------------------------------------
// Histórico minuto a minuto (Durable Object com SQLite, uma única instância "hist")
// O TSE só publica o arquivo mais recente; aqui guardamos um ponto por minuto, sempre
// que o dado muda, para o gráfico e as estatísticas.
// Os pontos ficam também na memória do objeto: a tabela inteira só é lida quando ele
// acorda (o plano grátis limita as linhas lidas por dia).
// ---------------------------------------------------------------------------
export class Historico {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.sql = ctx.storage.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS pts (minuto INTEGER PRIMARY KEY, gerado TEXT, pst REAL, vvc REAL, votos TEXT)");
    this.pts = null;
  }
  carregar() {
    if (!this.pts) {
      this.pts = this.sql.exec("SELECT minuto, gerado, pst, vvc, votos FROM pts ORDER BY minuto").toArray()
        .map((r) => ({ t: r.minuto * 60000, gerado: r.gerado, pst: r.pst, vvc: r.vvc, votos: JSON.parse(r.votos) }));
    }
    return this.pts;
  }
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/add") {
      const p = await request.json();
      const pts = this.carregar(), ult = pts[pts.length - 1];
      if (ult && ult.gerado === p.gerado && ult.pst === p.pst) return new Response("igual");
      this.sql.exec("INSERT OR REPLACE INTO pts (minuto, gerado, pst, vvc, votos) VALUES (?, ?, ?, ?, ?)",
        p.minuto, p.gerado, p.pst, p.vvc, JSON.stringify(p.votos));
      const novo = { t: p.minuto * 60000, gerado: p.gerado, pst: p.pst, vvc: p.vvc, votos: p.votos };
      const i = pts.findIndex((x) => x.t === novo.t);
      if (i >= 0) pts[i] = novo;
      else { pts.push(novo); if (pts.length > 1 && pts[pts.length - 2].t > novo.t) pts.sort((a, b) => a.t - b.t); }
      return new Response("ok");
    }
    if (url.pathname === "/list") {
      const desde = Number(url.searchParams.get("desde")) || 0; // pontos de ensaio (antes do início) ficam de fora
      return Response.json(this.carregar().filter((p) => p.t >= desde));
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
  const res = await consultarTSE(env); // mesma memória, espera após falha e consulta única dos visitantes
  if (!res.data) return "TSE: " + ((res.erro && [res.erro.error, res.erro.status].filter(Boolean).join(" ")) || "sem resposta");
  const d = res.data;
  if (!(d.pst > 0)) return "sem apuração";
  const votos = {};
  d.cands.forEach((c) => { votos[c.n] = c.vap; });
  const r = await stub.fetch("https://hist/add", { method: "POST", body: JSON.stringify({ minuto: Math.floor(agora / 60000), gerado: d.generated, pst: d.pst, vvc: Number.isFinite(d.vvc) ? d.vvc : null, votos }) });
  if (!r.ok) throw new Error("histórico respondeu " + r.status);
  const txt = await r.text();
  MEM_HIST = null; // próximo pedido de /api/historico já vê o ponto novo
  return txt === "igual" ? "sem mudança" : "gravado";
}

let MEM_HIST = null; // histórico já em texto, na memória do isolate (30 s)
async function historico(env) {
  if (MEM_HIST && Date.now() - MEM_HIST.t < 30000) return json(MEM_HIST.body, 30);
  const stub = histStub(env);
  if (!stub) return json({ ok: false, error: "historico_indisponivel" }, 60);
  try {
    const ini = startMs(env);
    const r = await stub.fetch("https://hist/list?desde=" + ini);
    if (!r.ok) throw new Error("status " + r.status);
    const body = JSON.stringify({ ok: true, start: new Date(ini).toISOString(), pontos: await r.json() });
    MEM_HIST = { t: Date.now(), body };
    return json(body, 30);
  } catch (err) {
    return json({ ok: false, error: "historico_erro" }, 10, 502);
  }
}

export default {
  async scheduled(event, env, ctx) {
    // o resultado aparece nos logs do Worker; um erro faz o evento do agendador constar como falha
    ctx.waitUntil(gravarMinuto(env).then(
      (r) => { if (r !== "fora da janela") console.log("histórico: " + r); },
      (err) => { console.error("histórico: erro: " + (err && err.message)); throw err; }));
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
