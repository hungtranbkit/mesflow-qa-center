import pytest
pytest.importorskip("flask")
pytest.importorskip("requests")
import agent

class Resp:
    def __init__(self,status=200,body=None): self.status_code=status; self._body=body or {}; self.text=str(self._body); self.content=b'1'; self.url='http://x'
    def json(self): return self._body

class FakeClient:
    def __init__(self): self.deleted=[]
    def get(self,url,**kw):
        if '/production-orders?' in url: return Resp(body={'items':[{'id':1,'code':'QAV65817-PO-1','status':'IN_PROGRESS'},{'id':2,'code':'REAL-PO-1'}]})
        if '/employees?' in url: return Resp(body={'items':[{'id':3,'employee_no':'QAV65817-EMP-001','name':'QA'},{'id':4,'employee_no':'EMP-REAL'}]})
        if '/stations?' in url: return Resp(body={'items':[{'id':5,'code':'QAV65817-ST-001','name':'QA'},{'id':6,'code':'ST-REAL'}]})
        return Resp(404,{})
    def delete(self,url,**kw): self.deleted.append((url,kw.get('json'))); return Resp(body={'ok':True,'counts':{}})


def setup_function(): agent._runs.clear()


def test_legacy_prefix_cleanup_is_gone_even_with_active_run():
    agent._runs['r']=agent.RunState('r','functional')
    r=agent.app.test_client().post('/api/cleanup',json={'confirm':'DELETE QA DATA'})
    assert r.status_code==410
    assert r.json['error']=='LEGACY_QA_CLEANUP_DISABLED'


def test_legacy_cleanup_never_calls_target_api(monkeypatch):
    target=FakeClient()
    monkeypatch.setattr(agent,'session_from_config',lambda cfg:target)
    r=agent.app.test_client().post('/api/cleanup',json={'preview':True})
    assert r.status_code==410
    assert not target.deleted
