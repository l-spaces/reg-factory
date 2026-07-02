// reg-factory WebUI 前端逻辑（原生 JS，无构建）
let SCRIPTS = [];
let EMBEDS = [];
let curRun = null;     // 当前运行 run_id
let curSrc = null;     // 当前选中脚本
let evtSrc = null;     // EventSource
let smsTimer = null;   // 接码助手倒计时刷新

// 账号中心状态管理
let accState = {
    allAccounts: [],      // 所有账号数据（筛选后）
    currentPage: 1,       // 当前页码
    pageSize: 10,         // 每页条数
    selectedEmail: null,  // 当前选中的邮箱
    usages: [],           // 当前邮箱的 usages
    cookies: []           // 当前邮箱的 cookies
};

const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];

// ---------------------------------------------------------------- 状态灯轮询
async function pollStatus(){
  try{
    const s = await (await fetch('/api/status')).json();
    $('#dot-bb').classList.toggle('on', s.bitbrowser);
    $('#dot-clash').classList.toggle('on', s.clash);
    $('#node').textContent = '节点 ' + (s.node || '--');
    $('#running').textContent = s.running ? `● ${s.running} 个任务运行中` : '';
  }catch(e){}
}
setInterval(pollStatus, 5000);

// ---------------------------------------------------------------- 视图切换
function showView(v){
  $('#view-run').style.display      = v==='run'      ? 'flex'   : 'none';
  $('#view-env').style.display      = v==='env'      ? 'block'  : 'none';
  $('#view-embed').style.display    = v==='embed'    ? 'block'  : 'none';
  $('#view-mailpool').style.display = v==='mailpool' ? 'block'  : 'none';
  $('#view-accounts').style.display = v==='accounts' ? 'block'  : 'none';
  $$('.navbtn').forEach(b=>b.classList.toggle('active', b.dataset.view===v));
  if(v==='env')      loadEnv();
  if(v==='mailpool') loadMailpool();
  if(v==='accounts') loadAccounts();
}
$$('.navbtn').forEach(b=> b.onclick = ()=> showView(b.dataset.view));

// ---------------------------------------------------------------- 脚本导航
async function loadScripts(){
  SCRIPTS = (await (await fetch('/api/scripts')).json()).scripts;
  const nav = $('#script-nav');
  const cats = {};
  SCRIPTS.forEach(s => (cats[s.category]=cats[s.category]||[]).push(s));
  nav.innerHTML = '';

  // 内嵌功能页 —— 放最上面
  try{
    EMBEDS = (await (await fetch('/api/embeds')).json()).embeds || [];
    if(EMBEDS.length){
      const t=document.createElement('div'); t.className='cat-title'; t.textContent='功能'; nav.appendChild(t);
      EMBEDS.forEach(e=>{
        const b=document.createElement('button');
        b.className='scriptbtn'; b.textContent='🌐 '+e.title; b.dataset.embed=e.id;
        b.onclick=()=>openEmbed(e.id);
        nav.appendChild(b);
      });
    }
  }catch(err){}

  for(const cat of Object.keys(cats)){
    const t = document.createElement('div');
    t.className='cat-title'; t.textContent=cat; nav.appendChild(t);
    cats[cat].forEach(s=>{
      const b=document.createElement('button');
      b.className='scriptbtn'; b.textContent=s.title; b.dataset.id=s.id;
      b.onclick=()=>{ showView('run'); selectScript(s.id); };
      nav.appendChild(b);
    });
  }
  // 外部工具链接(新标签打开)
  try{
    const links = (await (await fetch('/api/links')).json()).links || [];
    if(links.length){
      const t = document.createElement('div');
      t.className='cat-title'; t.textContent='外部工具'; nav.appendChild(t);
      links.forEach(l=>{
        const a=document.createElement('a');
        a.className='scriptbtn linkbtn'; a.href=l.url; a.target='_blank'; a.rel='noopener';
        a.title=l.desc||l.url; a.innerHTML=`🔗 ${l.title}`;
        nav.appendChild(a);
      });
    }
  }catch(e){}
}

function selectScript(id){
  curSrc = SCRIPTS.find(s=>s.id===id);
  $$('.scriptbtn').forEach(b=>b.classList.toggle('active', b.dataset.id===id));
  renderForm(curSrc);
}

// ---------------------------------------------------------------- 渲染表单
function renderForm(s){
  const p = $('#form-panel');
  p.innerHTML = '';
  const h = document.createElement('div');
  h.innerHTML = `<h2 class="form-title">${s.title}</h2><p class="form-desc">${s.desc||''}</p>`;
  p.appendChild(h);

  s.args.forEach(a=>{
    const f = document.createElement('div'); f.className='field';
    const key = a.flag.replace(/^--/,'');
    const label = a.label || key;
    if(a.type==='bool'){
      f.className='field checkbox';
      f.innerHTML = `<input type="checkbox" id="f_${key}" ${a.default?'checked':''}>
        <label for="f_${key}">${label}</label>`;
      if(a.help){ const hh=document.createElement('div'); hh.className='fhelp'; hh.textContent=a.help; f.appendChild(hh); }
    }else if(a.type==='choice'){
      f.innerHTML = `<label>${label}</label>
        <select id="f_${key}">${a.choices.map(c=>`<option ${c==a.default?'selected':''}>${c}</option>`).join('')}</select>
        ${a.help?`<div class="fhelp">${a.help}</div>`:''}`;
    }else if(a.type==='multi'){
      const def = a.default||[];
      f.innerHTML = `<label>${label}</label>
        <div class="multi">${a.choices.map(c=>`<label><input type="checkbox" value="${c}" ${def.includes(c)?'checked':''} data-multi="${key}">${c}</label>`).join('')}</div>
        ${a.help?`<div class="fhelp">${a.help}</div>`:''}`;
    }else{
      const t = a.secret ? 'password' : (a.type==='int' ? 'number' : 'text');
      f.innerHTML = `<label>${label}</label>
        <input type="${t}" id="f_${key}" value="${a.default!==undefined&&a.default!==''?a.default:''}" placeholder="${a.help||''}">
        ${a.help?`<div class="fhelp">${a.help}</div>`:''}`;
    }
    p.appendChild(f);
  });

  const btn = document.createElement('button');
  btn.className='btn-run'; btn.textContent='▶ 运行';
  btn.onclick = runScript;
  p.appendChild(btn);
  const cmd = document.createElement('div'); cmd.className='cmd-line'; cmd.id='cmd-preview';
  p.appendChild(cmd);
}

function collectArgs(s){
  const args = {};
  s.args.forEach(a=>{
    const key = a.flag.replace(/^--/,'');
    if(a.type==='bool'){
      args[a.flag] = $(`#f_${key}`).checked;
    }else if(a.type==='multi'){
      args[a.flag] = $$(`input[data-multi="${key}"]:checked`).map(x=>x.value);
    }else{
      const v = $(`#f_${key}`).value.trim();
      if(v!=='') args[a.flag] = a.type==='int' ? parseInt(v,10) : v;
    }
  });
  return args;
}

// ---------------------------------------------------------------- 运行 + SSE 日志
function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }
function logClass(line){
  if(/error|fail|失败|错误/i.test(line)) return 'log-err';
  if(/warn|警告/i.test(line)) return 'log-warn';
  if(/\bok\b|success|成功|✓|完成/i.test(line)) return 'log-ok';
  if(/^\d{2}:\d{2}:\d{2}/.test(line)) return 'log-dim';
  return '';
}
function appendLog(log, line){
  const cls = logClass(line);
  const safe = escHtml(line);
  log.innerHTML += (cls ? `<span class="${cls}">${safe}</span>` : safe) + '\n';
  log.scrollTop = log.scrollHeight;
}
async function runScript(){
  if(curRun && evtSrc){ evtSrc.close(); }
  const args = collectArgs(curSrc);
  const log = $('#log'); log.innerHTML='';
  $('#log-title').textContent = `运行日志 — ${curSrc.title}`;
  const r = await (await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({script:curSrc.id, args})})).json();
  if(r.error){ log.textContent='错误: '+r.error; return; }
  curRun = r.run_id;
  $('#cmd-preview').textContent = '$ '+r.cmd;
  $('#btn-stop').disabled = false;
  evtSrc = new EventSource(`/api/logs/${curRun}`);
  evtSrc.onmessage = e=>{ appendLog(log, e.data); };
  evtSrc.addEventListener('done', ()=>{ evtSrc.close(); $('#btn-stop').disabled = true; pollStatus(); });
  evtSrc.onerror = ()=>{ evtSrc.close(); $('#btn-stop').disabled = true; };
}

$('#btn-stop').onclick = async ()=>{
  if(!curRun) return;
  await fetch(`/api/stop/${curRun}`,{method:'POST'});
  $('#btn-stop').disabled = true;
};

// ---------------------------------------------------------------- 配置页
async function loadEnv(){
  const data = await (await fetch('/api/env')).json();
  const wrap = $('#env-groups'); wrap.innerHTML='';
  data.groups.forEach(g=>{
    const box = document.createElement('div'); box.className='env-group';
    const tests = (g.tests||[]).map(t=>
      `<button class="btn-test" data-test="${t.target}">${t.label}</button>`).join('');
    box.innerHTML = `<div class="env-group-title">
        <span>${g.group}</span>
        <span class="test-area">${tests}<span class="test-result" data-result-for="${g.group}"></span></span>
      </div>`;
    g.items.forEach(it=>{
      const row = document.createElement('div'); row.className='env-item';
      const type = it.secret ? 'password':'text';
      row.innerHTML = `
        <div class="k">${it.key}${it.required?'<span class="req">*</span>':''}</div>
        <div class="v">
          <input type="${type}" data-env="${it.key}" value="${(it.value||'').replace(/"/g,'&quot;')}"
                 placeholder="${it.default? '默认 '+it.default : ''}">
          ${it.help?`<div class="ehelp">${it.help}</div>`:''}
        </div>`;
      box.appendChild(row);
    });
    // 绑定该组的测试按钮
    box.querySelectorAll('.btn-test').forEach(btn=>{
      btn.onclick = ()=> runTest(btn.dataset.test, btn);
    });
    wrap.appendChild(box);
  });
}

// 连通测试：把当前页面所有 .env 输入(含未保存的)一起发过去，用最新值测
async function runTest(target, btn){
  const env = {};
  $$('input[data-env]').forEach(i=>{ if(i.value!=='') env[i.dataset.env]=i.value; });
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '测试中…';
  const res = btn.closest('.env-group').querySelector('.test-result');
  res.textContent=''; res.className='test-result';
  try{
    const r = await (await fetch(`/api/test/${target}`,{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({env})})).json();
    res.textContent = (r.ok?'✓ ':'✗ ') + r.msg;
    res.classList.add(r.ok?'ok':'bad');
  }catch(e){
    res.textContent = '✗ 测试请求失败: '+e; res.classList.add('bad');
  }finally{
    btn.disabled=false; btn.textContent=old;
  }
}

$('#btn-save-env').onclick = async ()=>{
  const env = {};
  $$('input[data-env]').forEach(i=>{ env[i.dataset.env] = i.value; });
  const r = await (await fetch('/api/env',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({env})})).json();
  const msg = $('#env-msg');
  msg.textContent = r.ok ? `✓ 已保存 ${r.saved} 项` : ('保存失败: '+(r.error||''));
  setTimeout(()=>msg.textContent='', 3000);
};

// ---------------------------------------------------------------- 内嵌页 + 接码助手
function openEmbed(id){
  const e = EMBEDS.find(x=>x.id===id);
  if(!e) return;
  showView('embed');
  $$('.scriptbtn').forEach(b=>b.classList.toggle('active', b.dataset.embed===id));
  $('#embed-title').textContent = e.title;
  $('#embed-open').href = e.url;
  $('#embed-frame').src = e.url;
  const helper = $('#sms-helper');
  if(e.sms_helper){
    helper.style.display='block';
    if(e.sms_service_default) $('#sms-service').placeholder = e.sms_service_default;
    refreshRents();
  }else{
    helper.style.display='none';
  }
}

async function copyText(txt, btn){
  try{ await navigator.clipboard.writeText(txt); if(btn){const o=btn.textContent;btn.textContent='已复制';setTimeout(()=>btn.textContent=o,1200);} }
  catch(e){ alert('复制失败,请手动选择: '+txt); }
}

function fmtRemain(sec){
  sec=Math.max(0,sec); const m=Math.floor(sec/60), s=sec%60;
  return `${m}:${String(s).padStart(2,'0')}`;
}

async function refreshRents(){
  let data;
  try{ data = await (await fetch('/api/sms/rents')).json(); }catch(e){ return; }
  const wrap = $('#sms-rents'); wrap.innerHTML='';
  (data.rents||[]).forEach(r=>{
    const card = document.createElement('div'); card.className='rent-card';
    const codesHtml = r.codes.length
      ? r.codes.map(c=>`<span class="code-chip">${c}<button class="mini" data-copy="${c}">复制</button></span>`).join('')
      : '<span class="dim">暂无验证码</span>';
    card.innerHTML = `
      <div class="rent-phone">
        <b>+${r.phone}</b>
        <button class="mini" data-copy="${r.phone}">复制号码</button>
        <span class="multi-badge ${r.can_multi?'ok':'no'}">${r.can_multi?'多次接码':'单次'}</span>
        <span class="remain" data-remain="${r.remain}">剩 ${fmtRemain(r.remain)}</span>
      </div>
      <div class="rent-actions">
        <button class="btn-sm getcode" data-pkey="${r.pkey}">获取验证码</button>
        <button class="btn-sm release" data-pkey="${r.pkey}">完成/释放</button>
      </div>
      <div class="codes">${codesHtml}</div>
      <div class="sms-msg" data-msg="${r.pkey}"></div>`;
    wrap.appendChild(card);
  });
  // 绑定
  wrap.querySelectorAll('[data-copy]').forEach(b=> b.onclick=()=>copyText(b.dataset.copy,b));
  wrap.querySelectorAll('.getcode').forEach(b=> b.onclick=()=>getCode(b.dataset.pkey,b));
  wrap.querySelectorAll('.release').forEach(b=> b.onclick=()=>releaseNum(b.dataset.pkey));
  // 倒计时滴答
  if(smsTimer) clearInterval(smsTimer);
  if((data.rents||[]).length){
    smsTimer = setInterval(()=>{
      $$('.remain').forEach(el=>{
        let r=parseInt(el.dataset.remain,10)-1; el.dataset.remain=r;
        el.textContent = r>0 ? '剩 '+fmtRemain(r) : '已过期';
      });
    },1000);
  }
}

$('#btn-rent').onclick = async ()=>{
  const btn=$('#btn-rent'); const o=btn.textContent; btn.disabled=true; btn.textContent='租号中…';
  const body={ service: $('#sms-service').value.trim()||undefined, country: $('#sms-country').value.trim()||undefined,
    prefer_multi: $('#sms-prefer-multi') ? $('#sms-prefer-multi').checked : true };
  try{
    const r = await (await fetch('/api/sms/rent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(!r.ok){ alert('获取号码失败: '+r.msg); }
    await refreshRents();
  }finally{ btn.disabled=false; btn.textContent=o; }
};

async function getCode(pkey, btn){
  const o=btn.textContent; btn.disabled=true; btn.textContent='等待验证码…';
  const msg = document.querySelector(`[data-msg="${pkey}"]`);
  if(msg) msg.textContent='';
  try{
    const r = await (await fetch('/api/sms/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pkey})})).json();
    if(r.ok){ await refreshRents(); }
    else if(msg){ msg.textContent = (r.expired?'⏰ ':'') + r.msg; }
  }finally{ btn.disabled=false; btn.textContent=o; }
}

async function releaseNum(pkey){
  await fetch('/api/sms/release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pkey})});
  await refreshRents();
}

// ---------------------------------------------------------------- 邮箱池
async function loadMailpool(){
  try{
    const d = await (await fetch('/api/mailpool')).json();
    $('#mailpool-total').textContent = `当前池中 ${d.total} 个邮箱`;
  }catch(e){}
}

$('#btn-import-mail').onclick = async ()=>{
  const text = $('#mailpool-input').value;
  if(!text.trim()){ $('#mailpool-msg').textContent='请先粘贴邮箱'; return; }
  const btn=$('#btn-import-mail'); const o=btn.textContent; btn.disabled=true; btn.textContent='导入中…';
  const msg=$('#mailpool-msg'); msg.textContent='';
  try{
    const r = await (await fetch('/api/mailpool',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text})})).json();
    if(r.ok){
      let m = `✓ 导入 ${r.added}，跳过重复 ${r.skipped}`;
      if(r.bad) m += `，格式错误 ${r.bad}`;
      m += `，池中共 ${r.total}`;
      msg.textContent = m;
      if(r.bad && r.bad_samples.length) msg.textContent += `（错误样例：${r.bad_samples[0]}…）`;
      $('#mailpool-total').textContent = `当前池中 ${r.total} 个邮箱`;
      if(r.added) $('#mailpool-input').value='';
    }else{ msg.textContent='导入失败: '+(r.msg||''); }
  }catch(e){ msg.textContent='导入请求失败: '+e; }
  finally{ btn.disabled=false; btn.textContent=o; }
};

// ---------------------------------------------------------------- 账号中心
const esc = s => String(s??'').replace(/[&<>"]/g, c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadAccounts(){ await Promise.all([loadAccStats(), loadAccList()]); }

async function loadAccStats(){
  try{
    const s = await (await fetch('/api/accounts/stats')).json();
    const box = $('#acc-stats');
    box.innerHTML = `<div class="acc-card total">总邮箱 <b>${s.total_emails}</b></div>`;
    const sel = $('#acc-platform');
    const cur = sel.value;
    const plats = Object.keys(s.platforms||{}).sort();
    sel.innerHTML = '<option value="">全部平台</option>' +
      plats.map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('');
    sel.value = cur;
    plats.forEach(p=>{
      const c = s.platforms[p];
      box.insertAdjacentHTML('beforeend',
        `<div class="acc-card">
          <div class="acc-card-h">${esc(p)}</div>
          <span class="b-ok">ok ${c.ok||0}</span>
          <span class="b-rsv">rsv ${c.reserved||0}</span>
          <span class="b-err">err ${c.error||0}</span>
          <span class="b-free">free ${c.free||0}</span>
        </div>`);
    });
  }catch(e){}
}

async function loadAccList(){
  try{
    const qs = new URLSearchParams();
    const p=$('#acc-platform').value, st=$('#acc-status').value, q=$('#acc-q').value.trim();
    if(p)  qs.set('platform', p);
    if(st) qs.set('status',   st);
    if(q)  qs.set('q',        q);
    const d = await (await fetch('/api/accounts?'+qs.toString())).json();
    $('#acc-count').textContent = `共 ${d.total} 个`;
    accState.allAccounts = d.accounts || [];
    accState.currentPage = 1;
    accState.selectedEmail = null;
    renderEmailsTable(1);
    renderPagination();
    renderUsagesTable();
    renderCookiesTable();
  }catch(e){ $('#acc-table').innerHTML='<p class="hint">加载失败</p>'; }
}

function renderEmailsTable(page){
  const start = (page - 1) * accState.pageSize;
  const end = start + accState.pageSize;
  const pageData = accState.allAccounts.slice(start, end);

  if(!pageData.length){
    $('#acc-table').innerHTML = '<p class="hint">无匹配账号</p>';
    return;
  }

  const rows = pageData.map(a=>{
    const rt = a.refresh_token ? (a.refresh_token.length > 16 ? a.refresh_token.substring(0,16)+'…' : a.refresh_token) : '-';
    const cid = a.client_id ? (a.client_id.length > 16 ? a.client_id.substring(0,16)+'…' : a.client_id) : '-';
    return `
      <tr data-email="${esc(a.email)}" class="${accState.selectedEmail===a.email?'selected':''}">
        <td class="acc-em">${esc(a.email)}</td>
        <td class="acc-pw">
          <span class="pw-mask">••••••</span>
          <span class="pw-real" hidden>${esc(a.password)}</span>
          <button class="mini pw-toggle" type="button">显示</button>
        </td>
        <td class="truncate">${esc(rt)}</td>
        <td class="truncate">${esc(cid)}</td>
        <td>${esc(a.source)}</td>
        <td class="dim">${esc((a.created_at||'').slice(0,10))}</td>
        <td class="acc-actions">
          <button class="btn-edit mini" data-email="${esc(a.email)}">编辑</button>
          <button class="btn-delete mini" data-email="${esc(a.email)}">删除</button>
        </td>
      </tr>`;
  }).join('');

  $('#acc-table').innerHTML = `
    <table class="tbl">
      <thead><tr><th>邮箱</th><th>密码</th><th>Refresh Token</th><th>Client ID</th><th>来源</th><th>入库</th><th>操作</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderPagination(){
  const totalPages = Math.ceil(accState.allAccounts.length / accState.pageSize);
  if(totalPages <= 1){
    $('#acc-pagination').innerHTML = '';
    return;
  }

  const prev = accState.currentPage > 1;
  const next = accState.currentPage < totalPages;

  $('#acc-pagination').innerHTML = `
    <button id="btn-prev-page" ${prev?'':'disabled'}>← 上一页</button>
    <span class="page-info">${accState.currentPage} / ${totalPages}</span>
    <button id="btn-next-page" ${next?'':'disabled'}>下一页 →</button>
  `;

  if(prev) $('#btn-prev-page').onclick = ()=> changePage(accState.currentPage - 1);
  if(next) $('#btn-next-page').onclick = ()=> changePage(accState.currentPage + 1);
}

function changePage(page){
  accState.currentPage = page;
  renderEmailsTable(page);
  renderPagination();
}

function renderUsagesTable(){
  const container = $('#acc-usages');
  if(!accState.selectedEmail){
    container.innerHTML = '<p class="hint">请点击上方邮箱查看使用记录</p>';
    return;
  }
  if(!accState.usages.length){
    container.innerHTML = '<p class="hint">该邮箱暂无平台使用记录</p>';
    return;
  }

  const rows = accState.usages.map(u=>`
    <tr>
      <td>${esc(u.platform)}</td>
      <td class="st-${esc(u.status)}">${esc(u.status)}</td>
      <td class="dim">${esc(u.reason||'')}</td>
      <td class="dim">${esc((u.updated_at||'').slice(0,19).replace('T',' '))}</td>
    </tr>`).join('');

  container.innerHTML = `
    <table class="tbl">
      <thead><tr><th>平台</th><th>状态</th><th>原因</th><th>更新时间</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderCookiesTable(){
  const container = $('#acc-cookies');
  if(!accState.selectedEmail){
    container.innerHTML = '<p class="hint">请点击上方邮箱查看 Cookies</p>';
    return;
  }
  if(!accState.cookies.length){
    container.innerHTML = '<p class="hint">该邮箱暂无 Cookies</p>';
    return;
  }

  const rows = accState.cookies.map(c=>{
    const preview = c.payload ? (c.payload.length > 50 ? c.payload.substring(0,50)+'…' : c.payload) : '';
    return `
      <tr>
        <td>${esc(c.platform)}</td>
        <td class="truncate">${esc(preview)}</td>
        <td class="dim">${esc((c.updated_at||'').slice(0,19).replace('T',' '))}</td>
      </tr>`;
  }).join('');

  container.innerHTML = `
    <table class="tbl">
      <thead><tr><th>平台</th><th>Cookie 内容</th><th>更新时间</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// 事件委托：打码切换 + 行点击详情 + 编辑/删除按钮
$('#acc-table').addEventListener('click', e=>{
  // 密码打码切换
  const tg = e.target.closest('.pw-toggle');
  if(tg){
    e.stopPropagation();
    const td = tg.closest('.acc-pw');
    const m = td.querySelector('.pw-mask'), r = td.querySelector('.pw-real');
    const show = r.hidden;
    r.hidden = !show; m.hidden = show;
    tg.textContent = show ? '隐藏' : '显示';
    return;
  }

  // 删除按钮
  const delBtn = e.target.closest('.btn-delete');
  if(delBtn){
    e.stopPropagation();
    const email = delBtn.dataset.email;
    if(!confirm(`确定要删除账号 ${email} 吗？此操作不可恢复。`)) return;
    deleteAccount(email);
    return;
  }

  // 编辑按钮
  const editBtn = e.target.closest('.btn-edit');
  if(editBtn){
    e.stopPropagation();
    const email = editBtn.dataset.email;
    openEditModal(email);
    return;
  }

  // 行点击详情
  const tr = e.target.closest('tr[data-email]');
  if(tr) openAccDetail(tr.dataset.email);
});

async function openAccDetail(email){
  if(accState.selectedEmail === email) return; // 点击已选中的行，不重复请求

  try{
    const d = await (await fetch('/api/accounts/'+encodeURIComponent(email))).json();
    accState.selectedEmail = email;
    accState.usages = d.usages || [];
    accState.cookies = d.cookies || [];

    renderEmailsTable(accState.currentPage); // 重新渲染以更新高亮
    renderUsagesTable();
    renderCookiesTable();
  }catch(e){
    console.error('加载账号详情失败:', e);
  }
}

// 筛选 / 刷新 / 抽屉事件绑定
['acc-platform','acc-status'].forEach(id=>
  $('#'+id).addEventListener('change', loadAccList));
let accQTimer = null;
$('#acc-q').addEventListener('input', ()=>{
  clearTimeout(accQTimer);
  accQTimer = setTimeout(loadAccList, 250);
});
$('#btn-acc-refresh').onclick = loadAccounts;

// ---------------------------------------------------------------- 删除账号
async function deleteAccount(email){
  try{
    const r = await fetch('/api/accounts/'+encodeURIComponent(email), {method:'DELETE'});
    const data = await r.json();
    if(!r.ok || !data.ok){
      alert('删除失败: '+(data.error||''));
      return;
    }
    // 成功提示
    const msg = $('#acc-count');
    const old = msg.textContent;
    msg.textContent = '✓ 已删除';
    setTimeout(()=>msg.textContent=old, 2000);
    // 刷新列表
    await loadAccList();
  }catch(e){
    alert('删除请求失败: '+e);
  }
}

// ---------------------------------------------------------------- 编辑账号
function openEditModal(email){
  // 查找当前账号数据
  const acc = accState.allAccounts.find(a => a.email === email);
  if(!acc) return;

  // 预填数据
  $('#edit-email').value = acc.email;
  $('#edit-password').value = acc.password;
  $('#edit-msg').textContent = '';

  // 显示模态框
  const modal = $('#edit-modal');
  modal.style.display = 'flex';
  modal.dataset.oldEmail = email; // 保存原邮箱用于 API 调用
}

// 关闭模态框
$('#btn-edit-cancel').onclick = ()=> $('#edit-modal').style.display = 'none';
$('#edit-modal').addEventListener('click', e=>{
  if(e.target.id === 'edit-modal') $('#edit-modal').style.display = 'none';
});

// 提交编辑
$('#btn-edit-confirm').onclick = async ()=>{
  const modal = $('#edit-modal');
  const oldEmail = modal.dataset.oldEmail;
  const newEmail = $('#edit-email').value.trim();
  const password = $('#edit-password').value.trim();

  if(!newEmail || !password){
    $('#edit-msg').textContent = '邮箱和密码不能为空';
    return;
  }

  const body = {};
  if(newEmail !== oldEmail) body.new_email = newEmail;
  if(password) body.password = password;

  try{
    const btn = $('#btn-edit-confirm');
    const oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '保存中…';

    const r = await fetch('/api/accounts/'+encodeURIComponent(oldEmail), {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await r.json();

    if(!r.ok || !data.ok){
      $('#edit-msg').textContent = data.error || '更新失败';
      btn.disabled = false;
      btn.textContent = oldText;
      return;
    }

    // 成功
    modal.style.display = 'none';
    const msg = $('#acc-count');
    const oldMsg = msg.textContent;
    msg.textContent = '✓ 已更新';
    setTimeout(()=>msg.textContent=oldMsg, 2000);
    await loadAccList();

    btn.disabled = false;
    btn.textContent = oldText;
  }catch(e){
    $('#edit-msg').textContent = '请求失败: '+e;
    $('#btn-edit-confirm').disabled = false;
    $('#btn-edit-confirm').textContent = '确定';
  }
};

// ---------------------------------------------------------------- 启动
loadScripts();
pollStatus();
