"""Strategy control seam: authority, cancellation before publish, fail closed."""
import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'runtime/ui/app'))
import local_api as api
from fastapi import HTTPException

class StrategyControl(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'tts_segmentation_strategy.txt'
        self.path.write_text('B\n')
        self.authority=patch.object(api,'READING_STRATEGY_FILE',self.path);self.authority.start()
    def tearDown(self):
        self.authority.stop();self.tmp.cleanup()
    def test_default_and_roundtrip_with_cancellation_barrier(self):
        for value in ('A','C','B'):
            before=self.path.read_text().strip()
            def stop(method,path):
                self.assertEqual((method,path),('post','/v1/speak/stop'))
                self.assertEqual(self.path.read_text().strip(),before)
                return {'ok':True,'generation':17}
            with patch.object(api,'_forward',side_effect=stop):
                result=api.reading_strategy_set(api.ReadingStrategyRequest(strategy=value))
            self.assertEqual(result['strategy'],value);self.assertEqual(result['generation'],17)
            self.assertEqual(api.reading_strategy_get()['strategy'],value)
    def test_invalid_choice_never_cancels(self):
        with patch.object(api,'_forward') as forward:
            with self.assertRaises(HTTPException):api.reading_strategy_set(api.ReadingStrategyRequest(strategy='D'))
            forward.assert_not_called()
        self.assertEqual(self.path.read_text(),'B\n')
    def test_failed_cancel_does_not_publish(self):
        with patch.object(api,'_forward',return_value={'ok':False,'reason':'UNREACHABLE'}):
            with self.assertRaises(HTTPException):api.reading_strategy_set(api.ReadingStrategyRequest(strategy='C'))
        self.assertEqual(self.path.read_text(),'B\n')
    def test_invalid_authority_falls_back_to_formal_default(self):
        self.path.write_text('invalid')
        self.assertEqual(api.reading_strategy_get()['strategy'],'B')

if __name__=='__main__':unittest.main()
