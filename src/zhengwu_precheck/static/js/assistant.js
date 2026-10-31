const $=id=>document.getElementById(id);
let boot,caseId,rule,selection,initError;
export const materialContext={ids:[],precheckId:null};
const say=text=>{$('assistant-status').textContent=text;};
async function request(path,body,method='POST') {
  const options={method,credentials:'same-origin',headers:{}};
  if(boot?.csrf_token)options.headers['X-CSRF-Token']=boot.csrf_token;
  if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch(path,options);const data=await response.json();
  if(!response.ok)throw new Error(data.error?.message||'服务暂时不可用，请核对记录或转人工。');
  return data;
}
async function init(){boot=await request('/bootstrap',undefined,'GET');const saved=localStorage.getItem('zw-assistant-case');if(saved){try{const previous=await request('/agent/cases/'+encodeURIComponent(saved),undefined,'GET');caseId=previous.case_id;if(previous.matter_id){$('assistant-matter').value=previous.matter_id;rule=await request(`/agent/cases/${caseId}/rules?matter_id=${encodeURIComponent(previous.matter_id)}&scenario_id=${encodeURIComponent(previous.scenario_id)}`,undefined,'GET');if(previous.selection?.snapshot_id===rule.snapshot_id)selection=previous.selection;}$('assistant-rule-info').textContent=selection?'已恢复此前确认的教学规则。':'请重新查看并确认教学规则。';return;}catch{localStorage.removeItem('zw-assistant-case');}}caseId=(await request('/agent/cases',{})).case_id;localStorage.setItem('zw-assistant-case',caseId);}
function actionLink(draft){$('assistant-actions').replaceChildren();if(draft){const a=document.createElement('a');a.href='/my/ticket-drafts/'+encodeURIComponent(draft);a.textContent='预览草稿并确认提交';$('assistant-actions').append(a);}}
const guard=fn=>async()=>{try{await ready;if(initError)throw initError;await fn();}catch(e){say(e.message);}};
const ready=init().catch(e=>{initError=e;say(e.message);});
$('assistant-rules').onclick=guard(async()=>{selection=null;materialContext.precheckId=null;$('assistant-confirm').disabled=true;const option=$('assistant-matter').selectedOptions[0];rule=await request(`/agent/cases/${caseId}/rules?matter_id=${encodeURIComponent(option.value)}&scenario_id=${encodeURIComponent(option.dataset.scenario)}`,undefined,'GET');$('assistant-rule-info').textContent=rule.notice+' '+rule.guides.map(g=>g.filename+' / '+g.rule_version).join('；')+(rule.precheck_rules_approved?'；辅助预检规则已审核发布。':'；辅助预检规则尚未审核发布，可先咨询或转人工。');$('assistant-confirm').disabled=rule.conflict||rule.guides.length===0;});
$('assistant-matter').onchange=()=>{rule=null;selection=null;materialContext.precheckId=null;$('assistant-confirm').disabled=true;$('assistant-rule-info').textContent='事项已切换，请重新查看和确认规则。';};
$('assistant-confirm').onclick=guard(async()=>{if(!rule)return;selection=await request(`/agent/cases/${caseId}/selection`,{matter_id:rule.matter_id,scenario_id:rule.scenario_id,snapshot_id:rule.snapshot_id});say('已确认教学事项及规则。');});
$('assistant-ask').onclick=guard(async()=>{const result=await request(`/agent/cases/${caseId}/ask`,{question:$('assistant-question').value});$('assistant-answer').textContent=result.text;actionLink(result.result?.data?.draft_id);});
$('assistant-draft').onclick=guard(async()=>{const option=$('assistant-matter').selectedOptions[0];const payload={title:'教学人工咨询',content:$('assistant-question').value,matter_id:selection?option.value:'unassigned',scenario_id:selection?option.dataset.scenario:'manual-triage',case_id:caseId,shared_material_ids:materialContext.ids};if(materialContext.precheckId)payload.precheck_id=materialContext.precheckId;const draft=await request('/agent/drafts',payload);actionLink(draft.id);say('草稿已保存，请预览正文和共享范围后确认。');});
export {request,guard,$,say,actionLink};
export const context=()=>({caseId,rule,selection});
import('./materials.js').catch(()=>say('材料功能暂不可用，可继续人工咨询。'));
