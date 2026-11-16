const state={busy:false};
const feedback=document.querySelector('#feedback');
async function send(url,body){const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});let data={};try{data=await response.json();}catch(error){}if(!response.ok)throw new Error(data.detail?.message||data.message||'请求失败，请重试');return data;}
const runForm=document.querySelector('#run-form');
if(runForm)runForm.addEventListener('submit',async event=>{event.preventDefault();if(state.busy)return;state.busy=true;const button=event.target.querySelector('button');button.disabled=true;feedback.textContent='核验中…';try{const form=new FormData(event.target);const body={sample:form.get('sample'),params:{threshold:form.get('threshold')}};if(form.get('text').trim())body.text=form.get('text');const data=await send('/api/runs',body);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(error){feedback.textContent=error.message;}finally{state.busy=false;button.disabled=false;}});
const reviewForm=document.querySelector('#review-form');
if(reviewForm)reviewForm.addEventListener('submit',async event=>{event.preventDefault();try{await send('/api/runs/'+event.target.dataset.run+'/review',{revision:Number(event.target.dataset.revision),decision:new FormData(event.target).get('decision')});location.reload();}catch(error){feedback.textContent=error.message;}});

const adjustFeedback=document.querySelector('#adjust-feedback');
const adjustButton=document.querySelector('#adjust-run');
if(adjustButton)adjustButton.addEventListener('click',async()=>{
  if(state.busy)return;state.busy=true;adjustButton.disabled=true;adjustFeedback.textContent='重算中…';
  try{
    const select=document.querySelector('#catalog-select');
    const threshold=document.querySelector('#adjust-threshold');
    const data=await send('/api/runs/'+adjustButton.dataset.run+'/adjustments',{catalog_version:select.value,params:{threshold:threshold.value}});
    location.href='/adjust?run_id='+adjustButton.dataset.run+'&adjustment='+data.id;
  }catch(error){adjustFeedback.textContent=error.message;}finally{state.busy=false;adjustButton.disabled=false;}
});
document.querySelectorAll('[data-activate]').forEach(button=>button.addEventListener('click',async()=>{
  if(state.busy)return;state.busy=true;button.disabled=true;
  const status=document.createElement('span');status.className='inline-status';
  try{
    await send('/api/catalogs/'+button.dataset.activate+'/activate',{expected_version:button.dataset.expected});
    status.textContent='已启用';status.classList.add('pass');location.reload();
  }catch(error){status.textContent='启用失败：'+error.message+'（仍使用原版本）';status.classList.add('bad');button.after(status);button.disabled=false;}
  finally{state.busy=false;}
}));

const adjustReview=document.querySelector('#adjust-review-form');
if(adjustReview)adjustReview.addEventListener('submit',async event=>{
  event.preventDefault();
  try{
    await fetch('/api/adjustments/'+event.target.dataset.adjustment+'/review',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:Number(event.target.dataset.revision),decision:new FormData(event.target).get('decision')})}).then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.detail?.message||data.message||'请求失败，请重试');return data;});
    location.reload();
  }catch(error){document.querySelector('#adjust-feedback').textContent=error.message;}
});
