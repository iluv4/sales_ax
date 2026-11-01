const $=s=>document.querySelector(s);let rows=[],current=null;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl=u=>/^https?:\/\//i.test(u||'')?u:'';
function toast(msg){const t=$('#toast');t.textContent=msg;t.style.display='block';clearTimeout(toast.t);toast.t=setTimeout(()=>t.style.display='none',4500)}
async function api(path,opts={}){const r=await fetch(path,opts);const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.detail||('요청 실패 '+r.status));return data}
const json=body=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
async function busy(btn,work){btn.disabled=true;btn.classList.add('busy');try{return await work()}catch(e){toast(e.message)}finally{btn.disabled=false;btn.classList.remove('busy')}}
const tag=s=>`<span class="tag ${s==='Gmail 초안'?'':s==='메일 작성됨'?'warn':'gray'}">${esc(s)}</span>`;

function render(state){
  rows=state.rows;$('#q-status').textContent=(state.ai_configured?'AI 연결됨':'AI 키 없음')+' · '+(state.google_connected?'Gmail 연결됨':'Gmail 미연결');
  $('#q-brochure-now').textContent=state.brochure?`현재 소개서: ${state.brochure.filename} (${state.brochure.chars.toLocaleString()}자 읽음)`:'아직 없음';
  $('#q-count-label').textContent=`${rows.length}명 · 이메일 있음 ${rows.filter(r=>r.email).length}명 · 초안 ${rows.filter(r=>r.gmail_draft_id).length}건`;
  $('#q-rows').innerHTML=rows.length?rows.slice().reverse().map(r=>{const src=safeUrl(r.source_url),web=safeUrl(r.website);return `<tr>
    <td>${tag(r.status)}</td>
    <td><strong>${esc(r.name)}</strong><small>${esc(r.category)}${r.followers?` · 팔로워 ${Number(r.followers).toLocaleString()}`:''}</small></td>
    <td>${r.instagram?`<a href="https://instagram.com/${encodeURIComponent(r.instagram)}" target="_blank" rel="noopener">@${esc(r.instagram)}</a>`:''}
      <small>${r.email?esc(r.email):'이메일 없음'}</small>${web?`<small><a href="${esc(web)}" target="_blank" rel="noopener">웹사이트</a></small>`:''}</td>
    <td class="evidence-cell">${esc(r.evidence)}${src?`<small><a href="${esc(src)}" target="_blank" rel="noopener">출처</a></small>`:''}</td>
    <td><div class="row-actions"><button data-act="open" data-id="${r.id}" class="${r.body?'':'primary'}">${r.body?'메일 보기':'메일 쓰기'}</button><button data-act="del" data-id="${r.id}" class="danger" aria-label="${esc(r.name)} 삭제">삭제</button></div></td></tr>`}).join('')
    :`<tr><td colspan="5"><div class="empty"><span class="empty-symbol">⌕</span>1번에서 찾을 사람을 입력하면 여기에 쌓입니다.</div></td></tr>`;
}
const refresh=async()=>render(await api('/api/quick/state'));

$('#q-search').onsubmit=e=>{e.preventDefault();const max=$('#q-max').value;busy($('#q-search-btn'),async()=>{
  const r=await api('/api/quick/search',json({query:$('#q-query').value.trim(),count:+$('#q-count').value||8,max_followers:max===''?null:+max}));
  toast(`${r.added.length}명 추가${r.skipped?` · 중복·조건 밖 ${r.skipped}명 제외`:''}`);await refresh()})};
$('#q-brochure').onsubmit=e=>{e.preventDefault();const f=$('#q-file').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);
  busy($('#q-upload-btn'),async()=>{await api('/api/quick/brochure',{method:'POST',body:fd});toast('소개서를 저장했습니다.');await refresh()})};

function openEditor(r){current=r;$('#q-editor-title').textContent=r.name+'님께 보낼 메일';$('#q-to').value=r.email||'';$('#q-subject').value=r.subject||'';$('#q-body').value=r.body||'';$('#q-editor').showModal()}
async function write(r,btn){const updated=await busy(btn,()=>api(`/api/quick/rows/${r.id}/draft`,{method:'POST'}));if(updated){await refresh();openEditor(updated)}}
const save=()=>api(`/api/quick/rows/${current.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:$('#q-to').value,subject:$('#q-subject').value,body:$('#q-body').value})});
$('#q-rows').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const r=rows.find(x=>x.id===b.dataset.id);if(!r)return;
  if(b.dataset.act==='del'){busy(b,async()=>{await api(`/api/quick/rows/${r.id}`,{method:'DELETE'});await refresh()});return}
  r.body?openEditor(r):write(r,b)};
$('#q-close').onclick=()=>$('#q-editor').close();
$('#q-save').onclick=()=>busy($('#q-save'),async()=>{current=await save();toast('저장했습니다.');await refresh()});
$('#q-redo').onclick=()=>busy($('#q-redo'),async()=>{current=await api(`/api/quick/rows/${current.id}/draft`,{method:'POST'});openEditor(current);await refresh()});
$('#q-gmail').onclick=()=>busy($('#q-gmail'),async()=>{current=await save();const r=await api(`/api/quick/rows/${current.id}/gmail`,{method:'POST'});
  toast('Gmail 초안을 만들었습니다. Gmail 임시보관함에서 확인·예약 발송하세요.');$('#q-editor').close();await refresh();window.open(r.gmail_url,'_blank','noopener')});
refresh().catch(e=>toast(e.message));
