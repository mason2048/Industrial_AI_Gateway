import io
import json
import socket
import time
from pathlib import Path
from dataclasses import dataclass
import pytest
from fastapi.testclient import TestClient
from opcua import Server, ua
from backend.models import Tag, typed_value
from backend.database import Database
from backend.history import History, iso
from backend.tag_manager import import_excel, export_excel, validate_tags
from backend.opcua_client import Gateway, OPCUADriver
from backend.main import create_app
from seed_data import demo_tags


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path/'history.db')
    db.replace_tags(demo_tags())
    return db


def test_threshold_heartbeat_restart_quality_retention(db):
    tag = demo_tags()[0]
    h = History(db,heartbeat=1800,retention=7)
    now=time.time()
    def save(v,t,q='Good'): return h.save([tag],{1:{'value':v,'quality':q}},'simulation',now+t)
    assert save(5e-5,0)==1
    assert save(5.00001e-5,1)==0
    assert save(5.8e-5,2)==0
    assert save(6.1e-5,3)==1  # compare against saved baseline, not previous scan
    assert save(6.1e-5,1802)==0
    assert save(6.1e-5,1803)==1
    h=History(db,heartbeat=1800,retention=7)
    assert save(6.1e-5,1804)==0
    assert save(None,1805,'BadCommunicationError')==1
    assert save(None,1806,'BadCommunicationError')==0
    assert save(6.1e-5,1807)==1
    assert h.save([tag],{1:{'value':6.1e-5,'quality':'Good'}},'opcua',now+1808)==1
    assert db.cleanup(now+8*86400)==6


def test_bool_and_disabled_history(db):
    h=History(db)
    t=demo_tags()[3]
    assert h.save([t],{t.id:{'value':True,'quality':'Good'}},'simulation')==0
    t=t.model_copy(update={'save':True,'threshold':999})
    assert h.save([t],{t.id:{'value':True,'quality':'Good'}},'simulation')==1
    assert h.save([t],{t.id:{'value':False,'quality':'Good'}},'simulation')==1


@pytest.mark.parametrize('kind,value,expected',[('BOOL',True,True),('WORD',65535,65535),('DWORD',4294967295,4294967295),('FLOAT',5e-5,5e-5)])
def test_types(kind,value,expected): assert typed_value(value,kind)==expected


@pytest.mark.parametrize('kind,value',[('BOOL',1),('WORD',-1),('WORD',65536),('WORD',2.5),('DWORD',4294967296),('FLOAT',float('nan')),('FLOAT',float('inf')),('FLOAT',True)])
def test_invalid_types(kind,value):
    with pytest.raises(ValueError): typed_value(value,kind)


def test_excel_roundtrip_1000_and_atomic_identity(db):
    tags=demo_tags()+[Tag(id=i,address=f'SIM.{i}',name=f'Tag{i}',type='WORD') for i in range(7,1001)]
    assert import_excel(export_excel(tags))==tags
    db.replace_tags(tags)
    assert len(db.tags())==1000
    with pytest.raises(ValueError): validate_tags(tags+[Tag(id=1001,address='a',name='x',type='WORD')])
    with pytest.raises(ValueError): validate_tags(tags+[tags[0]])
    h=History(db)
    h.save(tags,{1:{'value':1.,'quality':'Good'}},'simulation')
    changed=[tags[0].model_copy(update={'address':'different'}),*tags[1:]]
    with pytest.raises(ValueError): db.replace_tags(changed)
    assert db.tags()[0]['address']==tags[0].address


def test_history_full_summary_pagination_source(db):
    now=time.time()
    db.insert_history([(1,now+i,i,'Good','simulation','真空泵01','真空压力','Pa','FLOAT') for i in range(30)])
    db.insert_history([(1,now+1,999,'Good','opcua','真空泵01','真空压力','Pa','FLOAT')])
    h=History(db)
    r=h.query(start=iso(now-1),end=iso(now+31),source='simulation',limit=5,offset=5)
    assert r['total']==30 and len(r['items'])==5 and r['has_more']
    assert r['summary'][0]['mean']==14.5
    assert r['items'][0]['value']==5
    with pytest.raises(ValueError): h.query(start=iso(now),end=iso(now-1))


@pytest.fixture
def app_root(tmp_path):
    (tmp_path/'config').mkdir()
    (tmp_path/'data').mkdir()
    (tmp_path/'frontend').mkdir()
    (tmp_path/'frontend/index.html').write_text('gateway',encoding='utf-8')
    config={'mode':'simulation','endpoint':'opc.tcp://127.0.0.1:4840','poll_interval':.1,'batch_size':100,'heartbeat_seconds':1800,'retention_days':7,'simulation_write_enabled':True,'backup_enabled':False}
    (tmp_path/'config/config.json').write_text(json.dumps(config),encoding='utf-8')
    (tmp_path/'data/tags.xlsx').write_bytes(export_excel(demo_tags()))
    return tmp_path


def test_end_to_end_api_permissions_and_replay(app_root):
    app=create_app(app_root)
    with TestClient(app) as c:
        for _ in range(30):
            data=c.get('/api/current').json()
            if data['good']==6: break
            time.sleep(.05)
        assert data['good']==6 and data['mode']=='simulation'
        for route in ['/','/api/health','/api/config','/api/tags','/api/ai/current','/api/ai/status','/api/ai/history','/openapi.json']:
            assert c.get(route).status_code==200,route
        assert c.get('/api/ai/status').json()['health']=='unknown'
        # Realtime becomes healthy before the independent writer commits its
        # first batch. Wait for the observable history result, not a fixed sleep.
        deadline=time.monotonic()+5
        while True:
            result=c.post('/api/ai/query',json={'question':'分析今天真空变化','device':'真空泵01'}).json()
            if result['evidence_count']>=1 or time.monotonic()>=deadline: break
            time.sleep(.05)
        assert result['evidence_count']>=1 and not result['plc_write_allowed']
        assert c.post('/api/ai/write',json={'tag_id':1,'value':2}).status_code in (404,405)
        assert c.get('/api/current',headers={'Host':'evil.example'}).status_code==403
        assert c.put('/api/tags',json=[],headers={'Origin':'https://evil.example'}).status_code==403
        table=c.get('/api/tags')
        before=table.json()
        headers={'X-Operator-Pin':(app_root/'data/operator_pin.txt').read_text(),'If-Match':table.headers['ETag']}
        assert c.post('/api/tags/import',files={'file':('bad.xlsx',b'not xlsx')},headers=headers).status_code==400
        assert c.get('/api/tags').json()==before
        exported=c.get('/api/tags/export')
        assert len(import_excel(exported.content))==6
        assert c.put('/api/tags',json=[],headers=headers).status_code==400
        assert c.post('/api/connection',json={'mode':'opcua','endpoint':'http://x'}).status_code==422
        assert c.post('/api/operator/write-request',json={'tag_id':1,'value':2}).status_code==403
        assert c.post('/api/operator/write-request',json={'tag_id':1,'value':2},headers=headers).status_code==403
        before[0]['permission']='WRITE'
        assert c.put('/api/tags',json=before,headers=headers).status_code==200
        proposal=c.post('/api/operator/write-request',json={'tag_id':1,'value':0.00009},headers=headers).json()
        confirm={'request_id':proposal['request_id'],'confirmation':proposal['confirmation']}
        assert c.post('/api/operator/write-confirm',json=confirm,headers=headers).status_code==200
        assert c.post('/api/operator/write-confirm',json=confirm,headers=headers).status_code==409
        time.sleep(.2)
        assert c.get('/api/current').json()['items'][0]['value']==0.00009
        with app.state.db.connect() as db:
            assert db.execute('SELECT outcome FROM write_audit').fetchone()[0]=='success'


def test_simulation_1000_collection(tmp_path):
    db=Database(tmp_path/'history.db')
    tags=[Tag(id=i,address=f'SIM.{i}',name=f'Tag{i}',type=['BOOL','WORD','DWORD','FLOAT'][i%4]) for i in range(1,1001)]
    db.replace_tags(tags)
    gateway=Gateway(db,History(db),{'mode':'simulation','endpoint':'opc.tcp://127.0.0.1:4840','poll_interval':.1})
    gateway.start()
    try:
        for _ in range(50):
            data=gateway.snapshot()
            if data['good']==1000: break
            time.sleep(.05)
        assert data['good']==1000
        assert not data['storage_error']
    finally: gateway.stop()


def test_real_opcua_protocol_1000_points_and_quality():
    # In-process OPC UA server exercises the actual TCP protocol, not a mocked client.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=Server()
    endpoint=f'opc.tcp://127.0.0.1:{port}'
    server.set_endpoint(endpoint)
    idx=server.register_namespace('IndustrialGatewayTest')
    obj=server.get_objects_node().add_object(idx,'Demo')
    types=[('BOOL',True,ua.VariantType.Boolean),('WORD',65535,ua.VariantType.UInt16),('DWORD',4294967295,ua.VariantType.UInt32),('FLOAT',5e-5,ua.VariantType.Float)]
    tags=[];nodes=[]
    for i in range(1,1001):
        kind,value,variant=types[(i-1)%4]
        node=obj.add_variable(ua.NodeId(f'Tag{i}',idx),f'Tag{i}',ua.Variant(value,variant))
        node.set_writable();nodes.append(node)
        tags.append(Tag(id=i,address=f'A{i}',name=f'Tag{i}',type=kind,permission='WRITE',node_id=node.nodeid.to_string()))
    server.start()
    driver=OPCUADriver({'endpoint':endpoint,'batch_size':100})
    try:
        started=time.perf_counter();result=driver.read(tags);elapsed=time.perf_counter()-started
        assert len(result)==1000 and all(v['quality']=='Good' for v in result.values())
        assert result[2]['value']==65535 and result[3]['value']==4294967295
        assert result[4]['value']==pytest.approx(5e-5)
        with pytest.raises(PermissionError):
            driver.write(tags[1],1234)
        assert nodes[1].get_value()==65535  # V1.1 must not modify even writable PLC nodes.
        missing=tags[0].model_copy(update={'node_id':f'ns={idx};s=Missing'})
        assert driver.read([missing])[1]['quality']=='BadNodeIdUnknown'
        wrong=tags[1].model_copy(update={'type':'BOOL'})
        assert driver.read([wrong])[2]['quality']=='BadTypeMismatch'
        driver.disconnect()
        assert driver.read(tags[:4])[1]['quality']=='Good'  # reconnect
        print(f'\nOPC UA 1000-point first batch scan with connection: {elapsed:.3f}s')
    finally:
        driver.disconnect();server.stop()


def test_opcua_connection_failure_marks_all_bad(db):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    gateway=Gateway(db,History(db),{'mode':'opcua','endpoint':f'opc.tcp://127.0.0.1:{port}','poll_interval':.1})
    gateway.start()
    try:
        for _ in range(100):
            data=gateway.snapshot()
            if data['last_scan']: break
            time.sleep(.05)
        assert not data['connected'] and data['good']==0
        assert all(x['value'] is None and x['quality']=='BadCommunicationError' for x in data['items'])
    finally: gateway.stop()
