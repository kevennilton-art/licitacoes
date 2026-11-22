// Dispara a coleta de licitações no GitHub sem expor a chave no site.
// Variáveis no Netlify: GH_TOKEN (obrigatória), PIN (obrigatória), GH_REPO e GH_BRANCH (opcionais).
import { timingSafeEqual } from "node:crypto";

const REPO = process.env.GH_REPO || "kevennilton-art/licitacoes";
const BRANCH = process.env.GH_BRANCH || "main";
const WF = "coletar.yml";
const INTERVALO_MIN_MS = 60 * 60 * 1000; // não recoleta se a última terminou há menos de 1 hora

const json = (o, s = 200) =>
  new Response(JSON.stringify(o), {
    status: s,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

function gh(path, opt = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...opt,
    headers: {
      Authorization: `Bearer ${process.env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "licitando-netlify",
      ...(opt.headers || {}),
    },
  });
}

async function ultima() {
  const r = await gh(`/actions/workflows/${WF}/runs?per_page=1`);
  if (!r.ok) throw new Error(`GitHub respondeu ${r.status}`);
  const d = await r.json();
  const x = (d.workflow_runs || [])[0];
  if (!x) return { estado: "nenhuma" };
  const rodando = ["queued", "in_progress", "waiting", "requested", "pending"].includes(x.status);
  return {
    estado: rodando ? "rodando" : "parada",
    inicio: x.run_started_at || x.created_at,
    fim: x.updated_at,
    resultado: x.conclusion,
  };
}

function pinOk(dado) {
  const a = Buffer.from(String(dado || ""));
  const b = Buffer.from(String(process.env.PIN || ""));
  return a.length === b.length && a.length > 0 && timingSafeEqual(a, b);
}

export default async (req) => {
  if (!process.env.GH_TOKEN || !process.env.PIN) {
    return json({ ok: false, msg: "O botão ainda não foi configurado no Netlify." }, 500);
  }
  try {
    if (req.method === "GET") return json({ ok: true, ...(await ultima()) });
    if (req.method !== "POST") return json({ ok: false, msg: "Método não permitido." }, 405);

    let corpo = {};
    try { corpo = await req.json(); } catch (e) {}
    if (!pinOk(corpo.pin)) return json({ ok: false, msg: "PIN incorreto." }, 401);

    const u = await ultima();
    if (u.estado === "rodando") {
      return json({ ok: false, msg: "Já existe uma coleta em andamento. Aguarde terminar.", ...u }, 409);
    }
    if (u.estado === "parada" && u.resultado === "success" && Date.now() - new Date(u.fim).getTime() < INTERVALO_MIN_MS) {
      return json({ ok: false, msg: "Os dados foram atualizados há menos de 1 hora.", ...u }, 429);
    }
    const r = await gh(`/actions/workflows/${WF}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: BRANCH }),
    });
    if (r.status !== 204) return json({ ok: false, msg: `O GitHub recusou o pedido (${r.status}).` }, 502);
    return json({ ok: true, msg: "Coleta iniciada. Leva cerca de 1h40; os dados novos aparecem ao final." }, 202);
  } catch (e) {
    return json({ ok: false, msg: "Erro ao falar com o GitHub: " + (e && e.message) }, 502);
  }
};
