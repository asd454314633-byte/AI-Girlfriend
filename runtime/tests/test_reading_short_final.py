"""Preserve verbatim source coverage and checkpoints when B joins a short tail."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'runtime/core'),str(ROOT/'runtime/ui/app')]
from providers.reading_strategy import ReadingUnitPlanner
import local_api as api

def source(index,text,last=False,chapter=0):
    return dict(document_id='fixture',chapter_index=chapter,chunk_index=index,
                text=text,char_start=index*100,char_end=index*100+len(text),is_last=last)

class ShortFinal(unittest.TestCase):
    def plan(self,strategy,tail):
        first=source(0,'窗外的树叶轻轻摇动，我们按照原来的顺序读完每一句。')
        first['following_sources']=[source(i+1,t,i==len(tail)-1) for i,t in enumerate(tail)]
        return ReadingUnitPlanner().submit(first['text'],first,strategy,3)
    def test_single_double_sentence_and_consecutive_tail(self):
        for tail in [['完'],['结束。'],['今天读完了。'],['好。','结束。']]:
            with self.subTest(tail=tail):
                result=self.plan('B',tail)
                self.assertFalse(result['pending'])
                self.assertEqual(result['completed_chunk_index'],len(tail))
                self.assertEqual([s['chunk_index'] for s in result['source_chunks']],list(range(len(tail)+1)))
                self.assertEqual(''.join(s['text'] for s in result['source_chunks']),
                                 '窗外的树叶轻轻摇动，我们按照原来的顺序读完每一句。'+''.join(tail))
                self.assertTrue(result['text'].endswith(tail[-1]+('。' if tail[-1]=='完' else '')))
    def test_A_C_do_not_join(self):
        for strategy in ['A','C']:
            result=self.plan(strategy,['结束。'])
            self.assertEqual(len(result['source_chunks']),1)
            self.assertEqual(result['completed_chunk_index'],0)
    def test_long_remaining_text_is_not_joined(self):
        result=self.plan('B',['这是正常长段，应继续保留独立提交和原有播放顺序。'])
        self.assertEqual(len(result['source_chunks']),1)
    def test_cross_chapter_or_nonsequential_tail_is_not_joined(self):
        for bad in [source(1,'结束。',True,1),source(2,'结束。',True)]:
            first=source(0,'这份正常正文应当独立处理。');first['following_sources']=[bad]
            result=ReadingUnitPlanner().submit(first['text'],first,'B',3)
            self.assertEqual(len(result['source_chunks']),1)
    def test_resume_at_final_never_replays_predecessor(self):
        final=source(5,'结束。',True)
        result=ReadingUnitPlanner().submit(final['text'],final,'B',4)
        self.assertEqual([s['chunk_index'] for s in result['source_chunks']],[5])
    def test_pending_prefix_and_final_tail_keep_source_order(self):
        p=ReadingUnitPlanner();self.assertTrue(p.submit('一。',source(0,'一。'),'B',4)['pending'])
        middle=source(1,'我们按照原来的顺序读完正文。')
        middle['following_sources']=[source(2,'结束。',True)]
        result=p.submit(middle['text'],middle,'B',4)
        self.assertEqual([s['chunk_index'] for s in result['source_chunks']],[0,1,2])
        self.assertFalse(p.pending)
    def test_api_builds_readonly_lookahead_without_progress_write(self):
        chunks=[source(0,'正文保持原样。'),source(1,'结束。',True)]
        view={'chunks':[dict(s,index=s['chunk_index']) for s in chunks]}
        with patch.object(api.reading,'chunk_location',return_value=chunks[0]),patch.object(api.reading,'chapter_view',return_value=view),patch.object(api.reading,'set_progress',side_effect=AssertionError('planning wrote progress')),patch.object(api,'_forward',return_value={'ok':True}) as forward:
            api.reading_speak(api.SpeakRequest(document_id='fixture',chunk_index=0))
            body=forward.call_args.args[2]
            self.assertEqual(body['text'],chunks[0]['text'])
            self.assertEqual(body['reading_source']['following_sources'][0]['text'],'结束。')
if __name__=='__main__':unittest.main(verbosity=2)
