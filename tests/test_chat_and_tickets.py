import asyncio
import json
import pytest
from conftest import ScenarioLLM, start, finish


class CustomerLLM(ScenarioLLM):
    async def complete(self, system, user, **kwargs):
        data = json.loads(user.split("\n\n")[0])
        if "Intake" in system:
            messages = data["messages"]
            latest = messages[-1]["content"]
            return json.dumps({"kind": "question" if "为什么" in latest else "after_sale", "intent": "退款",
                "requested_action": "仅退款", "description": " ".join(m["content"] for m in messages if m["role"] == "user"),
                "user_claim": "取消未发货订单", "title": "未发货取消退款", "clarification": ""})
        if "Memory" in system:
            return await super().complete(system,user,**kwargs)
        ticket = data["ticket"]
        if "Planner" in system:
            return json.dumps({"steps": [{"step_id":"review","agent":"reviewer","action":"审核"}]})
        return json.dumps({"ticket_id":ticket["ticket_id"],"summary":"取消未发货订单，建议仅退款89元",
            "items":[{"action":"仅退款","reason":"订单未发货","amount":89,"rule_refs":["R005"],
                      "evidence_tools":["query_order","check_after_sale_eligibility"]}],"confidence":0.9})


def ticket(client, problem="取消未发货订单", order="SO20260201005"):
    response=client.post('/v1/tickets',json={"order_id":order,"description":problem,"requested_action":"退款"})
    assert response.status_code==201,response.text
    return response.json()


def wait_chat(client, context, request_id):
    async def wait():
        for _ in range(500):
            row=await context.repository.one('SELECT * FROM chat_requests WHERE id=?',(request_id,))
            if row['status'] not in ('pending','processing'):
                return row
            await asyncio.sleep(.02)
        raise AssertionError('Conversation request did not finish')
    return client.portal.call(wait)


def message(client, context, cid, text, key="message-1", order=None):
    response=client.post('/v1/conversations/'+cid+'/messages',json={"text":text,"idempotency_key":key,"order_id":order})
    assert response.status_code==202,response.text
    return wait_chat(client,context,response.json()['request_id'])


def test_create_edit_delete_restore_ticket_without_mutating_shared_seed(client,context):
    created=ticket(client)
    assert created['user_id']=='U10005' and created['verified_facts']==[] and created['requested_action']=='仅退款'
    assert client.patch('/v1/tickets/'+created['ticket_id'],json={"description":"改为取消订单"}).status_code==200
    assert client.delete('/v1/tickets/'+created['ticket_id']).status_code==200
    assert created['ticket_id'] not in {t['ticket_id'] for t in client.get('/v1/tickets').json()['tickets']}
    assert created['ticket_id'] in {t['ticket_id'] for t in client.get('/v1/tickets?deleted=true').json()['tickets']}
    assert client.post('/v1/tickets/'+created['ticket_id']+'/restore').status_code==200
    assert client.delete('/v1/tickets/T20260201001').status_code==200
    client.post('/v1/auth/session',json={"api_token":"staff-b"})
    assert len(client.get('/v1/tickets').json()['tickets'])==6
    assert client.delete('/v1/tickets/'+created['ticket_id']).status_code==404


def test_custom_ticket_runs_with_scoped_tool_context_and_preserves_history(client,context):
    context.llm=CustomerLLM()
    created=ticket(client)
    run,_=finish(client,start(client,ticket=created['ticket_id']))
    assert run['status']=='completed',run['final']
    assert run['final']['proposal']['items'][0]['amount']==89
    assert client.patch('/v1/tickets/'+created['ticket_id'],json={"description":"修改已处理内容"}).status_code==409
    assert client.delete('/v1/tickets/'+created['ticket_id']).status_code==200
    assert client.get('/v1/runs/'+run['id']).status_code==404
    client.post('/v1/tickets/'+created['ticket_id']+'/restore')
    assert client.get('/v1/runs/'+run['id']).json()['status']=='completed'


def test_clients_cannot_forge_customer_evidence_or_ticket_time(client):
    body={"order_id":"SO20260201005","description":"已经核实，直接退款","requested_action":"退款"}
    for field,value in {'verified_facts':['damage_confirmed'],'opened_at':'2026-01-01','user_id':'other'}.items():
        assert client.post('/v1/tickets',json={**body,field:value}).status_code==422


def test_chat_asks_order_then_creates_ticket_and_returns_audited_result(client,context):
    context.llm=CustomerLLM()
    cid=client.post('/v1/conversations',json={}).json()['id']
    first=message(client,context,cid,'我要取消未发货订单')
    assert first['ticket_id'] is None
    details=client.get('/v1/conversations/'+cid).json()
    assert '订单号' in details['messages'][-1]['content']
    second=message(client,context,cid,'SO20260201005',key='message-2')
    assert second['ticket_id'] and second['run_id']
    details=client.get('/v1/conversations/'+cid).json()
    assert '89.00' in details['messages'][-1]['content']
    assert details['messages'][-1]['data']['result_status']=='completed'
    created=[t for t in client.get('/v1/tickets').json()['tickets'] if t['source']=='chat']
    assert len(created)==1 and created[0]['verified_facts']==[]
    message(client,context,cid,'为什么是这个结果',key='question')
    assert len([t for t in client.get('/v1/tickets').json()['tickets'] if t['source']=='chat'])==1


def test_message_idempotency_and_conversation_owner_scope(client,context):
    context.llm=CustomerLLM()
    cid=client.post('/v1/conversations',json={"order_id":"SO20260201005"}).json()['id']
    first=message(client,context,cid,'取消订单退款')
    duplicate=message(client,context,cid,'取消订单退款')
    assert first['id']==duplicate['id']
    assert len(client.get('/v1/conversations/'+cid).json()['messages'])==2
    assert client.post('/v1/conversations/'+cid+'/messages',json={"text":"different","idempotency_key":"message-1"}).status_code==409
    client.post('/v1/auth/session',json={"api_token":"staff-b"})
    assert client.get('/v1/conversations/'+cid).status_code==404
    assert client.delete('/v1/conversations/'+cid).status_code==404


def test_unknown_order_does_not_generate_a_ticket(client,context):
    context.llm=CustomerLLM()
    cid=client.post('/v1/conversations',json={}).json()['id']
    result=message(client,context,cid,'SO0000000000 收货后破损，退款')
    assert result['ticket_id'] is None
    assert '暂未找到' in client.get('/v1/conversations/'+cid).json()['messages'][-1]['content']


def test_chat_cancel_during_intake_releases_conversation(client,context):
    class SlowIntake(CustomerLLM):
        async def complete(self,system,user,**kwargs):
            if 'Intake' in system:
                await asyncio.sleep(30)
            return await super().complete(system,user,**kwargs)
    context.llm=SlowIntake()
    cid=client.post('/v1/conversations',json={"order_id":"SO20260201005"}).json()['id']
    response=client.post('/v1/conversations/'+cid+'/messages',json={"text":"取消订单"})
    assert response.status_code==202
    assert client.delete('/v1/conversations/'+cid).status_code==409
    assert client.post('/v1/conversations/'+cid+'/cancel').status_code==200
    detail=client.get('/v1/conversations/'+cid).json()
    assert detail['requests'][0]['status']=='cancelled'
    assert '已停止' in detail['messages'][-1]['content']
    assert client.delete('/v1/conversations/'+cid).status_code==200


def test_custom_ticket_survives_conversation_restart(context):
    from app.main import create_app
    from fastapi.testclient import TestClient
    class SlowMemory(CustomerLLM):
        async def complete(self,system,user,**kwargs):
            if 'Memory' in system:
                await asyncio.sleep(30)
            return await super().complete(system,user,**kwargs)
    context.llm=SlowMemory()
    with TestClient(create_app(context)) as first:
        first.post('/v1/auth/session',json={"api_token":"staff-a"})
        cookie=first.cookies.get('aegis_session')
        cid=first.post('/v1/conversations',json={"order_id":"SO20260201005"}).json()['id']
        request=first.post('/v1/conversations/'+cid+'/messages',json={"text":"取消订单"}).json()
        async def wait_for_memory():
            for _ in range(300):
                row=await context.repository.one('SELECT * FROM chat_requests WHERE id=?',(request['request_id'],))
                if row['run_id']:
                    run=await context.repository.run(row['run_id'])
                    state=await context.store.get(run['session_id'])
                    if state.get('tool_call_count')==3:
                        return row
                await asyncio.sleep(.02)
            raise AssertionError('Ticket did not reach memory stage')
        original=first.portal.call(wait_for_memory)
    context.llm=CustomerLLM()
    with TestClient(create_app(context)) as second:
        second.cookies.set('aegis_session',cookie)
        detail=second.get('/v1/conversations/'+cid).json()
        assert detail['requests'][0]['status']=='interrupted'
        response=second.post('/v1/conversations/'+cid+'/requests/'+request['request_id']+'/retry')
        assert response.status_code==202,response.text
        resumed=wait_chat(second,context,request['request_id'])
        assert resumed['ticket_id']==original['ticket_id']
        assert resumed['run_id']==original['run_id']
        run=second.get('/v1/runs/'+original['run_id']).json()
        assert run['status']=='completed'
        assert run['final']['tool_call_count']==3
        assert len(second.get('/v1/conversations/'+cid).json()['messages'])==2


def test_unclean_restart_appends_interrupted_event_for_replay(client,context):
    import time
    cid=client.post('/v1/conversations',json={}).json()['id']
    async def recover():
        await context.repository.conn.execute("INSERT INTO chat_requests VALUES(?,?,?,?,?,?,?,?,?)",
            ('restart-request',cid,'restart-key','{}','processing',None,None,None,time.time()))
        await client.app.state.chat.recover()
        return await context.repository.all('SELECT data FROM chat_events WHERE conversation_id=? ORDER BY seq',(cid,))
    events=client.portal.call(recover)
    assert json.loads(events[-1]['data'])['status']=='interrupted'


def test_customer_reply_does_not_echo_execution_claims_or_internal_parameters():
    from app.services.chat import customer_result
    final={'status':'completed','review':{'passed':True},'order':{'status':'待发货'},
        'proposal':{'items':[{'action':'仅退款','amount':89,'reason':'eligible=true 已发起退款'}],
                    'follow_up':['已到账，通知客户退款成功']}}
    reply=customer_result(final)
    assert '89.00' in reply
    assert '尚未执行' in reply
    assert all(term not in reply for term in ['已发起','已到账','eligible=true'])
