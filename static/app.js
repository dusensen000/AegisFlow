const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const jsonView = value => '<pre>' + esc(JSON.stringify(value, null, 2)) + '</pre>';
const labels = {unprocessed:'待处理',queued:'排队中',running:'处理中',completed:'审核通过',manual_review:'待人工复核',manual_resolved:'人工已复核',needs_evidence:'待补充证据',failed:'处理失败',paused:'已暂停',cancelled:'已取消'};
const stages = {supervisor:'任务派发',planner:'计划生成',tool_agent:'工具查询',memory_agent:'证据归纳',reviewer:'方案审核',final:'任务结束'};
let tickets = [], selected = null, epoch = 0, staff = null, stream = null, reviewTarget = null;
const records = new Map();

function icons() { window.lucide?.createIcons(); }
function notice(message) { $('#notice').hidden = !message; $('#notice').textContent = message || ''; }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers:{'Content-Type':'application/json',...options.headers}});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败 (' + response.status + ')');
  return data;
}
function badge(status) { return '<span class="status ' + esc(status) + '">' + esc(labels[status] || status) + '</span>'; }
function displayStatus(record) { return record?.manual?.status === 'withdrawn' ? 'cancelled' : record?.status === 'manual_review' && record.manual?.status === 'resolved' ? (record.manual.decision?.outcome === 'needs_evidence' ? 'needs_evidence' : 'manual_resolved') : record?.status || 'unprocessed'; }
function disconnect() { stream?.source.close(); stream = null; }

async function loadWorkspace() {
  ['#chat-tab','#ticket-tab','#manual-tab','#chat-send-btn'].forEach(selector => $(selector).disabled = true);
  disconnect(); if (typeof closeChat === 'function') closeChat(); records.clear();
  await refreshTicketData();
  $('#staff-role').textContent = staff.level >= 3 ? '高级客服' : '客服';
  await loadManual();
  if (tickets.length) await selectTicket(tickets[0].ticket_id);
  await loadConversations(); showTab('chat');
  ['#chat-tab','#ticket-tab','#manual-tab'].forEach(selector => $(selector).disabled = false);
  renderChat();
}

async function refreshTicketData() {
  const [ticketData, sessionData] = await Promise.all([api('/v1/tickets'), api('/v1/sessions')]);
  tickets = ticketData.tickets;
  for (const session of sessionData.sessions) {
    if (session.ticket_id && !records.has(session.ticket_id)) records.set(session.ticket_id, {thread_id:session.id});
  }
  $('#ticket-count').textContent = tickets.length;
  await Promise.all([...records.values()].filter(record => record.thread_id).map(async record => {
    const session = await api('/v1/sessions/' + record.thread_id);
    record.state = session.state; record.supplements = session.supplements;
    record.status = session.runs[0]?.status || 'unprocessed';
    record.run_id = session.runs[0]?.id;
    if (record.run_id) {
      const detail = await api('/v1/runs/' + record.run_id);
      record.manual = detail.manual_review;
    }
  }));
  renderTickets();
}

function renderTickets() {
  const query = $('#search-input').value.toLowerCase(), filter = $('#status-filter').value;
  $('#ticket-list').innerHTML = tickets.filter(ticket => {
    const status = displayStatus(records.get(ticket.ticket_id));
    return (!filter || status === filter) && JSON.stringify(ticket).toLowerCase().includes(query);
  }).map(ticket => {
    const status = displayStatus(records.get(ticket.ticket_id));
    return '<button class="ticket-item ' + (selected === ticket.ticket_id ? 'active' : '') + '" data-ticket="' + esc(ticket.ticket_id) + '"><div class="item-top"><strong>' + esc(ticket.ticket_id) + '</strong>' + badge(status) + '</div><p>' + esc(ticket.description) + '</p><small>' + esc(ticket.requested_action) + ' / ' + esc(ticket.order_id) + '</small></button>';
  }).join('') || '<p class="empty">暂无匹配工单</p>';
}

async function selectTicket(ticketId) {
  const token = ++epoch;
  disconnect(); selected = ticketId; notice('');
  $('#sidebar').classList.remove('open');
  showTab('ticket');
  const ticket = tickets.find(t => t.ticket_id === ticketId);
  if (!ticket) { notice('工单不存在或已删除'); return; }
  $('#ticket-title').textContent = ticket.ticket_id;
  $('#ticket-description').textContent = ticket.description;
  $('#ticket-facts').innerHTML = [['订单号',ticket.order_id],['客户 ID',ticket.user_id],['客户陈述',ticket.user_claim],['期望动作',ticket.requested_action],['工单时间',ticket.opened_at],['已核验事实',ticket.verified_facts.join('、') || '暂无']].map(([key,value]) => '<dt>' + esc(key) + '</dt><dd>' + esc(value) + '</dd>').join('');
  const record = records.get(ticketId) || {};
  records.set(ticketId, record);
  $('#timeline').innerHTML = '';
  record.seen = new Set(); record.state = record.state || {};
  renderState(record);
  if (record.thread_id) {
    try {
      const session = await api('/v1/sessions/' + record.thread_id);
      if (token !== epoch) return;
      record.state = session.state; record.supplements = session.supplements;
      const run = session.runs[0];
      record.run_id = run?.id; record.status = run?.status || 'unprocessed';
      renderState(record); renderTickets();
      if (run) {
        const details = await api('/v1/runs/' + run.id);
        if (token !== epoch) return;
        record.feedback = details.feedback; record.manual = details.manual_review; renderState(record); renderTickets();
        subscribe(record, ticketId);
      }
    } catch (error) { if (token === epoch) notice(error.message); }
  }
}

function renderState(record) {
  const state = record.state || {}, proposal = state.proposal, review = state.review;
  const status = record.status || 'unprocessed', active = ['running','queued','paused'].includes(status);
  $('#edit-ticket-btn').disabled = active || !tickets.find(t => t.ticket_id === selected)?.editable;
  $('#delete-ticket-btn').disabled = !selected;
  $('#run-stage').textContent = active && status !== 'paused' ? (stages[state.next_agent] || '') : '';
  $('#run-status').className = 'status ' + displayStatus(record);
  $('#run-status').textContent = labels[displayStatus(record)] || status;
  $('#process-btn').disabled = active;
  $('#process-btn').innerHTML = '<i data-lucide="play"></i>' + (record.run_id ? '重新处理' : '开始处理');
  $('#cancel-btn').hidden = !active;
  $('#resume-btn').hidden = status !== 'paused';
  $('#supplement-input').disabled = active;
  $('#supplement-btn').disabled = active;
  $('#copy-btn').disabled = !proposal;
  $('#export-btn').disabled = !record.run_id;
  $('#supplement-history').innerHTML = (record.supplements || []).map(s => '<p class="supplement">' + esc(s) + '</p>').join('');
  $('#proposal').innerHTML = proposal ? ((review?.passed ? '' : '<p class="draft">待审核建议</p>') + '<p>' + esc(proposal.summary) + '</p>' + proposal.items.map(item => '<article class="proposal-item"><div class="section-heading"><h4>' + esc(item.action) + '</h4>' + (item.amount == null ? '' : '<span class="amount">¥' + esc(Number(item.amount).toFixed(2)) + '</span>') + '</div><p>' + esc(item.reason) + '</p><div class="references">' + item.rule_refs.map(ref => '<a href="#rule-' + esc(ref) + '">' + esc(ref) + '</a>').join('') + '</div><details><summary>对应证据</summary>' + jsonView({tools:item.evidence_tools,call_ids:item.evidence_ids}) + '</details></article>').join('')) : '<p class="empty">暂无处理建议</p>';
  $('#review').innerHTML = review ? '<div class="review-banner ' + (review.passed ? 'pass' : '') + '"><strong>' + (review.needs_manual_review ? '进入人工复核' : review.needs_replan ? '审核未通过，重新规划' : review.passed ? '审核通过' : '审核未通过') + '</strong><ul>' + (review.issues || []).map(issue => '<li>' + esc(issue) + '</li>').join('') + '</ul><span>重规划 ' + (state.replan_count || 0) + ' 次 / 执行 ' + (state.step_count || 0) + ' 步 / 工具调用 ' + (state.tool_call_count || 0) + ' 次</span></div>' : '';
  if (proposal?.follow_up?.length) $('#proposal').innerHTML += '<details><summary>后续跟进</summary><ul>' + proposal.follow_up.map(item => '<li>' + esc(item) + '</li>').join('') + '</ul></details>';
  if (state.errors?.length && ['failed','manual_review'].includes(status)) $('#review').innerHTML += '<div class="review-banner">' + state.errors.map(esc).join('<br>') + '</div>';
  if (record.manual?.decision) $('#review').innerHTML += '<div class="review-banner pass"><strong>人工复核结果：' + esc({approved:'同意建议',rejected:'拒绝建议',needs_evidence:'需要补充证据'}[record.manual.decision.outcome]) + '</strong><p>' + esc(record.manual.decision.note) + '</p></div>';
  $('#feedback').hidden = !proposal || active;
  $('#accept-btn').disabled = status !== 'completed' || !review?.passed;
  $('#feedback-status').textContent = record.feedback ? (record.feedback.outcome === 'accepted' ? '已采纳建议' : '已记录问题反馈') : '';
  const results = state.tool_results || [];
  $('#tool-evidence').innerHTML = results.length ? results.map(result => '<details><summary>' + esc(result.tool_name) + ' ' + badge(result.status === 'ok' ? 'completed' : 'failed') + '</summary>' + jsonView(result) + '</details>').join('') : '<p class="empty">暂无工具调用</p>';
  const order = results.find(r => r.tool_name === 'query_order' && r.status === 'ok')?.data;
  $('#order-evidence').innerHTML = order ? '<dl class="facts">' + [['商品',order.product_name],['订单状态',order.status],['实付', '¥' + order.amount],['保障期',order.protection_expired ? '已超期' : '未超期']].map(([k,v]) => '<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>').join('') + '</dl>' : '<p class="empty">订单尚未查询</p>';
  const logistics = results.find(r => r.tool_name === 'query_logistics' && r.status === 'ok')?.data;
  $('#logistics-evidence').innerHTML = logistics ? '<details><summary>' + esc(logistics.carrier) + ' · ' + esc(logistics.current_status) + '</summary>' + logistics.events.map(event => '<p>' + esc(event.time) + '<br>' + esc(event.status) + ' · ' + esc(event.location) + '</p>').join('') + '</details>' : '';
  $('#rules-evidence').innerHTML = (state.retrieved_rules || []).map(rule => '<details id="rule-' + esc(rule.id) + '"><summary>' + esc(rule.id) + ' · ' + esc(rule.title) + '</summary><p>' + esc(rule.content) + '</p><small>' + esc(rule.source) + ' / ' + esc(rule.version) + '</small></details>').join('') || '<p class="empty">暂无规则引用</p>';
  $('#memory').innerHTML = state.memory?.summary ? '<p>' + esc(state.memory.summary) + '</p><ul>' + state.memory.facts.map(fact => '<li>' + esc(fact) + '</li>').join('') + '</ul>' : '<p class="empty">暂无会话摘要</p>';
  icons();
}

function subscribe(record, ticketId) {
  disconnect();
  const source = new EventSource('/v1/runs/' + record.run_id + '/events');
  const binding = {source, run_id:record.run_id, ticketId};
  stream = binding;
  for (const type of ['started','update','done','paused']) source.addEventListener(type, event => {
    if (stream !== binding || selected !== ticketId || record.seen.has(event.lastEventId)) return;
    try {
      record.seen.add(event.lastEventId);
      const data = JSON.parse(event.data);
      if (type === 'started') record.status = 'running';
      if (type === 'update') {
        const delta = data.delta || {};
        record.state = {...record.state, ...delta};
        if (delta.status) record.status = delta.status;
        if (data.stage !== 'supervisor' && data.stage !== 'final') {
          const tool = data.stage === 'tool_agent' ? delta.tool_results?.at(-1) : null;
          const description = tool ? tool.tool_name + ' · ' + (tool.status === 'ok' ? '成功' : tool.error) : stages[data.stage];
          $('#timeline').insertAdjacentHTML('beforeend', '<li class="' + (tool?.status === 'error' ? 'fail' : '') + '"><span>' + esc(description) + '</span><time>' + new Date((data.created || 0) * 1000).toLocaleTimeString('zh-CN', {hour12:false}) + '</time></li>');
        }
      }
      if (type === 'done' || type === 'paused') {
        record.status = data.status; source.close(); stream = null; loadManual().catch(error => notice(error.message));
      }
      renderState(record); renderTickets();
      $('#connection').textContent = '服务正常';
    } catch (error) { notice(error.message); }
  });
  source.onerror = async () => {
    if (stream !== binding) return;
    $('#connection').textContent = '正在重连';
    try {
      const row = await api('/v1/runs/' + binding.run_id);
      if (stream !== binding) return;
      if (!['running','queued'].includes(row.status)) {
        source.close(); stream = null; record.status = row.status;
        record.state = row.final || record.state; renderState(record); renderTickets();
      }
    } catch (error) { source.close(); stream = null; notice(error.message); }
  };
}

async function processTicket() {
  const ticketId = selected, record = records.get(ticketId);
  $('#process-btn').disabled = true; notice('');
  try {
    if (!record.thread_id) record.thread_id = (await api('/v1/sessions', {method:'POST',body:JSON.stringify({ticket_id:ticketId})})).thread_id;
    const run = await api('/v1/runs', {method:'POST',body:JSON.stringify({ticket_id:ticketId,thread_id:record.thread_id,idempotency_key:crypto.randomUUID()})});
    const details = await api('/v1/runs/' + run.run_id);
    record.run_id = run.run_id; record.status = run.status; record.state = details.initial;
    record.seen = new Set(); record.feedback = null; record.manual = null;
    const ticket = tickets.find(t => t.ticket_id === ticketId); if (ticket) ticket.editable = false;
    if (selected === ticketId) { $('#timeline').innerHTML = ''; renderState(record); subscribe(record,ticketId); }
    renderTickets();
  } catch (error) { if (selected === ticketId) { notice(error.message); renderState(record); } }
}

async function loadManual() {
  const data = await api('/v1/manual-reviews');
  $('#manual-count').textContent = data.items.filter(item => item.status !== 'resolved').length;
  $('#manual-list').innerHTML = data.items.map(item => '<article class="manual-item"><div class="section-heading"><h3>' + esc(item.ticket_id) + '</h3><span class="status">' + esc({pending:'待领取',claimed:'复核中',resolved:'已复核'}[item.status]) + '</span></div><p>' + esc(item.final?.proposal?.summary || item.final?.errors?.join('；') || '需人工复核') + '</p><small>' + esc(item.final?.review?.issues?.join('；') || '') + '</small>' + (item.decision ? '<p>复核意见：' + esc(item.decision.note) + '</p>' : '') + (data.can_review && item.status === 'pending' ? '<p><button data-claim="' + esc(item.run_id) + '">领取任务</button></p>' : data.can_review && item.status === 'claimed' && item.assignee === staff.actor_id ? '<p><button data-decide="' + esc(item.run_id) + '">提交复核</button></p>' : '') + '</article>').join('') || '<p class="empty case-section">暂无人工复核任务</p>';
}

function showTab(tab) {
  document.body.dataset.view = tab;
  $('#ticket-view').hidden = tab !== 'ticket'; $('#manual-view').hidden = tab !== 'manual';
  $('#chat-view').hidden = tab !== 'chat'; $('#trash-view').hidden = tab !== 'trash';
  $('#conversation-sidebar').hidden = tab !== 'chat'; $('#management-sidebar').hidden = tab === 'chat';
  $('#chat-tab').classList.toggle('active',tab === 'chat');
  if (tab !== 'ticket') { epoch++; disconnect(); }
  $('#ticket-tab').classList.toggle('active',tab === 'ticket'); $('#manual-tab').classList.toggle('active',tab === 'manual');
}
async function safe(action) { try { await action(); } catch(error) { notice(error.message); } }

$('#ticket-list').onclick = event => { const button = event.target.closest('[data-ticket]'); if (button) safe(() => selectTicket(button.dataset.ticket)); };
$('#search-input').oninput = renderTickets; $('#status-filter').onchange = renderTickets;
$('#process-btn').onclick = () => safe(processTicket);
$('#menu-btn').onclick = () => $('#sidebar').classList.toggle('open');
$('#ticket-tab').onclick = () => safe(async () => { await refreshTicketData(); if (tickets.length) await selectTicket(tickets.find(t => t.ticket_id === selected)?.ticket_id || tickets[0].ticket_id); else { selected = null; showTab('ticket'); $('#ticket-title').textContent = '暂无工单'; $('#process-btn').disabled = true; } });
$('#manual-tab').onclick = () => { showTab('manual'); safe(loadManual); };
$('#refresh-manual').onclick = () => safe(loadManual);
$('#theme-btn').onclick = () => { const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; localStorage.setItem('aegis_theme',theme); };
$('#staff-btn').onclick = () => $('#staff-dialog').showModal();
$('#close-staff').onclick = () => $('#staff-dialog').close();
$('#staff-form').onsubmit = async event => { event.preventDefault(); const button = event.submitter; button.disabled = true; try { staff = await api('/v1/auth/session',{method:'POST',body:JSON.stringify({api_token:$('#staff-token').value})}); $('#staff-token').value = ''; $('#staff-error').textContent = ''; await loadWorkspace(); $('#staff-dialog').close(); } catch(error) { $('#staff-error').textContent = error.message; } finally { button.disabled = false; } };
$('#cancel-btn').onclick = () => safe(async () => { const record = records.get(selected); await api('/v1/runs/' + record.run_id + '/cancel',{method:'POST'}); await selectTicket(selected); });
$('#resume-btn').onclick = () => safe(async () => { const record = records.get(selected); await api('/v1/runs/' + record.run_id + '/resume',{method:'POST'}); record.status = 'running'; renderState(record); record.seen = new Set(); $('#timeline').innerHTML = ''; subscribe(record,selected); });
$('#supplement-form').onsubmit = event => { event.preventDefault(); safe(async () => { const ticketId = selected, record = records.get(ticketId), text = $('#supplement-input').value.trim(); if (!text) return; if (!record.thread_id) record.thread_id = (await api('/v1/sessions',{method:'POST',body:JSON.stringify({ticket_id:ticketId})})).thread_id; await api('/v1/sessions/' + record.thread_id + '/supplements',{method:'POST',body:JSON.stringify({text})}); record.supplements = [...(record.supplements || []),text].slice(-5); if (selected === ticketId) { $('#supplement-input').value = ''; renderState(record); } }); };
$('#copy-btn').onclick = () => safe(() => navigator.clipboard.writeText(JSON.stringify(records.get(selected).state.proposal,null,2)));
$('#export-btn').onclick = () => { const record = records.get(selected), blob = new Blob([JSON.stringify(record.state,null,2)],{type:'application/json'}), url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = selected + '.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000); };
async function feedback(outcome) { const record = records.get(selected); const note = outcome === 'rejected' ? $('#supplement-input').value.trim() : ''; await api('/v1/runs/' + record.run_id + '/feedback',{method:'POST',body:JSON.stringify({outcome,note})}); record.feedback = {outcome,note}; renderState(record); }
$('#accept-btn').onclick = () => safe(() => feedback('accepted')); $('#reject-btn').onclick = () => safe(() => feedback('rejected'));
$('#manual-list').onclick = event => { const claim = event.target.closest('[data-claim]'), decide = event.target.closest('[data-decide]'); if(claim) safe(async () => { await api('/v1/manual-reviews/' + claim.dataset.claim + '/claim',{method:'POST'}); await loadManual(); }); if(decide) { reviewTarget = decide.dataset.decide; $('#decision-note').value = ''; $('#decision-dialog').showModal(); } };
$('#close-decision').onclick = () => $('#decision-dialog').close();
$('#decision-form').onsubmit = event => { event.preventDefault(); safe(async () => { await api('/v1/manual-reviews/' + reviewTarget + '/decision',{method:'POST',body:JSON.stringify({outcome:$('#decision-outcome').value,note:$('#decision-note').value})}); $('#decision-dialog').close(); await loadManual(); }); };

document.documentElement.dataset.theme = localStorage.getItem('aegis_theme') || 'light';
icons();
safe(async () => {
  await api('/health');
  try { staff = await api('/v1/auth/session',{method:'POST'}); }
  catch(error) { $('#staff-error').textContent = error.message; $('#staff-dialog').showModal(); return; }
  $('#connection').textContent = '服务正常'; await loadWorkspace();
});
