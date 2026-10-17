/* Pure parsers: Notion page text (enhanced markdown) -> board model. Runs in the page and in node. */
function boardParsers(){
  const cleanTxt=s=>String(s||'')
    .replace(/<\/?[a-z][^>]*>/gi,'')
    .replace(/\[([^\]]*)\]\([^)]*\)/g,'$1')
    .replace(/\*\*|`/g,'')
    .replace(/\\([~<>$\[\]\-_*#|])/g,'$1')
    .replace(/\\/g,'')
    .replace(/[\u{1F300}-\u{1FAFF}☀-➿]️?/gu,'')
    .replace(/\s+/g,' ').trim();
  const baseT=t=>String(t||'').toUpperCase().replace(/\.(L|BR)$/,'').replace(/[^A-Z0-9\-]/g,'');
  const trunc=(s,n)=>s.length>n?s.slice(0,n-1).replace(/\s+\S*$/,'')+'…':s;

  function section(text,startRe,endRe){
    const m=text.search(startRe); if(m<0) return '';
    const rest=text.slice(m+1); const e=endRe?rest.search(endRe):-1;
    return e<0?text.slice(m):text.slice(m,m+1+e);
  }
  function rows(html){ // table rows -> arrays of cell html
    const out=[];const re=/<tr>([\s\S]*?)<\/tr>/g;let m;
    while((m=re.exec(html))){const cells=[];const cr=/<td>([\s\S]*?)<\/td>/g;let c;while((c=cr.exec(m[1])))cells.push(c[1]);out.push(cells)}
    return out;
  }
  const num=s=>parseFloat(String(s).replace(/,/g,''));

  /* ---------- console ---------- */
  function parseConsole(text){
    const res={};
    // pipeline
    const pipe=section(text,/\n# [^\n]*PIPELINE/,/\n# /);
    const pl=[];
    rows(pipe).forEach(c=>{
      if(c.length<4) return;
      const tk=(c[0].match(/\*\*\[?([A-Z][A-Z0-9.\-]*)/)||[])[1]; if(!tk||tk==='Ticker') return;
      const lvlCell=cleanTxt(c[1]);
      if(/BOUGHT|struck|STRUCK/i.test(lvlCell.slice(0,40))) return;
      const m=lvlCell.match(/(\$)?\s?(\d[\d,]*(?:\.\d+)?)(?:\s*[–-]\s*(\d[\d,]*(?:\.\d+)?))?\s*(p)?/);
      if(!m) return;
      const lo=num(m[2]),hi=m[3]?num(m[3]):null,cur=m[4]?'p':'$';
      const lvl=hi||lo;
      const lvlTxt=cur==='p'?(hi?`${m[2]}–${m[3]}p`:`${m[2]}p`):`$${m[2]}`;
      pl.push({t:tk,lvl,lvlTxt,cur,tier:trunc(cleanTxt(c[2]),60),stage:cleanTxt(c[3]),kind:'pipeline'});
    });
    // position-level accumulate-belows (holdings)
    const pos=(text.match(/Position-level accumulate-belows[^\n]*/)||[''])[0];
    pos.split(/\s·\s/).forEach(seg=>{ const pm=seg.match(/\b([A-Z]{2,6})\s\\?\$(\d[\d,]*(?:\.\d+)?)(?![–-])/); if(!pm||/complete|zone/i.test(seg)) return;
      pl.push({t:pm[1],lvl:num(pm[2]),lvlTxt:'$'+pm[2],cur:'$',tier:'Holding',stage:'Accumulate-below on a holding. The §G 25% strike does not apply (ruled 2026-08-18): flagged, not struck.',kind:'holding'}); });
    // watchlist
    const W=section(text,/\n# [^\n]*WATCHLIST/,null);
    const bullets=W.split(/\n- /).slice(1);
    const watch=[];const seen=new Set();
    bullets.forEach(b=>{
      const head=b.match(/^\*\*\[?([A-Z][A-Z0-9.\-]*)\]?(?:\([^)]*\))?\*\*\s*(?:\(((?:[^()]|\([^)]*\))*)\))?/);
      if(!head) return;
      let tks=[head[1]];
      const multi=b.match(/^\*\*([A-Z0-9.]+(?:\s*·\s*[A-Z0-9.]+)+)\*\*/); if(multi) tks=multi[1].split(/\s*·\s*/);
      const and=b.match(/^\*\*([A-Z0-9.]+)\*\* and \*\*([A-Z0-9.]+)\*\*/); if(and) tks=[and[1],and[2]];
      const body=cleanTxt(b.split(/\n/)[0]);
      tks.forEach(t=>{
        const k=baseT(t); if(seen.has(k)) return; seen.add(k);
        const date=(body.match(/2026-\d\d-\d\d/)||[null])[0];
        const tier=/Tier 1/.test(body)&&!/Tier 2/.test(body.slice(0,400))?'T1':(/Tier 2/.test(body)?'T2':'—');
        let summary=body.replace(/^[A-Z0-9.·\s]+(\([^)]*(\([^)]*\))?[^)]*\))?\s*(--|—)?\s*/,'');
        const lvlM=body.match(/accumulate-below\s+\$(\d+(?:\.\d+)?)/i);
        watch.push({t,name:cleanTxt(multi||and?'':(head[2]||'')).slice(0,60),date,tier,summary:trunc(summary,420)});
        if(lvlM&&tks.length===1) pl.push({t,lvl:num(lvlM[1]),lvlTxt:'$'+lvlM[1],cur:'$',tier:'Watchlist exception',stage:trunc(summary,300),kind:'watch-level'});
      });
    });
    res.pipeline=pl; res.watch=watch;
    // checkpoint register
    const reg=section(text,/\n# [^\n]*CHECKPOINT REGISTER/,/\n# /);
    const regmap={};
    rows(reg).forEach(c=>{ if(c.length<4) return; const stage=cleanTxt(c[2]).toLowerCase();
      const names=cleanTxt(c[1]); const due=cleanTxt(c[0]).replace(/\(.*$/,'').trim();
      const what=cleanTxt(c[3]);
      (names.match(/\b[A-Z][A-Z0-9.]{0,6}\b/g)||[]).forEach(tk=>{const k=baseT(tk); if(!regmap[k]) regmap[k]={due:trunc(due,48),what,stage,closed:/^CLOSED/.test(what)}; });
    });
    res.register=regmap;
    // struck + closed prose lists, live-verdict tickers
    const struck=(W.match(/Struck at Gate 0[^:]*:\*?\*?\s*([^\n<]*)/)||['',''])[1];
    res.struck=cleanTxt(struck).split(/\s*·\s*/).map(s=>baseT(s.split(' ')[0])).filter(Boolean);
    const closedL=(text.match(/Closed in 2026[^:]*:\*?\*?\s*([^\n]*)/)||['',''])[1];
    res.closed=cleanTxt(closedL).split(/\s*·\s*/).map(s=>baseT(s.split(' ')[0])).filter(x=>x&&x.length<=6);
    const lv=section(text,/\n# [^\n]*LIVE VERDICTS/,/\n## |\n# /);
    res.holdings=rows(lv).map(c=>c[0]&&(cleanTxt(c[0]).match(/[A-Z][A-Z0-9.]*/g)||[])).flat().filter(Boolean).map(baseT).filter(x=>x!=='NAME');
    res.registerTickers=Object.keys(regmap);
    return res;
  }

  /* ---------- exclusion index ---------- */
  function parseExclusion(text){
    const out={pipeline:[],watchlist:[],discarded:[],cond:{}};
    const re=/\n## (Pipeline|Watchlist|Discarded)[^\n]*\n([\s\S]*?)(?=\n## |$)/g;let m;
    while((m=re.exec(text))){
      const key=m[1].toLowerCase();
      rows(m[2]).forEach(c=>{ if(c.length<3) return; const tk=(c[0].match(/\*\*([^*]+)\*\*/)||[])[1]; if(!tk) return;
        out[key].push(baseT(tk)); out.cond[baseT(tk)]=cleanTxt(c[2]); });
    }
    return out;
  }

  /* ---------- queue pages ---------- */
  function parseQueue(text,pageLabel){
    const out=[];
    rows(text).forEach(c=>{
      if(c.length<6) return;
      const id=cleanTxt(c[0]); if(!/^R?\d+$/.test(id)) return;
      const tk=(c[1].match(/\*\*([A-Z][A-Z0-9.\-]*)\*\*/)||[])[1]; if(!tk) return;
      const nm=c[1].match(/\*\*\s*\(([\s\S]*?)\)\s*·\s*([\s\S]*)$/);
      const name=nm?cleanTxt(nm[1]):tk; const bucketRaw=nm?cleanTxt(nm[2]):'';
      const b=/foreign/i.test(bucketRaw)?'Foreign':/<\s*\$?500M/.test(bucketRaw)?'US <$500M':/>\s*\$?5B/.test(bucketRaw)?'US >$5B':/SMID/.test(bucketRaw)?'US SMID':'Other';
      const status=cleanTxt(c[2]).toLowerCase();
      const tri=cleanTxt(c[3]);
      const mc=(tri.match(/\$?([\d.]+)B\b/)||[])[1];
      let mult=(tri.match(/P\/FCF-SBC ([\d.]+)x/)||tri.match(/= ([\d.]+)x/)||tri.match(/·\s*([\d.]+)x/)||[])[1];
      if(/NOT MEASURED/.test(tri)&&!/= [\d.]+x/.test(tri)) mult=null;
      const vs=(tri.match(/\(([+\-−]?[\d.]+)%\)/)||tri.match(/·\s*([+\-−][\d.]+)%/)||[])[1];
      const disp=cleanTxt(c[5]);
      let cat=null;
      if(status!=='unreviewed'){
        const h=disp.slice(0,160);
        cat=status==='watchlist'?'To watchlist':status==='pipeline'?'To pipeline':/GATE 0/i.test(h)?'Gate 0 failure':/takeout|merger|tender offer|\[EVENT\] pending/i.test(h)?'Deal pending':/valuation/i.test(h)?'Valuation (MOS gate)':'Growth or margin';
      }
      const dd=status!=='unreviewed'?((disp.match(/20\d\d-\d\d-\d\d/)||[null])[0]):null;
      out.push({id,t:tk,name:trunc(name,40),b,mc:mc?num(mc):null,mult:mult?num(mult):null,vs:vs!=null?num(String(vs).replace('−','-')):null,status,cat,dd,page:pageLabel});
    });
    return out;
  }

  function pageLabel(title){const m=String(title).match(/rows?\s+(R?\d+\s*[–-]\s*R?\d+)/i);return m?('Rows '+m[1].replace(/\s/g,'')):cleanTxt(title).slice(0,30);}
  function childPages(text){
    const out=[];const re=/<page url="([^"]+)">([^<]*)<\/page>/g;let m;
    while((m=re.exec(text))) out.push({url:m[1],title:m[2]});
    return out;
  }

  /* tag what a watchlist condition waits on */
  function tagsFor(s){s=(s||'').toLowerCase();const t=[];
    if(/\/25|management|fork d|not scored|def 14a/.test(s)) t.push('Management score');
    if(/\bmos\b|price|multiple|yield|\d+x\b|valuation/.test(s)) t.push('Valuation');
    if(/growth|revenue|margin|fcf|ocf|comp|capex|sales/.test(s)) t.push('Fundamentals');
    if(/merger|deal|terminated|10-k|10-q|20-f|store|diagnos|resolv|reconcil|settled|identified|disclos/.test(s)) t.push('Data to resolve');
    return t;}

  /* assemble: console + exclusion + queue rows -> model pieces */
  function assemble(con,exc,queueRows){
    const m={};
    if(con){
      m.pipeline=con.pipeline;
      m.watch=con.watch.filter(w=>!(con.holdings||[]).includes(baseT(w.t))).map(w=>{
        const k=baseT(w.t);const r=con.register[k];
        let cond=(exc&&exc.cond[k])||'';
        if(!cond&&r) cond=r.what.replace(/^2026-\d\d-\d\d add[^.]*\.\s*/,'');
        if(!cond){const mm=w.summary.match(/Re-look[^:]*:\s*(.{0,300})/);cond=mm?mm[1]:''}
        return Object.assign({},w,{cond:trunc(cond,300),due:r?r.due:'',tags:tagsFor(cond||w.summary)});
      });
    }
    if(exc) m.book={pipeline:exc.pipeline.length,watch:exc.watchlist.length,disc:exc.discarded.length,struck:con?con.struck.length:null};
    if(queueRows) m.queue=queueRows;
    // reviewed set
    const rv=new Set();
    if(exc) [...exc.pipeline,...exc.watchlist,...exc.discarded].forEach(t=>rv.add(t));
    if(con){[...con.struck,...con.closed,...con.holdings,...con.registerTickers].forEach(t=>rv.add(t));
      con.pipeline.forEach(p=>rv.add(baseT(p.t))); con.watch.forEach(w=>rv.add(baseT(w.t)));}
    if(queueRows) queueRows.filter(q=>q.status!=='unreviewed').forEach(q=>rv.add(baseT(q.t)));
    m.reviewed=[...rv];
    return m;
  }
  return {pageLabel,cleanTxt,baseT,parseConsole,parseExclusion,parseQueue,childPages,assemble,tagsFor};
}
if(typeof module!=='undefined') module.exports=boardParsers;
