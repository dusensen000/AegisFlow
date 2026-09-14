let conversations = [], orderOptions = [], conversationId = null, chatData = null, chatSource = null, chatEpoch = 0;
let ticketEditing = null, deleteAction = null, sendingChat = false;
const chatMessages = new Map();
const requestStates = new Map();
const chatProgress = new Map();
let retryPayload = null;

function closeChat() { chatSource?.close(); chatSource = null; chatEpoch++; }
function orderLabel(order) { return order.product_name + ' · ' + order.order_id + ' · ' + order.status; }
function conversationBusy() { return [...requestStates.values()].some(row => ['pending','processing'].includes(row.status)); }

async function loadConversations() {
  const [data, orders] = await Promise.all([api('/v1/conversations'),api('/v1/orders')]);
  conversations = data.conversations; orderOptions = orders.orders;
  $('#chat-order').innerHTML = '<option value="">自动关联订单</option>' + orderOptions.map(order => '<option value="' + esc(order.order_id) + '">' + esc(orderLabel(order)) + '</option>').join('');
  $('#ticket-order').innerHTML = orderOptions.map(order => '<option value="' + esc(order.order_id) + '">' + esc(orderLabel(order)) + '</option>').join('');
  renderConversations();
  if (conversations.length) await selectConversation(conversations[0].id); else newConversation();
}

async function refreshConversations() { const token = chatEpoch; const data = await api('/v1/conversations'); if (token !== chatEpoch) return; conversations = data.conversations; const current = conversations.find(row => row.id === conversationId); if (chatData && current) { chatData.title = current.title; chatData.order_id = current.order_id; } renderConversations(); renderChat(); }
function renderConversations() {
  const query = $('#conversation-search').value.toLowerCase();
  $('#conversation-list').innerHTML = conversations.filter(row => (row.title + (row.order_id || '')).toLowerCase().includes(query)).map(row => '<button class="conversation-item ' + (row.id === conversationId ? 'active' : '') + '" data-conversation="' + esc(row.id) + '"><strong>' + esc(row.title) + '</strong><small>' + esc(row.order_id || '待关联订单') + '</small><small>' + esc({pending:'正在理解诉求',processing:'正在处理',interrupted:'已中断',completed:'已回复',error:'解析失败',cancelled:'已停止'}[row.request?.status] || '待沟通') + '</small></button>').join('') || '<p class="empty">暂无历史会话</p>';
}

function newConversation() {
  closeChat(); conversationId = null; chatData = null; retryPayload = null;
  chatMessages.clear(); requestStates.clear(); chatProgress.clear();
  $('#chat-order').value = ''; $('#chat-input').value = '';
  showTab('chat'); renderChat(); renderConversations(); $('#sidebar').classList.remove('open');
}

async function selectConversation(cid) {
  closeChat(); const token = chatEpoch; conversationId = cid;
  chatMessages.clear(); requestStates.clear(); chatProgress.clear(); retryPayload = null;
  showTab('chat'); notice(''); $('#sidebar').classList.remove('open');
  const detail = await api('/v1/conversations/' + cid);
  if (token !== chatEpoch || conversationId !== cid) return;
  chatData = detail;
  detail.messages.forEach(message => chatMessages.set(message.id,message));
  [...detail.requests].reverse().forEach(row => requestStates.set(row.id,row));
  renderChat(); renderConversations(); subscribeConversation(cid);
}

function renderChat() {
  const previous = $('#chat-messages'), position = previous.scrollTop, nearBottom = previous.scrollHeight - previous.scrollTop - previous.clientHeight < 90;
  $('#conversation-title').textContent = chatData?.title || '售后客服会话';
  const busy = conversationBusy() || sendingChat;
  const latest = [...requestStates.values()].at(-1);
  $('#chat-phase').textContent = busy ? ([...chatProgress.values()].at(-1) || '正在理解诉求') : latest?.status === 'interrupted' ? '处理已中断，可继续处理' : '';
  $('#chat-input').disabled = busy;
  $('#chat-send-btn').disabled = busy;
  $('#chat-stop-btn').hidden = !conversationBusy() && latest?.status !== 'interrupted';
  $('#delete-chat-btn').disabled = !conversationId;
  $('#chat-order').disabled = busy || !!chatData?.order_id;
  if (chatData?.order_id) $('#chat-order').value = chatData.order_id;
  const messages = [...chatMessages.values()].sort((a,b) => a.created - b.created);
  previous.innerHTML = messages.map(message => '<article class="chat-message ' + esc(message.role) + '" data-message-id="' + esc(message.id) + '"><span class="chat-avatar"><i data-lucide="' + (message.role === 'user' ? 'user-round' : 'headset') + '"></i></span><div class="chat-message-content"><div class="chat-bubble">' + esc(message.content) + '</div><time>' + new Date(message.created * 1000).toLocaleString('zh-CN',{hour12:false}) + '</time>' + (message.data?.ticket_id ? '<div class="chat-links">' + badge(message.data.result_status || 'unprocessed') + '<button data-open-ticket="' + esc(message.data.ticket_id) + '"><i data-lucide="clipboard-list"></i>查看工单</button></div>' : '') + '</div></article>').join('') || '<article class="chat-message assistant"><span class="chat-avatar"><i data-lucide="headset"></i></span><div class="chat-message-content"><div class="chat-bubble">您好，遇到了什么售后问题？</div></div></article>';
  if (busy) previous.innerHTML += '<div class="chat-message assistant"><span class="chat-avatar"><i data-lucide="loader-circle"></i></span><div class="chat-thinking">' + esc($('#chat-phase').textContent) + '</div></div>';
  for (const row of requestStates.values()) if (row.status === 'interrupted') previous.innerHTML += '<div class="chat-message"><button data-retry-request="' + esc(row.id) + '"><i data-lucide="play"></i>继续处理</button></div>';
  if (nearBottom || sendingChat) previous.scrollTop = previous.scrollHeight; else previous.scrollTop = position;
  icons();
}

function subscribeConversation(cid) {
  chatSource?.close();
  const source = new EventSource('/v1/conversations/' + cid + '/events'); chatSource = source;
  const seen = new Set();
  for (const kind of ['message','status','progress']) source.addEventListener(kind,event => {
    if (chatSource !== source || conversationId !== cid || seen.has(event.lastEventId)) return;
    seen.add(event.lastEventId);
    try {
      const data = JSON.parse(event.data);
      if (kind === 'message') {
        if (data.data?.request_key) chatMessages.delete('pending-' + data.data.request_key);
        chatMessages.set(data.id,data);
      }
      if (kind === 'status') {
        requestStates.set(data.request_id,{...(requestStates.get(data.request_id) || {}),...data,id:data.request_id});
        if (data.order_id) chatData.order_id = data.order_id;
        if (!['pending','processing'].includes(data.status)) { chatProgress.clear(); safe(refreshConversations); }
      }
      if (kind === 'progress') chatProgress.set(data.run_id,stages[data.stage] || '正在核对');
      renderChat();
    } catch(error) { notice(error.message); }
  });
  source.onerror = () => {
    if (chatSource !== source) return;
    $('#chat-phase').textContent = '连接中断，正在重连';
    api('/v1/conversations/' + cid).catch(error => { if (chatSource === source) { closeChat(); notice(error.message); } });
  };
}

async function sendChat() {
  const text = $('#chat-input').value.trim();
  if (!text || sendingChat || conversationBusy()) return;
  const orderId = $('#chat-order').value || null;
  const payload = retryPayload && retryPayload.text === text && retryPayload.order_id === orderId ? retryPayload : {text,order_id:orderId,idempotency_key:crypto.randomUUID()};
  sendingChat = true; notice('');
  try {
    if (!conversationId) {
      chatData = await api('/v1/conversations',{method:'POST',body:JSON.stringify({order_id:orderId})});
      conversationId = chatData.id; subscribeConversation(conversationId);
    }
    const cid = conversationId;
    chatMessages.set('pending-' + payload.idempotency_key,{id:'pending-' + payload.idempotency_key,role:'user',content:text,created:Date.now()/1000});
    renderChat();
    const response = await api('/v1/conversations/' + cid + '/messages',{method:'POST',body:JSON.stringify(payload)});
    if (!requestStates.has(response.request_id)) requestStates.set(response.request_id,{...response,id:response.request_id});
    $('#chat-input').value = ''; retryPayload = null;
    await refreshConversations();
  } catch(error) { chatMessages.delete('pending-' + payload.idempotency_key); retryPayload = payload; notice(error.message); }
  finally { sendingChat = false; renderChat(); }
}

async function openChatTicket(tid) { await refreshTicketData(); await selectTicket(tid); }
function ticketDialog(edit=false) {
  ticketEditing = edit ? tickets.find(ticket => ticket.ticket_id === selected) : null;
  $('#ticket-dialog-title').textContent = edit ? '编辑工单' : '新建工单';
  $('#save-ticket-btn').textContent = edit ? '保存修改' : '创建工单';
  $('#ticket-order').disabled = edit; $('#auto-run-label').hidden = edit;
  if (ticketEditing) $('#ticket-order').value = ticketEditing.order_id;
  $('#ticket-problem').value = ticketEditing?.description || ''; $('#ticket-claim').value = ticketEditing?.user_claim || '';
  $('#ticket-action').value = ticketEditing?.requested_action || '退款'; $('#ticket-form-error').textContent = '';
  $('#ticket-dialog').showModal();
}

function deletionDialog(kind,id) {
  $('#delete-title').textContent = kind === 'ticket' ? '删除工单' : '删除会话';
  $('#delete-description').textContent = kind === 'ticket' ? id + ' 将移至工单回收站。处理记录保留。' : '会话将从历史列表移除，关联工单保留。';
  $('#delete-error').textContent = '';
  deleteAction = async () => {
    await api(kind === 'ticket' ? '/v1/tickets/' + id : '/v1/conversations/' + id,{method:'DELETE'});
    if (kind === 'ticket') {
      disconnect(); records.delete(id); await refreshTicketData();
      if (tickets.length) await selectTicket(tickets[0].ticket_id); else { selected = null; showTab('trash'); await loadTrash(); }
    } else { closeChat(); newConversation(); await refreshConversations(); }
  };
  $('#delete-dialog').showModal();
}

async function loadTrash() {
  showTab('trash'); const rows = (await api('/v1/tickets?deleted=true')).tickets;
  $('#trash-list').innerHTML = rows.map(ticket => '<article class="trash-item"><div><h3>' + esc(ticket.ticket_id) + '</h3><p>' + esc(ticket.description) + '</p><small>' + esc(ticket.order_id) + '</small></div><button data-restore-ticket="' + esc(ticket.ticket_id) + '"><i data-lucide="archive-restore"></i>恢复</button></article>').join('') || '<p class="empty case-section">回收站为空</p>';
  icons();
}

$('#chat-tab').onclick = () => { showTab('chat'); renderChat(); };
$('#new-chat-btn').onclick = newConversation;
$('#conversation-search').oninput = renderConversations;
$('#conversation-list').onclick = event => { const button = event.target.closest('[data-conversation]'); if (button) safe(() => selectConversation(button.dataset.conversation)); };
$('#chat-form').onsubmit = event => { event.preventDefault(); safe(sendChat); };
$('#chat-input').onkeydown = event => { if(event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('#chat-form').requestSubmit(); } };
$('#chat-messages').onclick = event => { const ticket = event.target.closest('[data-open-ticket]'), retry = event.target.closest('[data-retry-request]'); if(ticket) safe(() => openChatTicket(ticket.dataset.openTicket)); if(retry) safe(async () => { await api('/v1/conversations/' + conversationId + '/requests/' + retry.dataset.retryRequest + '/retry',{method:'POST'}); await selectConversation(conversationId); }); };
$('#chat-stop-btn').onclick = () => safe(async () => { await api('/v1/conversations/' + conversationId + '/cancel',{method:'POST'}); await selectConversation(conversationId); });
$('#delete-chat-btn').onclick = () => deletionDialog('conversation',conversationId);
$('#new-ticket-btn').onclick = () => ticketDialog();
$('#edit-ticket-btn').onclick = () => ticketDialog(true);
$('#delete-ticket-btn').onclick = () => deletionDialog('ticket',selected);
$('#close-ticket-dialog').onclick = () => $('#ticket-dialog').close();
$('#ticket-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const fields = {description:$('#ticket-problem').value.trim(),user_claim:$('#ticket-claim').value.trim() || $('#ticket-problem').value.trim(),requested_action:$('#ticket-action').value};
    const ticket = await api(ticketEditing ? '/v1/tickets/' + ticketEditing.ticket_id : '/v1/tickets',{method:ticketEditing ? 'PATCH' : 'POST',body:JSON.stringify(ticketEditing ? fields : {...fields,order_id:$('#ticket-order').value})});
    const autoRun = !ticketEditing && $('#ticket-auto-run').checked;
    await refreshTicketData(); await selectTicket(ticket.ticket_id); $('#ticket-dialog').close();
    if (autoRun) await processTicket();
  } catch(error) { $('#ticket-form-error').textContent = error.message; }
  finally { button.disabled = false; }
};
$('#close-delete-dialog').onclick = () => $('#delete-dialog').close();
$('#confirm-delete-btn').onclick = async () => { $('#confirm-delete-btn').disabled = true; try { await deleteAction(); $('#delete-dialog').close(); } catch(error) { $('#delete-error').textContent = error.message; } finally { $('#confirm-delete-btn').disabled = false; } };
$('#trash-btn').onclick = () => safe(loadTrash);
$('#back-tickets-btn').onclick = () => $('#ticket-tab').click();
$('#trash-list').onclick = event => { const button = event.target.closest('[data-restore-ticket]'); if(button) safe(async () => { await api('/v1/tickets/' + button.dataset.restoreTicket + '/restore',{method:'POST'}); await refreshTicketData(); await loadTrash(); }); };
