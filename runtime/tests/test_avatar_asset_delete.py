"""All deletions use temporary assets; production avatars are never removed."""
import concurrent.futures
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'runtime/ui/app'))
import avatar_admin as admin
import avatar_assets as assets
from fastapi import FastAPI
from fastapi.testclient import TestClient


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='avatar_delete_test_')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.data=self.root/'data/avatars'; self.data.mkdir(parents=True)
        self.state=self.root/'state'; self.state.mkdir()
        self.original=self.root/'original.mp4';self.original.write_bytes(b'untouched original')
        self.entry={'id':'av_test1234','编号':'vtest123456','display_name':'删除测试人物','state':'ready',
                    'builtin':False,'dir':str(self.data/'av_test1234'),'source_video':{'name':str(self.original)}}
        self.path=Path(self.entry['dir']); self.path.mkdir();(self.path/'frames.bin').write_bytes(b'generated')
        self.registry=self.state/'avatars.json'
        self.save({'avatars':[copy.deepcopy(self.entry)]})
        (self.state/'active_avatar.txt').write_text('av_active1234',encoding='ascii')
        (self.state/'avatar_runtime_state.json').write_text(json.dumps({'state':'RUNNING','avatar_id':'av_active1234'}))
        (self.state/'avatar_switch_state.json').write_text(json.dumps({'busy':False}))
        for key,value in {'AVATAR_DATA_DIR':self.data,'AVATARS_JSON':self.registry,'POINTER_FILE':self.state/'active_avatar.txt',
            'AVATAR_RUNTIME_STATE':self.state/'avatar_runtime_state.json','SWITCH_STATE':self.state/'avatar_switch_state.json',
            'LOG_DIR':self.root/'logs','_registry_cache':None,'_making_slot':None,'_switch_state':{'busy':False},'_runner':None}.items():
            p=patch.object(admin,key,value);p.start();self.addCleanup(p.stop)
        for key,value in {'_port_open':False,'_runtime_avatar_id':'av_active1234'}.items():
            p=patch.object(admin,key,return_value=value);p.start();self.addCleanup(p.stop)

    def save(self,data):self.registry.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    def preview(self):return assets.operation(admin,assets.DeleteRequest(编号=self.entry['编号']))
    def delete(self,**extra):
        req={'编号':self.entry['编号'],'确认':True,'校验':assets.revision(self.entry),**extra}
        return assets.operation(admin,assets.DeleteRequest(**req),True)
    def mutate(self,**fields):
        self.entry.update(fields);self.save({'avatars':[copy.deepcopy(self.entry)]})
    def refused(self,code):
        self.assertEqual(self.delete()['reason'],code);self.assertTrue(self.path.exists())

    def test_preview_is_readonly_and_commit_preserves_original(self):
        before=self.registry.read_bytes();preview=self.preview()
        self.assertTrue(preview['ok']);self.assertEqual(self.registry.read_bytes(),before)
        result=self.delete();self.assertTrue(result['ok']);self.assertFalse(self.path.exists())
        folder=self.data.parent/'_deleted_avatars'/result['恢复编号']
        self.assertEqual((folder/'assets/frames.bin').read_bytes(),b'generated')
        self.assertEqual(json.loads((folder/'manifest.json').read_text())['state'],'committed')
        self.assertEqual(self.original.read_bytes(),b'untouched original')
        self.assertEqual(json.loads(self.registry.read_text())['avatars'],[])
        self.assertEqual((self.state/'active_avatar.txt').read_text(),'av_active1234')
        self.assertEqual(self.delete()['reason'],'ASSET_NOT_FOUND')

    def test_builtin_protected(self):self.mutate(builtin=True);self.refused('BUILTIN_PROTECTED')
    def test_missing_builtin_is_protected(self):
        del self.entry['builtin'];self.save({'avatars':[self.entry]});self.refused('BUILTIN_PROTECTED')
    def test_selected_protected(self):
        (self.state/'active_avatar.txt').write_text(self.entry['id']);self.refused('ACTIVE_PROTECTED')
    def test_runtime_mismatch_protected(self):
        (self.state/'avatar_runtime_state.json').write_text(json.dumps({'state':'RUNNING','avatar_id':self.entry['id']}))
        self.refused('ACTIVE_PROTECTED')
    def test_switch_and_making_protected(self):
        (self.state/'avatar_switch_state.json').write_text('{"busy":true,"previous":"av_test1234"}')
        self.refused('SWITCH_BUSY')
        (self.state/'avatar_switch_state.json').write_text('{"busy":false}')
        with patch.object(admin,'_making_slot',{'avatar_id':'other'}):self.refused('MAKING_BUSY')
    def test_corrupt_state_fails_closed(self):
        (self.state/'avatar_runtime_state.json').write_text('{');self.refused('STATE_UNREADABLE')
    def test_path_escape_shared_and_link_protected(self):
        self.mutate(dir=str(self.root));self.refused('UNSAFE_PATH')
        self.mutate(dir=str(self.path))
        self.save({'avatars':[self.entry,{**self.entry,'id':'another','编号':'other'}]});self.refused('UNSAFE_PATH')
        self.save({'avatars':[self.entry]})
        native=Path.is_symlink
        with patch.object(Path,'is_symlink',lambda p:p==self.path or native(p)):self.refused('UNSAFE_PATH')
    def test_confirmation_and_revision_required(self):
        self.assertEqual(self.delete(确认=False)['reason'],'CONFIRM_REQUIRED')
        self.assertEqual(self.delete(校验='stale')['reason'],'ASSET_CHANGED');self.assertTrue(self.path.exists())
    def test_registry_write_failure_rolls_back_move(self):
        original=admin._write_json_atomic
        def fail(path,data):
            if path==self.registry:raise PermissionError('injected')
            return original(path,data)
        with patch.object(admin,'_write_json_atomic',side_effect=fail):self.refused('DELETE_FAILED')
        self.assertEqual((self.path/'frames.bin').read_bytes(),b'generated')
        self.assertEqual(len(json.loads(self.registry.read_text())['avatars']),1)
    def test_move_failure_keeps_registry(self):
        original=assets.os.replace
        def fail(src,dest):
            if Path(src)==self.path:raise PermissionError('injected')
            return original(src,dest)
        with patch.object(assets.os,'replace',side_effect=fail):self.refused('DELETE_FAILED')
    def test_concurrent_delete_only_commits_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:self.delete(),range(2)))
        self.assertEqual(sum(r['ok'] for r in results),1)
    def test_stale_registry_writer_cannot_resurrect(self):
        old=copy.deepcopy(admin._read_registry());self.assertTrue(self.delete()['ok'])
        admin._write_registry(old)
        self.assertEqual(json.loads(self.registry.read_text())['avatars'],[])
    def test_interrupted_rename_is_recovered_before_next_operation(self):
        folder=self.data.parent/'_deleted_avatars'/('a'*32);folder.mkdir(parents=True)
        record={'state':'prepared','entry':self.entry,'source':str(self.path)}
        (folder/'manifest.json').write_text(json.dumps(record))
        assets.os.replace(self.path,folder/'assets')
        self.assertTrue(self.preview()['ok']);self.assertTrue(self.path.exists())
        self.assertEqual(json.loads((folder/'manifest.json').read_text())['state'],'rolled_back')
    def test_api_confirmation_and_cross_origin(self):
        app=FastAPI();app.include_router(admin.router)
        with TestClient(app,client=('127.0.0.1',32100)) as client:
            url='/api/avatar/delete'
            bad=client.post(url,json={'编号':self.entry['编号']},headers={'Origin':'https://evil.example'})
            self.assertEqual(bad.json()['reason'],'ORIGIN_DENIED')
            preview=client.post('/api/avatar/delete/preview',json={'编号':self.entry['编号']}).json()
            result=client.post(url,json={'编号':self.entry['编号'],'确认':True,'校验':preview['校验']}).json()
            self.assertTrue(result['ok'])
    def test_real_supervisor_mutex_contention(self):
        ready=threading.Event(); release=threading.Event()
        def hold():
            with assets.lifecycle_lock():ready.set();release.wait(3)
        t=threading.Thread(target=hold);t.start();self.assertTrue(ready.wait(2))
        try:self.assertEqual(self.preview()['reason'],'LIFECYCLE_BUSY')
        finally:release.set();t.join(3)


if __name__=='__main__':unittest.main(verbosity=2)
