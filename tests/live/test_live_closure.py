import ast
import copy
import json
from pathlib import Path
import unittest
from runtime.live.normalizer import EventNormalizer,load_bridge
from runtime.live.router import LiveInteractionRouter

ROOT=Path(__file__).resolve().parents[2]
CFG=json.loads((ROOT/'config/live.example.json').read_text(encoding='utf-8-sig'))
BRIDGE=load_bridge(ROOT / CFG['bridge_path'])

def packet(mid='1',text='@AI 你好呀？'):
    return {'Type':1,'Data':{'MsgId':mid,'RoomId':'test','User':{'Id':'u','Nickname':'测试'},'Content':text}}

class Closure(unittest.TestCase):
    def setUp(self):
        cfg=copy.deepcopy(CFG);cfg.update(enabled=True,selection_delay=0,room_cooldown=0,user_cooldown=0)
        self.r=LiveInteractionRouter(cfg,EventNormalizer(BRIDGE),clock=lambda:1000)
        tree=ast.parse((ROOT/'runtime/live/service.py').read_text(encoding='utf-8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='fixture_packet')
        ns={'copy':copy,'bridge':BRIDGE}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'fixture_packet','exec'),ns)
        self.fixture=ns['fixture_packet']
    def test_owner_disconnect_expires_inflight(self):
        self.r.ingest(packet());job=self.r.claim('owner','persona')
        self.r.expire(1000+CFG['dispatch_timeout']+1)
        self.assertIsNone(self.r.inflight)
        self.assertEqual(self.r.counts['replies_failed'],1)
        self.assertFalse(self.r.phase('owner',job['ticket'],'completed',{'audible':True},now=1200))
    def test_timeout_not_requeued_or_duplicated(self):
        self.r.ingest(packet());self.r.claim('owner','persona')
        self.r.expire(1000+CFG['dispatch_timeout']+1)
        self.r.ingest(packet())
        self.assertEqual(self.r.counts['comments_deduplicated'],1)
        self.assertEqual(self.r.queue,[])
    def test_simulation_separate_from_real_source(self):
        self.assertEqual(EventNormalizer(BRIDGE).normalize(packet())['source'],'douyin')
        self.assertEqual(EventNormalizer(BRIDGE).normalize(self.fixture(packet(),'run1'))['source'],'simulation')
    def test_fixture_preserves_original_and_duplicate_identity(self):
        original=packet();before=copy.deepcopy(original)
        first=self.fixture(original,'run1');duplicate=self.fixture(original,'run1')
        self.assertEqual(original,before)
        self.assertEqual(first,duplicate)
        self.assertNotEqual(first,self.fixture(original,'run2'))
    def test_missing_ids_do_not_collapse_distinct_fixture_messages(self):
        self.r.ingest(self.fixture(packet('',text='@AI 你好呀？'),'run'))
        self.r.ingest(self.fixture(packet('',text='@AI 今天天气怎么样？'),'run'))
        self.assertEqual(len(self.r.queue),2)
    def test_disabled_router_does_not_queue_fixture(self):
        self.r.set_enabled(False);self.r.ingest(self.fixture(packet(),'run'))
        self.assertEqual(self.r.queue,[])

if __name__=='__main__':unittest.main(verbosity=2)
