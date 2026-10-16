"""Existing reference-consumption regressions, with only temporary fake media."""
import importlib.util, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('higgs_public_test',ROOT/'runtime/higgs/higgs_worker.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Fake:
    def loaded(self):return True
    def generate(self,*args):self.args=args;return b'RIFF-test'
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.ref=Path(self.tmp.name)/'synthetic.wav';self.ref.write_bytes(b'RIFF-fixture')
        self.engine=Fake();self.patches=[patch.object(m,'_engine',self.engine),patch.object(m,'require_auth'),patch.object(m,'REFERENCE_AUDIO',self.ref),patch.object(m,'REFERENCE_TEXT','合成参考')]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_request_reference_consumed(self):
        m.voice_clone({'input':'测试','reference_audio_path':str(self.ref),'reference_text':'另一份合成文本'})
        self.assertEqual(self.engine.args[2],self.ref.resolve());self.assertEqual(self.engine.args[3],'另一份合成文本')
    def test_missing_audio_rejected(self):
        with self.assertRaises(HTTPException) as c:m.voice_clone({'input':'测试','reference_audio_path':str(self.ref.with_name('absent.wav'))})
        self.assertEqual(c.exception.status_code,422)
    def test_empty_reference_rejected(self):
        with self.assertRaises(HTTPException):m.voice_clone({'input':'测试','reference_text':' '})
    def test_default_preserved(self):
        m.voice_clone({'input':'测试'});self.assertEqual(self.engine.args[2],self.ref.resolve());self.assertEqual(self.engine.args[3],'合成参考')
    def test_dll_arguments(self):
        class DLL:
            def audiocpp_generate_voice_clone(self,*args):self.args=args;return 1
            def audiocpp_free_result(self,*args):pass
        engine=m.HiggsEngine.__new__(m.HiggsEngine);engine.handle=1;engine.dll=DLL();engine.loaded=lambda:True;engine.error=lambda:'fixture'
        with self.assertRaises(RuntimeError):engine.generate('文本',{},self.ref,'合成文本')
        self.assertEqual(engine.dll.args[2],m.os.fsencode(self.ref));self.assertEqual(engine.dll.args[3],'合成文本'.encode())
if __name__=='__main__':unittest.main()
