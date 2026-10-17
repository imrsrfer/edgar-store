/* Rebuild the Coverage board's fallback snapshot from Notion page results.

   node coverage_builder.js --parser coverage_parser.js --prev prev.json --out snap.json
        [--console f[|mode]] [--excl f[|mode]] [--queue "Title|f[|mode]" ...] [--spots spots.json]

   Audit 2026-10-15 D5 (auditor 2026-10-16), replacing the builder embedded in the
   artifact:
   - ONE input format. Every source is passed through normalize(), whether it is a
     saved notion-fetch result ({text}), an MCP content array ([{type,text}]), a
     JSON-encoded string, or plain page text written from an inline result. The
     same page text gives the same records whichever way it arrived (tested).
   - mode is "file" (tool saved it) or "inline" (the result came back inline and
     was written out verbatim). It is recorded per source; it never changes parsing.
   - Every source is VALIDATED against its own structure before it replaces
     anything. A source that fails keeps the previous rows AND the previous
     timestamp and is reported FAILED -- never silently refreshed.
   - Timestamps are per source (asof.pages[label], asof.console, asof.excl).
     asof.queue is the OLDEST queue page, so retained pages can never make the
     queue look current.
   - freshness[] lists every source: fresh (file) / fresh (inline) /
     STALE (retained) / FAILED (<reason>). Exit code 2 when anything FAILED. */
const fs = require('fs');
const path = require('path');

function normalize(raw) {
  let v = raw;
  for (let i = 0; i < 3 && typeof v === 'string'; i++) {
    const s = v.trim();
    if (!(s.startsWith('{') || s.startsWith('[') || s.startsWith('"'))) break;
    try { v = JSON.parse(s); } catch (e) { break; }
  }
  if (Array.isArray(v)) v = v.map(x => (x && typeof x === 'object' ? x.text || '' : String(x))).join('\n');
  else if (v && typeof v === 'object') v = v.text || (v.result && v.result.text) || '';
  return String(v).replace(/\r\n/g, '\n');
}

function splitSpec(spec) {           // "Title|file|mode" or "file|mode" or "file"
  const parts = spec.split('|');
  const mode = ['file', 'inline'].includes(parts[parts.length - 1]) ? parts.pop() : 'file';
  const file = parts.pop();
  return { title: parts.join('|'), file, mode };
}

function queueProblem(rows, text, prevRows) {
  if (!rows.length) return 'no queue rows parsed';
  const hdr = text.match(/\*\*Rows (R?)(\d+)\s*[–-]\s*R?(\d+) of/);
  if (hdr) {
    const want = Number(hdr[3]) - Number(hdr[2]) + 1;
    if (rows.length !== want) return `parsed ${rows.length} rows but the page header says ${want} (Rows ${hdr[2]}–${hdr[3]})`;
  }
  if (prevRows && prevRows.length && rows.length < prevRows.length / 2)
    return `rows fell from ${prevRows.length} to ${rows.length}`;
  return null;
}

function build(argv, now = new Date().toISOString()) {
  const get = k => { const i = argv.indexOf(k); return i < 0 ? null : argv[i + 1]; };
  const all = k => argv.map((a, i) => (a === k ? argv[i + 1] : null)).filter(Boolean);
  const P = require(path.resolve(get('--parser') || path.join(__dirname, 'coverage_parser.js')))();
  const prev = JSON.parse(fs.readFileSync(get('--prev'), 'utf8'));
  const out = JSON.parse(JSON.stringify(prev));
  out.asof = Object.assign({}, out.asof || {});
  out.asof.pages = Object.assign({}, (prev.asof && prev.asof.pages) || {});
  const freshness = [];
  const read = f => normalize(fs.readFileSync(f, 'utf8'));

  // ---- queue pages
  const pages = (prev.pages || []).slice();
  const rowsBy = {};
  (prev.queue || []).forEach(q => { (rowsBy[q.page] = rowsBy[q.page] || []).push(q); });
  pages.forEach(p => { if (!out.asof.pages[p]) out.asof.pages[p] = prev.asof && prev.asof.queue || null; });
  const refreshed = new Set();
  all('--queue').forEach(spec => {
    const { title, file, mode } = splitSpec(spec);
    const txt = read(file);
    let label = P.pageLabel(title);
    if (!/Rows/.test(label)) { const m = txt.match(/\*\*Rows (R?\d+)\s*[–-]\s*(R?\d+) of/); label = m ? `Rows ${m[1]}–${m[2]}` : label; }
    const rows = P.parseQueue(txt, label);
    const bad = queueProblem(rows, txt, rowsBy[label]);
    if (bad) { freshness.push({ source: `queue: ${label}`, status: 'FAILED', detail: bad }); return; }
    rowsBy[label] = rows; refreshed.add(label);
    if (!pages.includes(label)) pages.push(label);
    out.asof.pages[label] = now;
    freshness.push({ source: `queue: ${label}`, status: `fresh (${mode})`, detail: `${rows.length} rows` });
  });
  pages.filter(p => !refreshed.has(p) && !freshness.some(f => f.source === `queue: ${p}`))
    .forEach(p => freshness.push({ source: `queue: ${p}`, status: 'STALE (retained)', detail: `as of ${out.asof.pages[p]}` }));
  pages.sort((a, b) => (/R\d/.test(b) ? 1 : 0) - (/R\d/.test(a) ? 1 : 0) || ((parseInt(a.replace(/\D+/, '')) || 0) - (parseInt(b.replace(/\D+/, '')) || 0)));
  let queue = [];
  pages.forEach(p => { queue = queue.concat(rowsBy[p] || []); });
  queue = queue.map((q, i) => Object.assign({}, q, { pos: i + 1 }));
  out.pages = pages; out.queue = queue;
  const stamps = pages.map(p => out.asof.pages[p]).filter(Boolean).sort();
  out.asof.queue = stamps.length ? stamps[0] : (prev.asof && prev.asof.queue) || null;

  // ---- console
  let con = null;
  const conSpec = get('--console');
  if (conSpec) {
    const { file, mode } = splitSpec(conSpec);
    const parsed = P.parseConsole(read(file));
    const bad = !parsed.pipeline.length ? 'no pipeline rows parsed' : !parsed.watch.length ? 'no watchlist rows parsed' : null;
    if (bad) freshness.push({ source: 'console', status: 'FAILED', detail: bad });
    else { con = parsed; freshness.push({ source: 'console', status: `fresh (${mode})`, detail: `pipeline ${parsed.pipeline.length} · watch ${parsed.watch.length}` }); }
  } else freshness.push({ source: 'console', status: 'STALE (retained)', detail: `as of ${out.asof.console}` });

  // ---- exclusion index
  let exc = null;
  const excSpec = get('--excl');
  if (excSpec) {
    const { file, mode } = splitSpec(excSpec);
    const parsed = P.parseExclusion(read(file));
    const n = parsed.pipeline.length + parsed.watchlist.length + parsed.discarded.length;
    if (!n) freshness.push({ source: 'exclusion index', status: 'FAILED', detail: 'no rows parsed' });
    else { exc = parsed; freshness.push({ source: 'exclusion index', status: `fresh (${mode})`, detail: `${n} names` }); }
  } else freshness.push({ source: 'exclusion index', status: 'STALE (retained)', detail: `as of ${out.asof.excl}` });

  const m = P.assemble(con, exc, queue);
  if (con) {
    const prevP = {}; (prev.pipeline || []).forEach(p => { prevP[p.t] = p; });
    out.pipeline = m.pipeline.map(p => Object.assign({}, p, prevP[p.t] ? { conid: prevP[p.t].conid, exch: prevP[p.t].exch, spot: prevP[p.t].spot, spotTs: prevP[p.t].spotTs } : {}));
    out.watch = m.watch; out.asof.console = now;
  }
  if (exc) { out.book = m.book; out.asof.excl = now; }
  out.reviewed = con && exc ? m.reviewed : [...new Set([...(prev.reviewed || []), ...m.reviewed])];

  // ---- spots: only the names priced now get a new timestamp
  const spotsF = get('--spots');
  if (spotsF) {
    const sp = JSON.parse(fs.readFileSync(spotsF, 'utf8'));
    let n = 0;
    (out.pipeline || []).forEach(p => { if (sp[p.t]) { Object.assign(p, sp[p.t]); n++; } });
    const total = (out.pipeline || []).length;
    if (n) out.asof.spot = now;
    freshness.push({ source: 'spots', status: n === total ? 'fresh (file)' : n ? 'PARTIAL' : 'FAILED', detail: `${n}/${total} priced` });
  }
  out.freshness = freshness;
  return out;
}

if (require.main === module) {
  const argv = process.argv.slice(2);
  const outF = argv[argv.indexOf('--out') + 1];
  const out = build(argv);
  fs.writeFileSync(outF, JSON.stringify(out));
  const q = out.queue || [];
  console.log(`pipeline ${(out.pipeline || []).length} · watch ${(out.watch || []).length} · queue ${q.length} (${q.filter(r => r.status === 'unreviewed').length} unreviewed)`);
  out.freshness.forEach(f => console.log(`  ${f.status.padEnd(18)} ${f.source} — ${f.detail}`));
  process.exitCode = out.freshness.some(f => f.status === 'FAILED') ? 2 : 0;
}
module.exports = { build, normalize };
